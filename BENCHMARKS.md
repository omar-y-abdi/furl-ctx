# BENCHMARKS — Headroom Engine honest benchmarks

## Phase-7: route-by-min-tokens — ship the fewer-token recoverable render (before → after)

- Baseline: Phase-6 final (`7e446de7`). Policy default flipped
  `LosslessFirst → MinTokens`.
- Frontier change: every prior phase, when a compressible array could
  render BOTH ways — **lossless** (all rows, CSV-schema-encoded,
  reconstructible by the decoder) and **lossy-recoverable** (a visible
  sample + a surfaced `<<ccr:HASH>>` sentinel, dropped rows in the CCR
  store) — shipped lossless whenever it cleared a BYTE-savings gate. But
  **both renders are 100% recoverable** (lossless shows everything; the
  CCR recovery invariant guarantees the lossy path), so choosing between
  them loses no information — it is a pure SIZE decision. And the size
  metric that matters is **tokens, not bytes** (a fewer-byte render can
  tokenize larger — hex vs base64). The lossless-first contract was
  shipping the byte-smaller render even when the lossy-recoverable render
  was far fewer TOKENS (`logs@90`: lossless **5274 tok** vs
  lossy **598 tok**, BOTH 100% recoverable).
- New policy `RoutingPolicy { MinTokens (default), LosslessFirst }`
  (`config.rs`). Under `MinTokens`, when both renders exist `crush_array`
  counts the TOKENS of each (real tiktoken via `crate::tokenizer`, model
  `gpt-4o` — the benchmark model) and ships the fewer; ties prefer
  lossless (more rows visible). Under `LosslessFirst` the legacy
  byte-ratio gate is kept (used by the lossless round-trip suite, which
  asserts the lossless rendering directly). The decision is in
  `crusher.rs::crush_array` (lossless candidate + lossy candidate built,
  then `render_token_count` compares the two final model-visible
  strings). The lossless encoder/decoder and the canonical CCR hash are
  UNCHANGED — only the CHOICE between two existing renders changed.

| dataset | tok before (P6, lossless-first) | tok after (P7, min-tokens) | Δ | path before → after | drop | retention | needle-recall |
|---|---:|---:|---:|---|---:|---:|---:|
| code@7 | 41025 | 41025 | 0 | lossless → lossless | 0% | 100% | — |
| logs@90 | 5274 | **598** | **−4676** | lossless → **LOSSY** | 91.1% | 100% | 100% (out\|CCR) |
| search@90 | 1803 | **300** | **−1503** | lossless → **LOSSY** | 85.6% | 100% | 100% (out\|CCR) |
| repeated_logs@90 | 240 | **105** | **−135** | lossless → **LOSSY** | 100% | 100% | — |
| disk@9 | 347 | 347 | 0 | lossless → lossless | 0% | 100% | — |
| multiturn@135 | 5674 | **4339** | **−1335** | lossless → **LOSSY** | 49.6% | 100% | — |

**Recovery proof (logs@90, the headline flip).** The output ships the
lossy survivor table whose final line is the sentinel
`<<ccr:d421d36becdd 82_rows_offloaded>>`. Parsing that hash from the
OUTPUT ALONE and calling `ccr_get(hash)` returns the 82 dropped
originals; combined with the 8 visible survivor rows, **all 90 distinct
input rows are recoverable** (0 lost). The chosen-lossy render loses no
information — it just costs 598 tokens instead of 5274.

**Honest read (Phase-7).**
- **No dataset grew.** Every dataset is ≤ its Phase-6 token count; the
  five compressible ones strictly improved. `code@7` (entropy floor) and
  `disk@9` (small array, only the lossless render is valid) are unchanged
  — `MinTokens` correctly leaves them lossless.
- **`repeated_logs` went 240 → 105, not "stayed 240".** Route-by-min-tokens
  found that dropping the highly-redundant ping rows to the CCR store
  (100% recoverable) is fewer tokens than encoding all 90 rows in a
  lossless table. That is a strict, lossless-information win the
  byte-ratio gate could not see.
- **Needle-recall stays 100% (output OR CCR) AND visible-recall stays
  100%** across `search` and `logs` × {30, 90, 300} × {start, middle,
  end}. The lossy renders the policy now prefers carry every dropped
  needle to the CCR store under a surfaced pointer — nothing the model
  could be asked for is unreachable.
- **What this is NOT.** It is not a new encoder, not a new drop strategy,
  not a hash change. It is purely picking the smaller of two renders that
  the engine already produced and already guaranteed recoverable. The
  lossless round-trip suite (`test_lossless_column_encodings`) is pinned
  to `LosslessFirst` so it keeps asserting the lossless rendering in
  isolation; the recovery invariant suite
  (`test_ccr_recovery_invariant`, 21 tests) is policy-agnostic and stays
  green because the chosen lossy render is always recoverable.

## Phase-6: cross-message dedup — conversation-level redundancy (before → after)

- Baseline commit: `986bbc37` (Phase-5 final). Final commit: `357dbad8`
  (multiturn dataset `e7d98416` → exact cross-message dedup `4357aecb` →
  near-duplicate tier + counterfactual gate `357dbad8`).
- Frontier change: the untapped lever was CROSS-MESSAGE. Every prior
  phase compressed per-array WITHIN one message; the same tool output
  repeated across turns (an agent re-running `rg`/`git log`/`df`) paid
  full price per copy. A new pipeline stage (`CrossMessageDeduper`,
  between CacheAligner and ContentRouter) removes that waste at the
  conversation level, pointer-backed:
  1. **Exact tier** — a later byte-identical tool output is replaced by a
     `{"_ccr_dropped": "... <<ccr:HASH N_bytes_duplicate>>"}` sentinel
     naming the message that still carries the bytes. Store-before-replace:
     the original is persisted in the CCR store under the surfaced hash
     (SHA-256[:24]) BEFORE the swap; a failed store write vetoes the
     replacement.
  2. **Near tier** — a later JSON dict-array sharing byte-identical rows
     with an earlier kept-verbatim array ships only its DIFFERING rows +
     the sentinel; the full original stays recoverable under the hash, and
     reference sources are always kept-verbatim units so every elided row
     remains visible somewhere in the conversation. Gated on the
     COUNTERFACTUAL (rewrite must come in under ~45% of original bytes —
     what per-message lossless compression achieves on row-shaped data),
     not on the raw original.

New REAL dataset `multiturn` (all six raw captures committed): the same
`rg --json --sort path` run twice (parsed rows byte-identical), `df -k`
run twice ~3s apart (4/9 rows genuinely drift), and `git log -n 30`
viewed one real commit apart (29/30 rows byte-identical — the canonical
post-commit history re-check). Scored conversation-level: an item is
retained when visible in ANY message or recoverable via a surfaced
pointer (same strictness ladder as the single-message harness, per
message).

| dataset | tok before | tok after (P5 → P6) | lossless reduction | drop | retention |
|---|---:|---:|---:|---:|---:|
| multiturn@135 (NEW) | 14866 | 9996 (per-msg only) → **5674** | 32.8% → **61.8%** | 0% | 100% |
| code@7 | 41025 | 41025 → 41025 | 0.0% (entropy floor) | 0% | 100% |
| logs@90 | 8595 | 5274 → 5274 | 38.6% (unchanged) | 0% | 100% |
| search@90 | 4102 | 1803 → 1803 | 56.0% (unchanged) | 0% | 100% |
| repeated_logs@90 | 3621 | 240 → 240 | 93.4% (unchanged) | 0% | 100% |
| disk@9 | 694 | 347 → 347 | 50.0% (unchanged) | 0% | 100% |

Within Phase-6 the multiturn movement decomposes (same data, measured):
per-message engine only 9996 (32.8%) → exact tier 8232 (44.6% — the
second rg table, ~1.8k tok, collapses to a ~50-tok pointer) → near tier
**5674** (61.8% — the second git-log view, ~2.6k tok as a table,
collapses to one changed row + pointer). Needle-recall (output OR CCR) stays **100.0%**, visible
**100.0%**; recovery of every elided payload is byte-exact through the
Python `compression_store` under the surfaced hash.

Prompt-cache contract (P0, pinned by 14 tests in
`tests/test_cross_message_dedup.py` through public `compress()`):
message count/order/roles never change; index 0 is never modified (not
even duplicate blocks inside it); the frozen prefix is never modified;
`cache_control`-bearing blocks pass through byte-faithful; only LATER
occurrences are rewritten, so the cached prefix stays byte-stable.
Scope: tool outputs only (role tool/function strings + `tool_result`
blocks); user/system/assistant and `is_error` outputs untouched.
Additive Python pipeline stage — Rust per-array outputs and parity
fixtures untouched (`cross_message_dedup_enabled=True` config gate).

**Honest read (Phase-6).**
- **The naive near-dup gate measurably REGRESSED before being fixed**:
  on the real drifted `df -k` pair (4/9 rows shared) shipping 5 raw JSON
  rows + sentinel cost ~430 tok where the router's lossless CSV table of
  all 9 rows costs 347 (multiturn 2654 → 2828 at the v1 dataset). The
  tier is therefore counterfactual-gated and now correctly REFUSES the
  df pair (pinned by test); low-overlap drift stays with per-message
  compression. Near-dup only wins on high-overlap pairs (the 29/30
  git-log case), and the benchmark shows both behaviors on real data.
- The raw `rg --json` streams differ between runs (elapsed-time stats
  events); the PARSED match rows are byte-identical only with `--sort
  path` (unsorted parallel traversal reorders rows run-to-run). The
  dataset uses the sorted form and documents it.
- **Tightening the lossy visible keep-set did NOT happen — measured no
  surface**: since Phase-5 flipped logs lossless, NO real dataset and NO
  needle trial takes the lossy path (0/18 trials; every case routes
  lossless with 0 drops), so CCR-backing currently-kept critical rows
  has nothing real to measure against and Phase-4 already established
  the survivor set as the principled floor. Deferred until a real
  capture exercises the lossy path again.
- `code@7` stays 0% — entropy floor; no fake gains.

## Phase-5: reversible column encodings — reconstruction-aware lossless (before → after)

- Baseline commit: `cc90e5f3` (Phase-4 final). Final commit: `717c0568`
  (reference decoder `ad7d2a5a` → arith fold `d3934ba1` → ISO delta
  `17a21efc` → dict encoding `970069d5` → decimal scale-fold `717c0568`).
- Frontier change: "lossless" is now **exact reconstruction through the
  documented decoder** (`headroom/transforms/csv_schema_decoder.py`),
  NOT verbatim string presence. Retention and needle-visibility on
  columnar renderings are measured by DECODE-AND-COMPARE (canonical-
  signature equality of decoded rows); verbatim substring scanning
  survives only as the fallback for non-decodable renderings.

Four additive encodings on the CSV-schema path (JSON / Markdown-KV
formatters byte-identical; every encoding stamps only after a stamp-time
proof that decode == original, plus a strict rendered-byte gate
simulated WITH ditto):

1. **Arithmetic fold** — a non-nullable int column that is an exact
   progression (`value_i == base + step*i`, checked i64 math) declares
   `name:int=BASE+STEP` and vanishes from the rows. repeated_logs'
   `icmp_seq` 0..89: 588 → 412 tok.
2. **ISO-8601 delta** — a strict-shape timestamp column
   (`YYYY-MM-DDTHH:MM:SS(Z|±HH:MM)`) declares `name:string~`, ships the
   first value verbatim and `{±delta_seconds}[/tz]` after; pure integer
   civil-calendar math both sides, spelling-preserving (`Z` stays `Z`).
   **logs@90 FLIPS LOSSY → LOSSLESS**: the render crosses the 0.30 gate.
3. **Dictionary encoding** — a low-cardinality string column ships a
   `__dict:name=v0,v1,...` line (every distinct value verbatim exactly
   once) + per-row indexes. Catches NON-consecutive repetition ditto
   can't see (git-log author/email, 36 distinct / 90 rows): 5459 → 5274
   tok on logs.
4. **Decimal scale-fold** — a plain-decimal float column declares
   `name:float%k` and ships integer×10^k cells (`0.053` → `53`); encode
   and decode are pure string manipulation (zero float arithmetic).
   repeated_logs' `time_ms`: 412 → 240 tok.

| dataset | tok before | tok after (P4 → P5) | lossless reduction | drop | retention |
|---|---:|---:|---:|---:|---:|
| code@7 | 41025 | 41025 → 41025 | 0.0% (entropy floor — unchanged) | 0% | 100% |
| logs@90 | 8595 | 665 (LOSSY, 91.1% drop) → **5274 LOSSLESS** | — → **38.6%** | 91.1% → **0%** | 100% → 100% |
| search@90 | 4102 | 1803 → 1803 | 56.0% (unchanged, see honest read) | 0% | 100% |
| repeated_logs@90 | 3621 | 588 → **240** | 83.8% → **93.4%** (arith + scale folds) | 0% | 100% |
| disk@9 | 694 | 347 → 347 | 50.0% (unchanged) | 0% | 100% |

Needle-recall: overall (output OR CCR) **100.0%**, visible-in-output
**100.0%** (both families, all positions/cardinalities — logs trials now
route lossless, keeping every needle visible). Reconstruction proof:
for every CSV-schema dataset, `decode(output) == original` as an ORDERED
list with exact values (logs 90/90, search 90/90, repeated_logs 90/90,
disk 9/9).

**Honest read (Phase-5).**
- `logs@90` is the regime change to be clear about: the visible output
  grew 665 → 5274 tokens because the engine's lossless-first contract
  now routes it lossless (38.6% ≥ the 0.30 gate). Nothing is dropped,
  nothing needs CCR recovery. The 91.1%-drop lossy rendering was
  smaller but removed 82 rows from view. Route-by-min-tokens remains
  rejected (Phase-4) — this phase widens the lossless regime, exactly
  as designed.
- **Delta encoding of `absolute_offset` / `line_number` (search) did
  NOT pan out — measured NEGATIVE** (-2B and -5B on the real rg
  capture): per-file offsets reset and deltas carry a mandatory sign,
  so deltas render no shorter than absolutes. Not implemented; search
  stays 56.0%. The remaining search bytes are the `lines` source-text
  column — genuine entropy.
- **Dictionary encoding on search's `path` column correctly refuses**:
  paths repeat in consecutive runs, which ditto already collapses to
  1 byte; a dictionary line would re-pay every path plus indexes
  (measured +42B). The gate said no.
- **Hex→base64 recoding of the 40-char commit hashes was evaluated and
  rejected**: it saves bytes (~13/row) but LOSES tokens — BPE
  tokenizes hex at ~2.5 chars/token vs ~1.3 for base64; the byte win
  inverts at the token layer. Same reasoning kills binary varint /
  bit-packing in a text channel.
- `logs` residual (38.6% vs theoretical): commit hashes (true entropy,
  see above) and 90 distinct subjects (true content). The date and
  email/author columns — the structured redundancy — are now encoded.
- `code@7` stays 0% — distinct source files at the entropy floor.
- The recovery-invariant lossy-survivor fixture gained microsecond
  timestamps (realistic; strict encoder honestly refuses fractional
  seconds) so it keeps pinning the lossy sentinel path; its assertions
  are unchanged. All 21 recovery-invariant tests pass; the decoder the
  contract uses is the same reference decoder the benchmarks use.

## Phase-4: CCR-backed removal maximization (before → after)

- Baseline commit: `957ef601` (Phase-3 final). Final commit: `9ad7b4c2`
  (singleton degeneracy gate `1c64f095` → CCR-backed budget + query
  pinning `5772533b` → survivor compaction `9ad7b4c2`).
- Frontier: maximize what the engine REMOVES from the visible output at
  CONSTANT 100% information retention and 100% needle-recall
  (output OR CCR). Every removed row carries a surfaced `<<ccr:HASH>>`
  pointer and is byte-recoverable from the store.

Three changes, all on the lossy dict-array path (lossless routes untouched):

1. **Singleton-pin degeneracy gate** — the 1B field-value-singleton pin is
   a *rarity* signal; on an all-distinct array EVERY row is a singleton and
   the capped pin loop degenerated to first-K-by-index (measured: pins were
   literally indices 0..14 on logs@90). Majority-singleton arrays now skip
   pinning. 2059 → 1332 tok, drop 74.4% → 83.3%.
2. **CCR-backed aggressive budget + query-relevant pinning** — with a CCR
   store configured every drop is recoverable (the invariant the
   adversarial loop locked), so the lossy keep budget halves
   (`adaptive_k/2`, floor 5; storeless/parity mode keeps full budget; the
   tier-1 passthrough boundary is unchanged). Counterweight shipped in the
   same change: rows matching the query (deterministic anchors, uncapped +
   high-confidence relevance ≥ max(2×0.3, 0.5), cap 3) are pinned like
   critical items in the over-budget path. 1332 → 729 tok, drop 83.3% →
   91.1%, and visible needle-recall rose to 100%.
3. **Survivor compaction** — the lossy keep-set ships as the existing
   lossless CSV-schema rendering (schema header, const-fold, ditto) with
   the `{"_ccr_dropped": ...}` sentinel as the final line, when ≥ 64 bytes
   smaller than the JSON-array form and no OpaqueRef. Pure rendering win:
   same rows, verbatim values, same drop ratio. 729 → 665 tok.

| dataset | tok before | tok after (P3 → P4) | drop | retention |
|---|---:|---:|---:|---:|
| logs@90 | 8595 | 2059 → **665** | 74.4% → **91.1%** | 100% → 100% |
| code@7 | 41025 | 41025 → 41025 | 0% | 100% |
| search@90 | 4102 | 1803 → 1803 | 0% | 100% |
| repeated_logs@90 | 3621 | 588 → 588 | 0% | 100% |
| disk@9 | 694 | 347 → 347 | 0% | 100% |

Needle-recall: overall (output OR CCR) stays **100.0%**; visible-in-output
rose **88.9% → 100.0%** (logs family 77.8% → 100.0%) — query-relevant
pinning keeps the query-named row visible at every position/cardinality in
both regimes, even while the generic budget halves.

**Honest read (Phase-4).**
- The lossy visible set is now at its principled floor: the 8 surviving
  logs@90 rows are all constraint-pinned (errors / structural outliers /
  numeric anomalies — the quality guarantee), plus schema + sentinel.
  Going lower means cutting the critical-row guarantee; not done.
- The harness learned the new survivor-table shape (JSON string whose
  final line is the sentinel object) with the same strictness as the
  array shape; recovery itself was verified through the real Python
  store/retrieve surface (full 90-row original under the surfaced hash)
  and locked by 4 new recovery-invariant tests (17 → 21).
- **Semantic near-dup collapse beyond the stable-projection hash did NOT
  pan out on this bench** — by construction: dup-heavy real data (ping,
  paths) routes LOSSLESS (ditto/const-fold beat the 0.30 gate), so the
  residual lossy cases are all-distinct arrays with nothing to collapse.
  The Imp2 stable-hash dedup + `_dup_count` already covers the lossy dup
  case.
- **Route-by-min-tokens (lossy beats lossless on tokens: search 1803 →
  ~250 est., repeated_logs 588 → ~100 est.) was evaluated and rejected**:
  it would flip lossless-routed arrays to lossy drops, breaking the
  pinned lossless round-trip suite (`test_lossless_column_encodings`) and
  the lossless-first design contract. Deferred as a product decision.
- `code@7` stays 0% — distinct source files at the entropy floor; no
  fake gains.

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
