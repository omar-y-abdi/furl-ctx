# EVAL-SUMMARY — consolidated action doc (optimize · break · quality)

Baseline engine: commit `608fc7ac`. Method: 3 reusable workflows × ~50 sonnet agents each, isolated
worktrees, MEASURED before/after, loop-until-dry, anti-repeat ledger, opus synthesis per mode. ~150
agents, ~11M subagent tokens. Map-primed recon first. Two worst silent-loss defects independently
re-confirmed by the orchestrator (not trusted from agents).

## THE ONE CONCLUSION
The engine's real problem was never the compression ratio — it is **data integrity**. Across three
independent lenses (adversarial break, code-quality review, AND the earlier honest-verify), the same
class kept surfacing: **the engine silently loses data and busts contracts on common, realistic inputs.**
optimize confirmed compression is already near its practical ceiling (gains are marginal and some were
fixture coincidences). So the highest-value work is integrity fixes, not squeezing more percent.

---

## P0 — DATA-INTEGRITY / CONTRACT FIXES (do first; these break the core promise)

1. **Multi-line field → TOTAL silent loss on the lossless path.** [break Cluster A — orchestrator-confirmed: 0/90 rows recover]
   `csv_schema_decoder.py:325` splits on `\n` before parse, so any quoted cell containing a newline
   (log messages, stack traces, descriptions — extremely common) shatters every row. NO CCR backstop
   (lossless path emits no sentinel). FIX: RFC-4180 quote-aware line reader that tracks open-quote
   state across physical lines (incl. the header). Closes ~4 decoder defects at once.

2. **read_lifecycle store=None phantom hash.** [quality #1 — source-verified at read_lifecycle.py:485-497]
   Emits a `hash=...` retrieve-sentinel pointing to a hash stored NOWHERE when no CCR store is set.
   Literal Contract #1 break on the default path. FIX: don't emit a recovery pointer when nothing is persisted.

3. **CCR FIFO eviction → unbacked sentinels.** [break Cluster G — orchestrator-confirmed; quality independently fixed the eviction bugs]
   (a) Inter-call: enough calls overflow the in-memory cap (1000) and evict an earlier call's whole-blob
   while its `<<ccr:HASH>>` lives forever in that call's output → silent loss. (b) Eviction correctness
   bugs: tombstone accumulation (unbounded O(total_puts) memory) + ABA stale-token eviction of live
   re-inserted entries. FIX: generation-counter eviction (quality prototyped it) + never emit a sentinel
   the store can silently drop (or signal an unbacked pointer).

4. **Public compress() never freezes the Anthropic cached prefix.** [break Cluster C]
   Per-block guards (`content_router.py:2584/2640`) can't protect sibling/separate messages inside the
   cached prefix → SmartCrusher compresses them → block hash changes → prompt-cache MISS (Contract #2).
   FIX: message-level frozen-count, mirroring the Rust proxy's `compute_frozen_count`.

5. **furl_retrieve can't retrieve anything.** [break Cluster H + quality #2]
   SmartCrusher emits 12-char hashes; the retrieve tool validates exactly 24 → every public-API
   `<<ccr:HASH>>` the model sees is non-retrievable BY THE MODEL. FIX: accept the emitted hash width.

6. **Other silent key-drops without CCR.** [break Cluster F + quality #3 diff dangling-marker]
   Nullable null/empty collapse, object key-drop with no store write, diff cache_key not persisted.

## P1 — ROBUSTNESS / QUALITY (lock the class shut + clean wins) [quality]
- **Cross-language round-trip fuzz/property test (Rust compactor → Python decoder).** [break #2 / quality]
  Converts the whole lossless-corruption class from "patch each symptom" to "closed by construction."
  Highest long-term leverage. Additive test code, no behavior change.
- Eliminate the **double compaction run** on the hot path (`crush_array` calls `stage.run` twice). [quality] Perf, semantically identical.
- Eviction/capacity/TTL coherence; public-API input + immutability hardening; adaptive_sizer max_k parity.

## P2 — COMPRESSION GAINS (marginal; do last; only the safe ones) [optimize]
- **SAFE + generalizing:** hex-hash → base64url/base32 transcoding; percent/unit-suffix strip (inline marker). Small but real, low risk.
- **HIGH-REWARD / HIGH-RISK — gate behind out-of-sample needle test:** error-protection-gate bypass for
  homogeneous JSON arrays (+15.9pp) is **LOSSY on error-protected content** — it drops the very rows the
  gate exists to keep. Re-verify on fresh out-of-sample data with a needle test BEFORE implementing. Prefer
  the lossless-only variant (+9.6pp, all rows visible).
- **DO NOT BUILD:** `ifree=avail*10` cross-column ratio (+3.2pp) — fixture coincidence, not a real
  invariant (7 agents converged on the same coincidence). Breaks out-of-sample.

## DEAD ENDS (measured-failed; never re-try) [optimize]
BWT/MTF/RLE & prefix-delta regress at the TOKEN level · template-mining costs tokens · preamble
dictionaries rejected by the byte gate · `+` sign on numeric deltas is a separate BPE token ·
min-base/hex/octal integer re-encoding = zero/negative savings.

## RECOMMENDATION
Fix P0 (1→2→3 first — highest severity, all confirmed/source-verified), add the P1 cross-language fuzz
test to prevent regression of the entire class, then optionally the two SAFE P2 wins. Treat every "win"
as agent-measured on fixtures until re-verified out-of-sample at implementation time.
