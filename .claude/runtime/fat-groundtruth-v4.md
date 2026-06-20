# fat-groundtruth-v4.md — FRESH tool output against the POST-CUT tree (HEAD de3fd231)

> 4th-pass audit. The tree has had Tier-1 + Tier-2 cuts applied (~9,601 LOC removed). The
> prior audits (v1/v2/rust) ran against the PRE-CUT tree — their numbers are STALE. These
> are re-captured against the current tree. Branch verify/phase2-audit-report @ de3fd231.

## Current size (cloc, code only, excl archive/)
- **Rust 23,146 + Python 21,071 = 44,217 source LOC** (down from ~63,164 pre-cut).

## Tools came back NEARLY CLEAN (this reframes the audit)
- **cargo build -p headroom-core: 0 warnings, 0 dead_code, 0 `#[allow(dead_code)]` in tree.** The Rust core is compiler-clean post-cut. There are NO compiler-visible Rust orphans from the live_zone/pipeline removal. Residual Rust dead weight, if any, is REACHABILITY-only (pub items used solely by tests, or whole modules never called on the live compress() path) — invisible to cargo.
- **vulture headroom --min-confidence 70: 2 hits only** — `ccr/mcp_server.py:905 direct_mode`, `transforms/_telemetry_noop.py:47 attributes`. Line-level Python dead code is essentially gone.
- **ruff headroom --select F401,F811,F841,ERA001,RUF100: 34** — 29 RUF100 (unused-noqa, line-level nits), 5 ERA001 (commented-out — prior pass found 6/7 ERA hits were false-positive comment-headers; per-case check needed). Trivial.

## deptry DEP001 — optional/feature-gated imports (NOT "unused deps" — LEADS to vestigial FEATURES)
These are try/except-guarded optional imports. Each points at a feature module that may be vestigial (never invoked on the live compress() path). The LEAD: if the feature is dead, the module + its optional dep both go.
- `transforms/kompress_compressor.py` → torch, safetensors (heavy ML/learned compressor)
- `transforms/code_compressor.py` → tree_sitter (AST code compression)
- `component_tracker.py` → psutil (v1 scout flagged this "appears unreferenced")
- `models/ml_models.py` + `cache/dynamic_detector.py` → torch, spacy, sentence_transformers (NER/semantic tier — CONFIRMED LIVE in Tier-2 via dynamic_detector:603 get_spacy(), but verify it's reached on a NORMAL compress(), not just env-gated)
- `tokenizers/{base,mistral}.py` → PIL, mistral_common (tokenizer optional paths — likely intentional)
- `proxy/helpers.py` → zstandard, brotli (proxy — CONDEMNED, see exclusions)

## PRE-GREP REALITY CHECK (orchestrator did this — start from reality, don't re-discover)
- **kompress_compressor + code_compressor are ROUTED, not obviously vestigial.** content_router.py imports both and default-enables them (`enable_kompress=True`; Supported Compressors = CodeAwareCompressor/SearchCompressor/LogCompressor/KompressCompressor). So they are CONFIG-LIVE. The REAL question is RUNTIME reachability under a DEFAULT install: KompressCompressor is "ModernBERT/ML-based" (needs torch), CodeAwareCompressor needs tree_sitter — both optional deps. Does the path actually EXECUTE, or fall back to passthrough/another compressor when the optional dep is absent? If it always falls back (dep never installed in the real harness), the ML/AST path is effectively dead weight = vestigial-by-optional-dep. Trace config-live vs runtime-live. This is the subtle call, not a quick "no importer".
- **component_tracker.py is NOT unreferenced** (v1 scout's note was stale). Imported by compression_store.py + ccr/batch_store.py. Trace whether those are real runtime calls (telemetry/sizing) or dead imports.
- Net: the three deptry "prime suspects" mostly trace to importers/routing. Expectation for the headline: the tree is fairly LEAN; residual is subtle (optional-dep-gated compressor paths + the surface-deprecation tail), NOT big whole-feature deletes. Do not inflate.

## THE FRESH ANGLE (tools are clean, so look where tools can't)
Whole **vestigial FEATURE modules**: import cleanly (vulture/ruff pass) but are NEVER invoked on the live route. The live route = `compress()` (headroom/compress.py → TransformPipeline → CacheAligner → ContentRouter picks a compressor) + the real harness entries (the planned hook + ccr/mcp_server.py). A `transforms/*_compressor.py` that ContentRouter never routes to = dead weight. Trace reachability from those entries, all 4 coupling vectors.

## ALREADY CUT (Tier-1+2 — do NOT re-report as findable; they're in archive/)
Rust: transforms/pipeline/ subtree (13 files), safety.rs, live_zone.rs, recommendations.rs, lib.rs FFI compress_openai_responses_live_zone + their cargo tests. Python: conftest dead fixtures, ccr/batch_processor.py, compression_store.py:1097 comment, stale JSON results.

## CONFIRMED LIVE (do NOT flag — earned via gate loop / prior verify)
telemetry/ (SmartCrusher TOIN loop), onnx_runtime.py, wiki/, ml_models.py + dynamic_detector.py (NER/semantic call-sites), compression_feedback.py (CCR feedback loop), relevance/bm25.py + base.py (compression_store BM25Scorer), cache_control.rs, log/diff/search_compressor.rs, src/auth_mode.rs, src/compression_policy.rs, tokenizer*, SmartCrusher subsystem, ccr/ store core, ccr/mcp_server.py (load-bearing for the planned MCP retrieve plane).

## TIER-3 ALREADY-FLAGGED (surface-deprecation candidates — quantify, don't re-discover)
Public __all__/_LAZY_EXPORTS surface with ~0 runtime instantiation: relevance EmbeddingScorer/HybridScorer/create_scorer/embedding_available, cache optimizer registry (anthropic/openai/google CacheOptimizer, CacheOptimizerRegistry). Removable ONLY as a coordinated API deprecation (drop __all__/_LAZY_EXPORTS + docs + version bump), never a pure-move cut. The audit should QUANTIFY the LOC behind this surface and find MORE such surface, not re-litigate that they're "live by export."

## EXCLUSIONS (out of scope — don't spend agents here)
- **proxy/ (~3.2k) is CONDEMNED** — slated for full deletion in step 3 (the hook+MCP rebuild). Do NOT audit proxy-internal dead code; it's all going. (Exception: if something OUTSIDE proxy/ is dead-only-because-proxy-uses-it, that IS a finding — it becomes dead when proxy dies.)
- archive/, target/, .venv*, .sccache/, .claude/worktrees/, *.so, .git/.
- Complexity-lint (magic-values, C901, too-many-*) — NOT a cut (high-churn regression risk on a hardened, invariant-bound engine).

## HARD INVARIANTS (never flag for cut)
CCR recovery (100% byte-exact), prompt-cache ordering (idx0/prefix/cache_control), Py↔Rust hash parity, default lossless decoder.
