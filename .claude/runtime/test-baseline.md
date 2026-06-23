# test-baseline.md — Phase 4 test-hardening baseline (HEAD 90b1df8a, 2026-06-23)

> Captured BEFORE any test change (test-quality skill step 2). Baseline ref = HEAD 90b1df8a (clean).
> Two-track: Python = test-quality skill + score.py (validated profile). Rust = by hand (no Rust profile in score.py) + optional cargo-mutants.

## Coverage floor (pytest --cov=headroom --cov-branch): TOTAL 54%
417 passed / 14 skipped. 9104 statements, 3812 missed; 3244 branches, 410 partial.
Coverage is the NON-REGRESSION FLOOR — do not drop below per-module. It is NOT the goal; quality axes are.

## score.py scorecard BASELINE (lang=python, validated) — files=31 tests=409 loc=9880
| axis | value | want | read |
|---|---:|---|---|
| A1_substring_match | 4 | ↓ | error-message-substring asserts |
| A2_private_symbol | **166** | ↓ | HEAVY private-symbol access — worst anti-fragility axis |
| A4_recomputed_crypto | 4 | ↓ | recomputed-expected (parallel-mutation blind) |
| A5_or_joined | 1 | ↓ | or-joined error asserts |
| C1_mock_real | 1 | ↓ | minimal real-mocking — GOOD |
| B1_fixed_vector | **0** | ↑ | ZERO tests pin a fixed expected literal — biggest mutation-resistance gap |
| D1_loc_per_test | 24.16 | ↓ | |
| D2_param_ratio | **0.017** | ↑ | almost NO parametrization |

**Diagnosis:** the suite is coverage-ish but MUTATION-WEAK (B1=0, D2≈0) and leans on private-symbol access (A2=166). The skill's job: drive B1 up, D2 up, A2 down, add boundary tests, find bugs — coverage held as floor.

## HIGH-VALUE TARGETS (LIVE engine/compress-path × low coverage — ranked by bug-finding value)
1. **kompress_compressor.py** — 13% cov, 586 stmts. LIVE default text/code compressor (the real hit-zone). Biggest untested surface.
2. **cache_aligner.py** — 16% cov. LIVE compress-path; guards the cache-prefix ordering invariant.
3. **smart_crusher.py** — 70% cov, 344 stmts / 82 missed. Core engine (TOIN loop, anchor, crush).
4. **content_router.py** — routes every compress() call; the dispatch logic.
5. **parser.py** — 42% cov, 221 stmts. Parsing = boundary-bug-prone.
6. **ccr/mcp_server.py** — 20% cov. The user's MCP-tool foundation (retrieve plane) — must be solid.
7. **cache/base.py CacheConfig** — 0% cov. LIVE config.
8. **CCR store** (compression_store.py) + csv_schema_decoder (the recovery decoder/TRAP) — recovery invariant surface (21 tests exist; harden internals + boundaries).

LOWER PRIORITY (env-gated/optional/observability — not compress-correctness): dynamic_detector (NER, spacy/torch-gated), ml_models, telemetry/* (toin 45% is the exception — SmartCrusher TOIN loop, worth it), html_extractor, tokenizers/{huggingface,mistral}.

## HARD INVARIANTS the tests must lock (priority — a bug here = a compression bug):
CCR recovery 100% byte-exact · Py↔Rust hash parity (compute_item_hash) · prompt-cache prefix ordering (never drop idx0 / reorder / rewrite cache_control) · default lossless decoder. The eval `break` pass already found silent-loss holes (multi-line CSV field, inter-call eviction) THIS way — harder tests find more.

## Gate per module (test-quality skill): full pytest stays green + recovery 21 + module coverage ≥ its floor + every new test mutation-sensitive (fails when behavior breaks). NEVER weaken a test to win an axis. NEVER chase coverage through privates. REPL-verify library assumptions.

## ★ COMPRESSION-QUALITY FLOOR (live run_bench @ HEAD 78f8cd97, model=gpt-4o) — fixes must be ≥ this, NEVER degrade
| dataset | items | lossless reduction | drop | retain | path |
|---|---|---|---|---|---|
| code@7 | 7 | 0.0% (passthrough) | 0.0% | 100% | lossless |
| logs@90 | 90 | 92.8% | 91.1% | 100% | LOSSY |
| search@90 | 90 | 92.2% | 85.6% | 100% | LOSSY |
| repeated_logs@90 | 90 | 96.5% | 100% | 100% | LOSSY |
| disk@9 | 9 | 50.0% | 0.0% | 100% | lossless |
| multiturn@135 | 135 | 70.6% | 49.6% | 100% | LOSSY |
| **NEEDLE-RECALL (output OR CCR)** | — | — | — | **100.0%** | — |

USER CONSTRAINT (2026-06-23): "implement fixes on all 25 bugs WITHOUT tradeoffs on compression results — each fix yields BETTER or SAME compression vs before." So the COMPRESSION-QUALITY GATE per fix = run `.venv/bin/python -m benchmarks.run_bench`, confirm per-dataset lossless reduction ≥ floor above AND needle-recall (output OR CCR) == 100.0%, THEN restore (`git checkout HEAD -- benchmarks/baseline_results.json benchmarks/BASELINE.md`). Lossy-loss bugs (#2/#24/#25) → fix by making the loss CCR-RECOVERABLE (preserve savings + 100% recall), NOT by compressing less. If a fix degrades savings or drops recall <100% → REVERT, find a recoverable approach, or defer+report.
