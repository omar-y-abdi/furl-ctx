# BENCHMARKS — Headroom Engine honest benchmarks

## Phase-3: lossless column encodings (before → after)

- Baseline commit: `8e005090` (Phase-2 final). Final commit: `590c9c02`
  (constant-column fold `f04ad614` → ditto marks `1d5ff57b` → small-array
  lossless `590c9c02`).
- Token model: `gpt-4o` (real tiktoken BPE). All data real and committed
  under `benchmarks/data/`; re-run with `.venv/bin/python -m benchmarks.run_bench`.

Three additive, zero-loss encodings in the CSV-schema lossless rendering:

1. **Constant-column fold** — a column with the identical scalar in every
   row declares `name:type=value` once in the `[N]{...}` header and is
   omitted from rows. Null/empty constants never fold (ambiguous empty cell).
2. **Ditto marks** — a cell identical to the same column's previous-row cell
   renders as bare `=` (carry-forward); a literal `=` data cell is CSV-quoted;
   0–1-char cells never ditto.
3. **Small-array lossless routing** — arrays at/below `adaptive_k` (the common
   case for tool output) now attempt the lossless stage, gated by: no
   `OpaqueRef` substitution anywhere, ≥256 absolute bytes saved, and the
   existing 0.30 ratio gate. Passthrough stays the default for tiny /
   opaque-bearing / low-saving arrays.

Every encoding keeps every distinct value verbatim in the output and every
row reconstructible from the output alone — the CCR-recovery contract
decoder (`tests/test_ccr_recovery_invariant.py`) learned both encodings and
`tests/test_lossless_column_encodings.py` pins the round-trips through
public `compress()`.

| dataset | tok before | tok after (P2 → P3) | lossless reduction | drop | retention |
|---|---:|---:|---:|---:|---:|
| code@7 | 41025 | 41025 → 41025 | 0.0% → 0.0% (entropy floor; opaque gate keeps passthrough) | 0% | 100% |
| logs@90 | 8595 | 2059 → 2059 | LOSSY, unchanged (see honest read) | 74.4% | 100% |
| search@90 | 4102 | 2462 → **1803** | 40.0% → **56.0%** (ditto on path runs) | 0% | 100% |
| repeated_logs@90 | 3621 | 1662 → **588** | 54.1% → **83.8%** (const fold + ditto) | 0% | 100% |
| disk@9 (NEW, real `df -k`) | 694 | passthrough → **347** | 0% → **50.0%** (small-array routing) | 0% | 100% |

Needle-recall: overall (output OR CCR) stays **100.0%**; visible-in-output
recall rose **72.2% → 88.9%** (logs family 44.4% → 77.8%) because the smaller
lossless rendering now crosses the 0.30 gate for more log-array cardinalities
— those trials flipped lossy → lossless, keeping the needle visible.

**Honest read (Phase-3).**
- `logs@90` stays LOSSY: with ditto the lossless rendering reaches 26.97%
  byte savings — still under the 0.30 gate. The remaining row content
  (40-hex commit hash, ISO date, 90 distinct subjects) is genuine entropy.
  Dictionary-encoding the author/email columns was measured at ≈ +2.6pp
  (36 distinct authors over 90 rows — too high-cardinality) and would NOT
  flip the route; not implemented.
- `code@7` remains 0% — large distinct source files at the entropy floor;
  the small-array opaque gate intentionally refuses to substitute file
  contents with CCR pointers.
- Delta/range encoding of monotone numeric columns (icmp_seq 0..89,
  absolute_offset) would shrink further but reconstructed values are no
  longer verbatim in the output; deferred pending a reconstruction-aware
  retention metric.

---

# Phase-2 honest benchmark (before → after)

- Baseline commit: `0795e63e` (pre-1A / pre-Imp2 / pre-1B) — from `benchmarks/baseline_results.json`.
- Final commit: `031d4bc6` (1A unconditional CCR persist → Imp2 field-aware stable
  hash → 1B novelty fill + singleton pinning → non-dict persist `72dcc0ff` →
  CCR recovery invariant `031d4bc6`).
- Token model: `gpt-4o` (real tiktoken BPE via the engine's tokenizer registry).
- All numbers come from REAL captured local data and the engine's own `compress()`
  output. No synthetic low-entropy rows; no hand-written benchmark numbers.

Re-run (deterministic, off the committed snapshots under `benchmarks/data/`):

    .venv/bin/python -m benchmarks.run_bench     # suite + needle-recall
    .venv/bin/python -m benchmarks.run_final     # final suite + Imp2 A/B

## Methodology

### Datasets (all real, snapshotted under `benchmarks/data/`)

| dataset | rows | capture command | shape |
|---|---:|---|---|
| `code` | 7 | read of this repo's own source files | large distinct source files |
| `logs` | 90 | `git log --pretty=format:'%H<US>%an<US>%ae<US>%aI<US>%s' -n 300` | varying hash+ISO date; **all-distinct subjects** |
| `search` | 90 | `rg --json 'def ' headroom/` | distinct path/line/offset match objects |
| `repeated_logs` | 90 | `ping -c 100 -i 0.01 127.0.0.1` | **recurring content `{bytes,from,ttl}` + monotone `icmp_seq` identity counter** |

`repeated_logs` is the canonical Improvement-2 case: every row's value-bearing
content is byte-identical; only the monotone `icmp_seq` counter and the real
`time_ms` latency vary. `icmp_seq` is exactly the VaryingIdentity column that
makes every whole-item hash unique and that the field-aware stable hash excludes.
Two other real sources were tested and **rejected as Imp2 demos** (instructively):
`rg --json` results have genuinely distinct `path` per row (content, not identity →
no collapse), and macOS `install.log` timestamps sit below the engine's 0.9
identity gate. Both are documented so the benchmark is honest in both directions.

### Token counting & determinism
Counts go through the engine's own tokenizer at `gpt-4o` (real BPE, not `len()/4`).
Every run reads committed raw snapshots → reproducible; `--refresh` re-captures.

### The three metrics (reported separately, never blended)
1. **Lossless token reduction** — token savings ratio (a true zero-loss number only when drop ratio = 0; on the lossy path savings partly come from deletion, so the row is flagged LOSSY).
2. **Lossy drop ratio** — fraction of distinct input rows not visible in the output.
3. **Information retention** — fraction of distinct rows present in the output OR recoverable from CCR via a `<<ccr:HASH>>` pointer **surfaced in the output**.

## Before → after (baseline `0795e63e` → final `031d4bc6`)

| dataset | tok before | tok after (base → final) | lossless reduction | lossy drop ratio | information retention | path |
|---|---:|---:|---:|---:|---:|---|
| code@7 | 41025 | 41025 → 41025 | 0.0% → 0.0% | 0.0% → 0.0% | 100% → 100% | lossless (passthrough) |
| logs@90 | 8595 | 1332 → 2059 | 84.5% → 76.0% | 83.3% → **74.4%** | 100% → 100% | LOSSY |
| search@90 | 4102 | 2462 → 2462 | 40.0% → 40.0% | 0.0% → 0.0% | 100% → 100% | lossless |
| repeated_logs@90 | 3621 | — → 1662 | — → 54.1% | — → 0.0% | — → 100% | lossless |

Needle-recall (known unique row injected start/middle/end into 30/90/300-row real arrays):

| metric | baseline | final |
|---|---:|---:|
| overall recall (output OR CCR) | 100.0% | **100.0%** |
| overall visible-in-output recall | 72.2% | 72.2% |
| logs family, visible-only | 44.4% | 44.4% |
| search family, visible-only | 100.0% | 100.0% |

The headline movement is **lossy drop 83.3% → 74.4%** (8 more distinct rows kept
visible) and the inflated "savings" falling 84.5% → 76.0% (less of the reduction
is deletion). The "100% recall" is now *provable*, not flag-dependent (see below).

## Improvement-2 field-aware dedup — A/B (repeated_logs, real ping replies)

No runtime toggle exists for the field-aware hash (`compute_exclude_set` runs
unconditionally), so the A/B holds the data fixed and compares the engine's two
documented hash primitives, validated against the engine's parity contract
(empty exclude ⇒ `stable_item_hash` == `compute_item_hash`):

| metric | value |
|---|---|
| rows | 90 |
| identity exclude-set (engine-derived) | `('icmp_seq',)` |
| Imp2 OFF — whole-item hash families | **90** (no collapse: every row unique) |
| Imp2 ON — field-aware hash families | **58** |
| redundancy recovered | **35.6%** |
| engine real max `_dup_count` (lossy `top_n` path) | **3** |
| limiting case (also exclude volatile `time_ms`) | **90 → 1** |

Honest headline **90 → 58**, not 90 → 1: `icmp_seq` is the only true identity
column; the real per-reply `time_ms` latency genuinely varies and is correctly
kept as content (58 distinct latency-families remain).

## Adversarial recoverability verification

The core "same answers" claim — *every distinct item the engine drops or
substitutes is recoverable by a consumer holding only the output* — was tested by
an adversarial refute loop (`headroom-recoverability-refute` workflow: parallel
skeptics each trying to construct a silent-loss counterexample, refute-by-default).
It ran to convergence (loop-until-dry):

| Round | Finding | Resolution |
|---|---|---|
| 1 (find) | **5/5 refuted** — sub-step 1A's persist covered only the dict-array path; `crush_string_array`/`crush_number_array`/`crush_mixed_array` dropped items with no store write + no marker. 964/1000 strings unrecoverable via the **public** `compress()` API, default config. | `72dcc0ff` — extracted a shared `persist_dropped`/`ccr_dropped_sentinel`; wired it into all non-dict branches. |
| 2 (re-verify) | **1/4 refuted** — with `ccr_inject_marker=False` the store write happened but the `<<ccr:HASH>>` pointer was never appended, so the key was unreachable; **and** the lossless:table opaque-blob substitution never persisted originals (50/50 blobs lost). | `031d4bc6` — recovery pointer made **unconditional** on every drop (decoupled from the retrieval-tool flag); CompactionStage now persists opaque-blob originals. |
| 3 (re-verify) | **0 reachable refutations** — all flag combinations recover 100%. | Loop dry. |

**Final state (independently re-probed at `031d4bc6`):** across
`(enabled, marker) ∈ {(T,T),(F,T),(F,F)}` and array shapes string/number/mixed/dict
+ opaque-blob, a 1000-distinct-item input loses **0** items — every dropped item is
recovered via a `<<ccr:HASH>>` pointer **surfaced in the output**, through both the
Rust `ccr_get` and the Python `compression_store.retrieve` surfaces. Locked by 17
regression tests in `tests/test_ccr_recovery_invariant.py` + 4 Rust tests.

Out-of-scope (documented, not a public-API loss): direct lower-level `crush()` of a
single large *object* drops key:value pairs without persist — but a large object
routes to `router:noop` through public `compress()` (all keys survive verbatim), so
it is unreachable via the public API.

## Honest read

**What improved.**
- **The "same answers" guarantee is now real on the public `compress()` path.** Before, the audited needle-loss reproduced (964/1000 distinct items silently lost on non-dict arrays; key never surfaced under marker-off; opaque blobs never persisted). After the adversarial loop, **0 silent loss** under every reachable config — every dropped item carries a surfaced recovery pointer and is byte-recoverable. Information retention is provably 100%, not flag-dependent.
- `logs@90` lossy drop fell **83.3% → 74.4%** (1A + 1B keep more distinct rows visible).
- Imp2's field-aware dedup is real and measured: **90 → 58** hash-families on genuinely repeated content with a monotone identity counter, with real `_dup_count` stamped on the lossy path.

**What did NOT materialize, and why (stated plainly).**
- **Imp2 is a no-op on all-distinct data** (git-log unique subjects; rg-search distinct paths) — correct behavior: the field-aware hash equals the whole-item hash when the "varying" field is real content, not identity. The 82–95% headline only applies to genuinely repeated structured logs.
- **Visible needle-recall stayed 44.4% on the all-distinct logs harness.** 1B changed which needles survive, not the count: one distinct needle competing against 90 equally-distinct rows for a fixed 15-row budget is position-churned, not pinned. Deterministically pinning it needs a query/relevance signal — out of scope for Phase 2. It does **not** violate "same answers": the dropped needle is always CCR-recoverable (100% overall recall).
- **Honest lossless savings on genuinely high-entropy real data sit in the 0–54% band**, not 60–95%: code 0% (large distinct files don't crush), search 40%, repeated_logs 54%. The eye-catching 76% log figure is still part-deletion (the rows are recoverable, but the *token* win there is not free).
