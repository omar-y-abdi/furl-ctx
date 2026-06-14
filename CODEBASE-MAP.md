# HEADROOM COMPRESSION ENGINE — NAVIGATION MAP

## 1. PIPELINE

End-to-end flow: `compress(messages,model)` (`headroom/compress.py:157`) → `TransformPipeline.apply` (`headroom/transforms/pipeline.py:204`: CacheAligner → CrossMessageDeduper → ContentRouter) → `ContentRouter.compress` (`headroom/transforms/content_router.py:952`) detects content type via Rust `detect_content_type` and routes JSON-arrays to SmartCrusher. On the proxy hot path, `compress_anthropic_live_zone[_with_ccr]` (`crates/headroom-core/src/transforms/live_zone.rs:633/658`) walks the body, respects `compute_frozen_count` (`cache_control.rs:109`), and dispatches live-zone blocks to per-type compressors. JSON goes to `SmartCrusher.crush_array` (`crates/headroom-core/src/transforms/smart_crusher/crusher.rs:716`): tier-1 lossless compaction (`compaction/compactor.rs:131` → `formatter.rs:257`), tier-2 lossy row-drop planned by `planning.rs` + `orchestration.rs:prioritize_indices`, then `persist_dropped` (`crusher.rs:1160`) writes per-row chunks + whole-blob to the CCR store and emits the `<<ccr:HASH>>` sentinel. CCR storage lives behind `CcrStore` (`crates/headroom-core/src/ccr/mod.rs:40`); Python mirrors hashes into `CompressionStore` (`headroom/cache/compression_store.py:184`) so `headroom_retrieve` resolves them.

## 2. SUBSYSTEM MAP

**smart_crusher core (keep/drop + CCR emission)**
- `crusher.rs:716` — `crush_array` — dispatch lossless-vs-lossy, route by RoutingPolicy (MinTokens), return CrushArrayResult.
- `crusher.rs:905` — `crush_array_lossy` — entropy-floor override, plan→execute→persist→optional survivor re-render.
- `crusher.rs:1160` — `persist_dropped` — per-row chunks + row-index FIRST, whole-blob LAST, emit `<<ccr:HASH N_rows_offloaded>>` + `<<ccr:HASH#rows N_chunks>>`.
- `crusher.rs:126` — `ccr_sentinel_map` — build `{_ccr_dropped, _ccr_rows?}` sentinel (recovery pointer unconditional on drop).
- `crusher.rs:1567` — `ccr_backed_keep_budget` — effective_max = adaptive_k/2, floor 5, cap adaptive_k.
- `orchestration.rs:158` — `prioritize_indices` — dedup→fill→union critical (errors+outliers+anomalies+query-pins+singletons)→novelty fill; may return >budget.

**planning + analyzer (strategy selection)**
- `planning.rs:98` — `create_plan` — dispatcher to plan_smart_sample/top_n/cluster_sample/time_series.
- `planning.rs:529` — `apply_query_signals` — deterministic anchors + high-relevance pins (never positionally dropped).
- `analyzer.rs:421` — `analyze_crushability` — 11-case decision tree; only `unique_entities_no_signal`/`medium_uniqueness_no_signal` eligible for entropy-floor override.
- `analyzer.rs:649` — `select_strategy` — crushability+pattern → Skip/TimeSeries/ClusterSample/TopN/SmartSample.

**compaction (lossless columnar)**
- `compaction/compactor.rs:131` — `compact` — array→IR (Table|Buckets|Untouched).
- `compaction/compactor.rs:175` — `build_homogeneous_table` — STRICT-ORDER stamps: constant→arith→iso→decimal→dict→head-dict→affix (round-trip proven at stamp time).
- `compaction/encodings.rs:29/202/285/401/460` — `parse_iso_strict`/`encode_iso_column`/`encode_decimal_cell`/`common_affix`/`split_head` — reversible primitives (pure string ops, no float math).
- `compaction/formatter.rs:257` — `write_table` — CSV-schema grammar `[N]{col:type,...}` + `__dict/__affix/__head:` preamble + ditto-marked rows.
- `compaction/formatter.rs:560` — `format_ccr_marker` — `<<ccr:HASH,KIND,SIZE>>` for opaque blobs.

**CCR storage**
- `ccr/mod.rs:40` — `CcrStore` trait — put/get/len, Send+Sync.
- `ccr/mod.rs:69` — `compute_key` — BLAKE3→24 lowercase hex (parity key, matches `[a-f0-9]{24}`).
- `ccr/mod.rs:81` — `marker_for` — `<<ccr:HASH>>` (fixed format).
- `ccr/backends/mod.rs:97` — `from_config` — InMemory/Sqlite/Redis, loud errors, no silent fallback.
- `ccr/backends/in_memory.rs:87/119` — `put`/`get` — FIFO capacity eviction, lazy TTL via remove_if (TOCTOU-safe).

**other transforms + pipeline**
- `log_compressor.rs:289` — `FormatDetector::detect` / `log_compressor.rs:330` — `classify_lines` — AhoCorasick format + per-language stack-trace state machine.
- `diff_compressor.rs:550` — `score_hunk` — change-density + context-word + priority weights.
- `search_compressor.rs:235` — `parse_search_results` — byte-prefix parser (Windows drive + dash filenames).
- `pipeline/orchestrator.rs:96` — `CompressionPipeline::run` — reformats‖bloat-estimation (rayon::join), then serial gated offloads.
- `pipeline/traits.rs:195` — `OffloadTransform` — `cache_key: String` required (CCR contract type-enforced).

**routing / tokenizer / relevance**
- `tokenizer/registry.rs:69` — `get_tokenizer` — HF-registry → Tiktoken → Estimation dispatch.
- `tokenizer/tiktoken_impl.rs:109` — `encoding_for` — o200k/cl100k/p50k/r50k by model prefix.
- `cache_control.rs:109` — `compute_frozen_count` — only messages[].content markers bump floor; system/tools never.
- `relevance/bm25.rs:87` — `bm25_score` / `hybrid.rs:182` — `compute_alpha` — keyword scoring + adaptive alpha.
- `config.rs:26` — `RoutingPolicy` — MinTokens (default, ties→lossless) vs LosslessFirst (legacy).

**public API**
- `headroom/compress.py:157` — `compress` — one-liner entry; inflation guard reverts if tokens grow.
- `headroom/compress.py:76/137` — `CompressConfig`/`CompressResult` — config + metrics.
- `crates/headroom-py/src/lib.rs:741/787` — `PySmartCrusher.crush`/`crush_array_json` — PyO3 bridge (GIL-released, validates at boundary).

## 3. CHANGE INDEX

- Add/modify a lossless column encoding → `compaction/compactor.rs:175` (stamp order) + new `stamp_*` (e.g. `:425/:466/:513/:587/:670/:808`) + `compaction/encodings.rs` encode/decode pair + `formatter.rs:257` render + `headroom/transforms/csv_schema_decoder.py:315` Python decoder (byte-parity).
- Change keep/drop policy → `orchestration.rs:158` (prioritize_indices), `planning.rs:182-511` (plan_* signal sources), `analyzer.rs:421` (crushability cases).
- Change CCR-backed keep budget → `crusher.rs:1567` (divisor/floor/cap), `crusher.rs:926-930` (effective_max routing).
- Touch CCR offload / sentinel → `crusher.rs:1160` (persist_dropped, write order), `crusher.rs:126` (sentinel shape), `crusher.rs:1211-1225` (per-row chunk + `#rows` index).
- Alter routing policy → `crusher.rs:864-879` (MinTokens match), `crusher.rs:1120` (render_token_count), `config.rs:26` (RoutingPolicy enum).
- Change entropy-floor override → `crusher.rs:968-983` (condition), `crusher.rs:1557` (skip_reason gate).
- Change lossless thresholds → `config.rs:205` (min_savings_ratio 0.30), `crusher.rs:1520/1537` (256 small-array, 64 survivor).
- Change CCR hash/marker → `ccr/mod.rs:69` (compute_key) + `ccr/mod.rs:81` (marker_for) + Python `compression_store.py:317` (explicit_hash) + tool-injection regex `[a-f0-9]{24}`.
- Change live-zone dispatch / content routing → `live_zone.rs:420` (dispatch_compressor), `live_zone.rs:837` (compress_one_block gates), `content_detector.rs:221` (detect_content_type).
- Change frozen-count / cache contract → `cache_control.rs:109` (compute_frozen_count), `cache_control.rs:143` (walk_messages).
- Add a test (Rust) → `crates/headroom-core/tests/ccr_roundtrip.rs:36` / `live_zone_dispatch.rs:84` / `tokenizer_proptest.rs:19`.
- Add a test (Python) → `tests/test_ccr_recovery_invariant.py:92` / `tests/test_ccr_proportional_retrieval.py:157`.
- Run a benchmark → `benchmarks/run_bench.py` (baseline) / `verify/run.py` (adversarial 6-seed sweep) / `verify/measure.py:279` (strict byte-exact).

## 4. CONTRACT-ENFORCEMENT SITES

- **Recovery invariant (no data loss):** marker emission is UNCONDITIONAL on drop — `crusher.rs:1160` (persist_dropped writes store + emits marker regardless of `enable_ccr_marker`). Verified Rust: `tests/ccr_roundtrip.rs:161` (distinct_inputs_produce_distinct_store_entries), `:263` (marker injection); lossless-win-no-write at `ccr_roundtrip.rs:112`. Verified Python: `tests/test_ccr_recovery_invariant.py:159/175/215` (marker-off + opaque-blob defects), `:92` (_recover_from_output across Rust `ccr_get` + Python `py_store.retrieve`). Round-trip decoder: `csv_schema_decoder.py:315` / `verify/independent_recheck.py:138` (strict, no substring fallback).
- **Proportional retrieval (granular chunks):** `crusher.rs:1211-1225` (per-row chunk + `{hash}#rows` index). Asserted positive at 25%/50% retrieval: `tests/test_ccr_proportional_retrieval.py:157`; whole-blob negative anchor at `:229`; real per-row cost model at `verify/measure.py:372`.
- **Prompt-cache ordering / byte-fidelity:** `cache_control.rs:109` (only messages[].content markers bump frozen_count; system/tools always hot); TTL ordering (1h before 5m) at `cache_control.rs:252` (warn-only). Byte-range surgery preserves bytes outside live zone: `live_zone.rs:1236` (apply_replacements). Enforced by `tests/cache_control.rs:20/39`, `tests/live_zone_dispatch.rs` (SHA-256 prefix/suffix), `tests/live_zone_thresholds.rs:52` (512B gate), `tests/live_zone_ccr.rs:86` (no_marker_when_store_omitted).
- **Python↔Rust canonical hash parity:** single source `ccr/mod.rs:69` (BLAKE3→24 lowercase hex). Python mirrors via `compression_store.store(..., explicit_hash=hash)` (`smart_crusher.py:825` _mirror_single_hash, `:690` _mirror_ccr_to_python_store) and `diff_compressor.py:129` _persist_to_python_ccr. Backend-swap byte-equal keys: `tests/ccr_backends.rs:113`. Parity fixtures: `config.rs:214-247` (defaults), `tests/parity/fixtures/`.

## 5. BUILD / BENCH CHEATSHEET

```bash
# Build the PyO3 extension (required for hard imports: SmartCrusher, detect_content_type)
python -m pip install -e .            # maturin backend
scripts/build_rust_extension.sh       # idempotent; needs active venv + cargo
make verify-rust-core                 # rebuild if smartcrusher suspected broken

# Rust tests
cargo test -p headroom-core --lib smart_crusher
cargo test -p headroom-core --test ccr_roundtrip -- --nocapture
cargo test --workspace                # all crates incl. integration tests
cargo test -p headroom-core --features redis,magika,embeddings

# Python tests
pytest tests/                                         # full suite
pytest tests/test_ccr_recovery_invariant.py           # recovery invariant
pytest tests/test_ccr_proportional_retrieval.py       # proportional retrieval
pytest -m "not real_llm and not live"                 # fast unit only
make test-parity                                       # maturin develop + parity fixtures

# Benchmark + restore baseline
.venv/bin/python -m benchmarks.run_bench              # baseline on committed snapshots -> baseline_results.json + BASELINE.md
.venv/bin/python -m verify.run                        # adversarial 6-seed sweep, cold CCR per subprocess -> verify/raw_results.json
.venv/bin/python -m benchmarks.run_bench --refresh    # RE-CAPTURE live snapshots (overwrites benchmarks/data/*.raw.json)
# Restore baseline: re-run WITHOUT --refresh (uses committed snapshots), or `git checkout HEAD -- benchmarks/data/` to revert refreshed snapshots
```
Notes: `cargo test` cannot run the `headroom-py` cdylib (`test=false` in Cargo.toml) — Python-side tests only. Feature flags: `magika` (Tier-1 ML detect), `embeddings` (EmbeddingScorer, else BM25-only), `redis` (else UnsupportedBackend). Default model gpt-4o (real tiktoken); benchmarks use RoutingPolicy.MinTokens with CompressConfig defaults.