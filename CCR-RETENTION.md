# CCR Retention — Cluster G reframe + the "free lunch" we want back

> Status: **invariant satisfied** (no silent loss). **Open challenge:** true
> cross-call retention so evicted data is actually still *there*, not merely
> missed loudly. This file exists so we can return to that — the free lunch has
> always been the goal.

## What Cluster G actually is

The break-eval flagged *"CCR FIFO eviction → unbacked sentinels"* (Cluster G,
`ttl-capacity-eviction-unbacked-sentinel-v1`) as a **silent-loss** defect: the
in-memory store has a fixed `max_entries = 1000` cap (`in_memory.rs:87/119`,
Python `compression_store.py`), so a long enough session evicts the oldest
whole-blob entries (FIFO). A `<<ccr:HASH>>` sentinel emitted earlier then points
at data that is gone.

Independent verification **decoupled two separate concerns** that the original
framing fused:

| # | Concern | Question | Answer (measured) |
|---|---------|----------|-------------------|
| 1 | **Eviction policy** | *How often* is data unavailable? | Real — cap overflow drops the oldest entry. |
| 2 | **Miss behavior** | When unavailable, is the loss **silent** or **loud**? | **LOUD** — the model gets an explicit error. |

The "no silent loss" requirement constrains **#2 only**. It never promised
"never evict" (#1) — an in-memory store with a fixed cap *cannot* promise that
(the Rust doc comment already says as much).

### Why the loss is already loud (measured, not assumed)

There are exactly **two model-facing retrieval surfaces**, and both miss loudly:

- **Proxy / handler** — `CCRResponseHandler._execute_retrieval`
  (`headroom/ccr/response_handler.py`): calls `store.get_entry_status(...)` and,
  on a miss, returns `success=False` with an explicit `error` payload — for the
  bulk path, the search path, AND a real granular `#rows` offload.
- **MCP tool** — `HeadroomMCPServer._retrieve_content`
  (`headroom/ccr/mcp_server.py:451`): on a local-store + proxy miss, returns an
  explicit `error` dict (now routed through the same cause-honest helper).

The only other `store.retrieve()` caller, `context_tracker._execute_expansions`
(`:513`), is **proactive prefetch**, not a model request — a miss there just
skips one speculative expansion; the model's own explicit retrieval stays loud.
So no surface returns a silent `None`/empty to the model.

Probe (10 calls × 220 rows → cap overflow → retrieve the evicted call-0 sentinel
through the handler):

```
evicted_after_10_calls = True        # concern #1: eviction happened
model-facing retrieve  : success=False, loud=True   # concern #2: LOUD, never silent None
```

**Granular offloads are all-or-nothing for the model.** A granular sentinel is
`{"_ccr_dropped": "<<ccr:HASH N_rows_offloaded>>", "_ccr_rows": "<<ccr:HASH#rows N_chunks>>"}`
(`crusher.rs:113`). Both markers share the **same** `HASH`; the bare `HASH` the
model retrieves is backed by a **single whole-blob entry holding all rows**
(verified: `rows_in_original_content = 240` for a 240-row offload). The per-row
chunks under `HASH#rows` are a *proportional-retrieve optimization* the model
never addresses directly, so partial chunk eviction can never hand the model a
**silent subset** — the bare-hash retrieve either returns the complete blob or
misses loudly.

**Conclusion: G as a *silent-loss* defect does not reproduce.** The invariant
held at the retrieval layer before any change here.

## What changed (the one real residual: diagnostic honesty)

A capacity-evicted entry returns `status="missing"` (indistinguishable from
never-stored without per-eviction tracking). The old miss message was:

```
Entry not found (CCR TTL: 300 seconds)
```

That **misattributes a capacity eviction to the TTL** — misleading for the
common 1000-entry-overflow case (the entry may not have come anywhere near its
TTL). Fixed to be **cause-honest** (`format_retrieval_miss_detail`,
`compression_store.py`), with no new stateful tracking:

```
Entry no longer retrievable from the CCR store: it was evicted under capacity
pressure (store capacity: 1000 entries), expired (TTL 300s), or was never
stored. Recompute the source content, or configure a durable CCR backend
(Sqlite/Redis) for longer retention.
```

A genuinely TTL-*expired* entry keeps its exact wording (`status="expired"` →
`"Entry expired (CCR TTL: 300 seconds; age: N seconds)"`) — that cause is known.

Locked by `tests/test_ccr_eviction_loud_miss.py` (loudness for bulk + granular,
cause-honesty, and that real expiry keeps its precise cause).

## The free lunch — true retention (OPEN, return here)

Loud-on-miss is *correct* but it is not *free*: the model still loses access to
the evicted data and must recompute the source (if it even can). The standing
goal is **retention**: keep the data actually retrievable for as long as the
sentinel can plausibly be referenced, ideally at ~zero added cost. Options, with
trade-offs, ranked by how close they are to a free lunch:

1. **Durable backend (already built, just wire it).** `Sqlite`/`Redis`
   `CompressionStoreBackend` implementations already exist. Spilling evicted
   entries to a durable backend converts "evicted → gone" into "evicted from
   RAM → still on disk." Closest to free: bounded RAM, near-unbounded retention,
   data actually present. Cost: I/O on the cold path + a config/lifecycle story
   (when is the durable store cleared?). **Most promising.**

2. **Session-scoped / conversation-lifetime retention.** Tie entry lifetime to
   the conversation that emitted the sentinel rather than a global FIFO+TTL. A
   sentinel can only be referenced from within its own conversation, so an entry
   is dead exactly when its conversation ends — evict on session close, not on
   global cap. Cost: needs a session/conversation key threaded into the store;
   unbounded within a pathologically long single session (combine with #1).

3. **Reference-counted retention.** Keep an entry alive while any live sentinel
   in the active context still points at it; evict only when the refcount hits
   zero. Precise, no arbitrary TTL. Cost: requires tracking which sentinels are
   still in-context (the compressor sees inputs, not the evolving model context)
   — the hardest to make correct.

4. **Spill-to-disk LRU (a thinner #1).** Same idea as the durable backend but as
   a local LRU file cache rather than a full Sqlite/Redis dependency. Cheaper to
   adopt, weaker durability/query story.

5. **Raise the cap.** Trivial, buys headroom, but only *delays* eviction and
   grows RAM linearly — not a real solution, only a knob.

**Recommended next step when we return:** wire the existing durable backend
(#1) as the eviction spill target, keyed by session (#2) for cleanup — that
combination gives bounded RAM *and* genuinely-retained data (the free lunch),
reusing code that already exists rather than building new retention machinery.

## Cross-references
- `EVAL-break.md` — Cluster G original finding (row 6) + this reframe.
- `headroom/ccr/response_handler.py` — `_execute_retrieval` (loud miss).
- `headroom/ccr/mcp_server.py` — `_retrieve_content` (second loud surface).
- `headroom/cache/compression_store.py` — `format_retrieval_miss_detail`,
  `get_entry_status`, `CompressionStoreBackend` (Sqlite/Redis live here).
- `tests/test_ccr_eviction_loud_miss.py` — the locking regression tests.
