# Headroom Compression Engine — Next-Action Doc (Synthesis of 48 Measured Experiments)

## Summary — read this first

**No experiment in the 48-attempt ledger moved the compression ratio. Not one.**

The committed baseline (`benchmarks/baseline_results.json`, commit `0795e63`) — which I confirmed matches both the task's stated baseline and the current HEAD reality — is:

| dataset | lossless reduction | lossy drop | path |
|---|---|---|---|
| code@7 | **0.0%** | 0.0% | lossless (no opportunity) |
| logs@90 | **84.5%** | 83.3% rows dropped (CCR-recoverable) | LOSSY |
| search@90 | **40.0%** | 0.0% | lossless, no drop |
| needle-recall | 100% output-or-CCR / **72.2% visible-only** | — | — |

### The "92.8% / 92.2%" numbers are an artifact, not a gain

~30 ledger entries cite `logs@90 92.8%` / `search@90 92.2%` as **both their before AND their after**. Those worktrees ran `benchmarks.run_bench` against **re-captured live snapshots** (`--refresh`), i.e. a different, easier-compressing dataset. `before == after` at 92.8% proves only that the change was *neutral on that data* — it says nothing about a gain over the committed baseline.

Two entries (`stamp-arith-visible-o1-recount`, `log-compressor-single-pass-level-counts`) report a real-looking `84.5% → 92.8%` jump. **Both are false positives:**
- I verified in baseline source that `log_compressor.rs::format_output` (lines 917-920) only writes **summary stats** (error/warn/info counts) — it never touches line keep/drop. Code that cannot move a keep/drop decision cannot move the compression ratio.
- Both entries **self-contradict**, asserting "delta=0, unchanged" in prose while showing the jump in numbers.

The jump is the refreshed snapshot bleeding into their "after" measurement. **Do not chase 92.8%.**

### So what DID the fleet find?

Real value, on a **different axis**: *recoverability (Contract #1)* and *production robustness*. `needle-recall` only measures **direct-store** retrieval against the benchmark datasets — it misses the LLM-facing recovery path and the `store=None` path that Headroom's actual agentic workload uses. Three genuine Contract-#1 / recovery defects were caught and **verified against baseline source**. Those are the wins worth shipping.

If the goal is *more compression*, the honest answer is: the fleet exhausted polish, refactor, and coverage without moving the needle. The only untouched territory with compression upside is the **keep/drop policy itself** (`orchestration.rs:158` prioritize_indices, `planning.rs`, `analyzer.rs:421` crushability tree) — but no measured evidence exists for any change there, so it is forward guidance, not a ranked item.

## Top recommendation

**Ship the read-lifecycle `store=None` phantom-hash fix first.** Verified in baseline source: `headroom/transforms/read_lifecycle.py:485` computes `ccr_hash = sha256(content)[:24]` and emits `Retrieve original: hash={ccr_hash}]` when no CCR store is configured — a sentinel pointing to a hash **stored nowhere**. Sentinel present + original unretrievable = a literal Contract #1 break. ReadLifecycle is **default-enabled** and the public `compress()` path passes **no** store, so this is the default real-world path for agentic Read-tool traffic — exactly the path `needle-recall` (direct-store, benchmark-only) never exercises, which is why it slipped through.

## Ranked actions (rank = product value × confidence ÷ effort; every compression delta = 0)

| # | Title | Approach | Evidence (verified) | Value | Effort | Risk |
|---|---|---|---|---|---|---|
| 1 | read-lifecycle store=None phantom-hash | `read-lifecycle-coverage-and-contract1-fix` | read_lifecycle.py:485-497 emits unrecoverable sentinel; default-enabled; public path store=None | Closes Contract #1 hole on the default path | Low (~3 LOC + tests) | Low |
| 2 | Inject `headroom_retrieve` for `<<ccr:HASH>>` | `tool-injection-coverage-and-scanner-fix` | tool_injection.py:209-215 patterns match ONLY bracket form; no `<<ccr:>>` → tool never injected for SmartCrusher | Restores LLM recovery channel for primary compressor | Low (1 regex + tests) | Low-Med |
| 3 | Persist cache_key in `compress_with_stats` | `diff-compressor-ccr-persist-fix-and-coverage` | diff_compressor.py:154-175 returns cache_key but never persists; compress() does | Closes Contract #1 gap on diff sidecar API | Very low (3 LOC) | Very low |
| 4 | CCR store robustness (eviction/capacity/len-TTL) | `ccr-inmemory-generation-eviction-fix` (+sqlite-capacity, len-ttl, throttled-purge) | All delta=0, benchmark byte-identical, recovery 21/21; fixes unbounded growth + stale counts | Long-running proxy hardening | Medium (4 diffs + ~15 tests) | Low |
| 5 | Public-API hardening (frozen + validation + aliasing) | `compress-api-eager-input-validation` (+freeze, +list-aliasing, +routing-at-correct-layer) | All delta=0, recovery 21/21; eager errors on 50-vs-0.5, list mutation, bad policy | Removes silent public footguns | Low-Med (~68 tests) | Low |
| 6 | adaptive_sizer max_k fix (Py+Rust parity) | `adaptive-sizer-coverage-and-bounds-fix` | `if n<=8: return n` ignores max_k (demo returned 5 for max_k=2); fix both ports | Removes latent contract violation under custom configs | Very low (1 expr ×2) | Low |
| 7 | Hot-path allocation refactors (UNMEASURED CPU) | `render-result-string-cow-and-tests` (+serializer, persist-strings, orch-hash, info-score) | All delta=0, byte-parity tested; CPU win real by construction but **never measured** | Micro-perf + code quality only | Medium (parity proof) | Medium |

## Dead ends — do not re-try

- **Treating logs 92.8% / search 92.2% as a gain** — snapshot-refresh artifact. The committed baseline is 84.5% / 40.0%. Any future agent that reports a jump to 92.8% has almost certainly refreshed the snapshot; re-run `benchmarks.run_bench` **without** `--refresh` (or `git checkout HEAD -- benchmarks/data/`) before trusting any delta.
- **`stamp-arith-visible-o1-recount` and `log-compressor-single-pass-level-counts` as compression wins** — both report 84.5%→92.8% but change only perf/stats code that provably cannot alter keep/drop. Real (perf) but mislabeled; the ratio jump is the artifact above.
- **`ccr-retrieval-domain-types-45`** — self-rejected after an exhaustive sweep: the proposed CCRToolResult `error_cause` enum has no consuming callsite. "Representation without use," unmeasurable, against Simplicity-First. Honest rejection; do not revive.
- **Pure error-type swaps with no test/behavior change** (`approach_id: 29`, dead `ConfigurationError`/`ProviderError` wiring at delta=0) — low value; only pursue if a concrete caller will branch on the new type.
- **Competing read-lifecycle variant** `read-lifecycle-immutable-dataclass-coverage` — it emits an "Original not stored. Fingerprint:" marker, which STILL substitutes/drops from visible output. Superseded by rank #1 (which preserves the original verbatim).
- **Python-only adaptive_sizer fix** `adaptive-sizer-coverage-and-max-k-fix` — superseded by the parity variant (#6); diverging Python from Rust would risk Contract #3.
- **Stale routing-policy validation entries** — `#8` never landed; `numeric-range-validation-config (#16)` targets the DEAD `headroom.config.SmartCrusherConfig` (not used by the engine). Use only `routing-policy-eager-validation-at-correct-layer` (live transforms classes).
- **Running two refactors on the same function** — e.g. both render_result_string entries touch the same code; pick one.

## Methodology note

All compression claims here are **measured** against the committed baseline `benchmarks/baseline_results.json` (commit `0795e63`, gpt-4o tiktoken), independently re-confirmed against the task statement and the current HEAD reality (84.5% / 40.0% / 0.0%). Every Tier-1 defect (ranks 1-3) was **verified against baseline source code**, not taken on the ledger's word: read_lifecycle.py:485-497, tool_injection.py:209-215, diff_compressor.py:154-175. The benchmark uses committed snapshots (`benchmarks/data/*.raw.json`); the `--refresh` flag re-captures live data and is the root cause of the 92.8% artifact. **Tiering of compression reality:** logs@90 84.5% is a *lossy* tier (83.3% of rows dropped to CCR, recoverable via the per-row granular model — retrieval cost is already priced into `verify/measure.py:372`); search@90 40.0% and code@7 0.0% are *lossless* tiers with no drop. None of these tiers improved. Out-of-sample integrity (Contract #4) holds: the recommended fixes are code-correctness, not fixture-tuned. A confirmatory `benchmarks.run_bench` at HEAD would be definitive but is not required — the committed artifact and task statement already agree.