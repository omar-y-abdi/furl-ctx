# CCR Retention — Cluster G reframe + the "free lunch" we want back

> **Delivered guarantee:** retrieval is byte-exact ONLY within the in-memory
> window — at most 1000 live entries (`max_entries`) and at most 1800s old
> (`default_ttl`, session-scale since Engine P0-3; env-overridable via
> `FURL_CCR_TTL_SECONDS`). After capacity eviction (oldest-created-first) or TTL expiry
> the entry's payload is deleted from the single-tier store and is gone; a later
> retrieve returns a **loud, cause-honest miss** (never a silent `None`). So the
> invariant — **no silent loss** — holds, but it was never "never evict."
>
> Byte-exact here means the raw offloaded bytes for text. A structured JSON array
> recovers as a semantically-complete re-serialization of the same rows, not the
> original bytes. `FURL_CCR_TTL_SECONDS` and the other store env vars are read once,
> when the store is first built in a process, and changing them later in the same
> process is silently ignored.
>
> **Update (Engine P1-7, 2026-07-03):** a durable CCR backend now EXISTS —
> `SqliteBackend` (`furl_ctx/cache/backends/sqlite.py`), the MCP server's
> default store backend (`FURL_CCR_BACKEND=memory` opts out; the library
> default stays in-memory). It closes the restart-loss and cross-process
> retrieval gaps: un-evicted entries survive an MCP restart and sub-agent
> processes resolve main-agent hashes through the shared workspace file.
> It does NOT widen the eviction window — see option 1 below.
>
> **Update (Q10 spill tier, 2026-07-06 + 2026-07-08): SHIPPED, opt-in.** The
> retention half described as open below is now built. `FURL_CCR_SPILL=1`
> (`compression_store.py:1729` `CCR_SPILL_ENV`, `:1733`
> `_create_spill_backend_from_env`, wired into `get_compression_store` at
> `:1794`) demotes a capacity-evicted entry to a durable `SqliteBackend`
> instead of deleting it: `_evict_if_needed` calls `_spill_evicted` (`:1341`,
> best-effort/fail-open) BEFORE the primary `delete` (`:1436-1442`), and
> `retrieve()` falls through to `_recover_from_spill` (`:614`, `:1357`) on a
> primary miss — a spill hit returns byte-identical to the pre-eviction value,
> no promotion, no bookkeeping mutation. Lifecycle is bounded by the spill
> `SqliteBackend`'s own row-cap/TTL (`FURL_CCR_SQLITE_MAX_ROWS`), answering the
> "when is the durable store cleared?" question below. Locked by
> `tests/test_ccr_spill_tier.py`. **Off by default** — this is genuinely
> opt-in (default single-tier, matching the guarantee documented at the top of
> this file), not a silent behavior change. See the fully-updated §"The free
> lunch" below for the option-by-option disposition.

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

The **only live model-facing retrieval surface** today is the MCP tool, and it
misses loudly:

- **MCP tool (live)** — `FurlMCPServer._retrieve_content`
  (`furl_ctx/ccr/mcp_server.py:322`): on a store miss it returns an explicit
  `error` dict routed through the cause-honest helper
  (`format_retrieval_miss_detail`, `mcp_server.py:388`). This is what the model
  reaches via the `furl_retrieve` tool.
- **Proxy / handler (archived, NOT live)** — `CCRResponseHandler._execute_retrieval`
  once provided a second loud surface, calling `store.get_entry_status(...)` and
  returning `success=False` with an explicit `error` payload for the bulk path,
  the search path, AND a real granular `#rows` offload. That module now lives at
  `archive/furl_ctx/ccr/response_handler.py` and is no longer wired into a live
  retrieval path. Kept here only for the historical loud-miss measurement below.

The only other `store.retrieve()` caller, `context_tracker._execute_expansions`,
is **proactive prefetch**, not a model request — a miss there just skips one
speculative expansion; the model's own explicit retrieval stays loud. So no live
surface returns a silent `None`/empty to the model.

Probe (10 calls × 220 rows → cap overflow → retrieve the evicted call-0 sentinel;
measured against the then-live handler, retained as historical evidence):

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
pressure (store capacity: 1000 entries), expired (TTL 1800s), or was never
stored. Recompute the source content.
```

A genuinely TTL-*expired* entry keeps its exact wording (`status="expired"` →
`"Entry expired (CCR TTL: 1800 seconds; age: N seconds)"`) — that cause is known.

Locked by `tests/test_ccr_eviction_loud_miss.py` (loudness for bulk + granular,
cause-honesty, and that real expiry keeps its precise cause).

## The free lunch — true retention (#1 SHIPPED opt-in; #2-#5 still open)

Loud-on-miss is *correct* but it is not *free*: the model still loses access to
the evicted data and must recompute the source (if it even can). The standing
goal is **retention**: keep the data actually retrievable for as long as the
sentinel can plausibly be referenced, ideally at ~zero added cost. Options, with
trade-offs, ranked by how close they are to a free lunch:

1. **Durable backend + spill tier (BUILT — Engine P1-7 durable backend,
   2026-07-03; Q10 spill tier, 2026-07-06 PR #30, extended 2026-07-08 B2).**
   `SqliteBackend` (`furl_ctx/cache/backends/sqlite.py`) ships alongside
   `InMemoryBackend`: WAL journal mode, 0600 file under the workspace dir
   (`FURL_CCR_SQLITE_PATH` override), startup + opportunistic expired-row
   purge, 10 000-row oldest-first cap (`FURL_CCR_SQLITE_MAX_ROWS`),
   corruption→fail-open-to-memory with one loud ERROR, lock-contention→bounded
   retry then per-op fail-open. It is the MCP server's DEFAULT backend and
   selectable anywhere via `FURL_CCR_BACKEND=sqlite`. On its own it delivers
   restart survival and cross-process retrieval for entries still inside the
   window, but durability alone doesn't widen the window — that gap is now
   closed by the **spill tier**: `FURL_CCR_SPILL=1` makes a fast in-memory
   primary demote (not delete) capacity/TTL-evicted entries into a durable
   `SqliteBackend` spill (`compression_store.py:1341` `_spill_evicted`, called
   from `_evict_if_needed` immediately before the primary `delete`;
   `:1357` `_recover_from_spill`, checked on a primary `retrieve()` miss). The
   spill's own row-cap/TTL bounds its lifecycle (no unbounded growth). A
   redundant-combo guard skips the spill when the primary is already
   `SqliteBackend` (`_create_spill_backend_from_env`, `:1733`). **Off by
   default** — single-tier remains the out-of-the-box behavior; opting in via
   `FURL_CCR_SPILL=1` is what buys demote-not-delete. Locked by
   `tests/test_ccr_spill_tier.py`.

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

**Status:** option #1 (durable backend as the eviction spill target) is
**shipped and opt-in** (`FURL_CCR_SPILL=1`) — the "bounded RAM *and*
genuinely-retained data" free lunch is available today for anyone who turns it
on; it just isn't the default. Session-scoped keying (#2) for spill cleanup,
reference-counted retention (#3), a thinner spill-to-disk LRU (#4), and simply
raising the cap (#5) remain **owner-deferred** — the spill's own row-cap/TTL is
the current lifecycle answer, not a session-scoped one. Default (spill
disabled) behavior is unchanged from the window + loud-miss guarantee
documented at the top of this file; opting into the spill additionally makes
that window durable across capacity/TTL eviction, not just across MCP
restarts.

## Cross-references
- `docs/audits/EVAL-break.md` — Cluster G original finding (row 6) + this reframe.
- `furl_ctx/ccr/mcp_server.py` — `_retrieve_content` (the live loud-miss surface).
- `archive/furl_ctx/ccr/response_handler.py` — `_execute_retrieval`; archived,
  no longer a live retrieval surface (kept for the historical loud-miss probe).
- `furl_ctx/cache/compression_store.py` — `format_retrieval_miss_detail`,
  `get_entry_status`, and the `CompressionStoreBackend` protocol. Two concrete
  backends ship: `InMemoryBackend` (`furl_ctx/cache/backends/memory.py`, the
  library default) and the durable `SqliteBackend`
  (`furl_ctx/cache/backends/sqlite.py`, the MCP server default — Engine P1-7;
  locked by `tests/test_sqlite_backend.py` + `tests/test_mcp_sqlite_default.py`).
- `furl_ctx/cache/compression_store.py` — the Q10 spill tier:
  `CCR_SPILL_ENV`/`_create_spill_backend_from_env` (`:1729-1757`),
  `_spill_evicted`/`_recover_from_spill` (`:1341`, `:1357`), wired into
  `_evict_if_needed` (`:1436-1442`) and `retrieve()` (`:614`). Locked by
  `tests/test_ccr_spill_tier.py`.
- `tests/test_ccr_eviction_loud_miss.py` — the locking regression tests.
- `furl_ctx/ccr/marker_grammar.py` is the single owner of the CCR marker grammar.
  It defines all marker shapes and both hash widths; LIBRARY.md has the
  reader-facing summary.
