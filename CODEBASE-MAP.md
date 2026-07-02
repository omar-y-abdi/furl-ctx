# FURL COMPRESSION ENGINE — NAVIGATION MAP

> **Verified 2026-07-02, post Great Excision — reflects the slimmed tree.** Furl is a
> standalone solo project (not a fork). The Anthropic proxy transport was removed earlier; the only
> live route is the Python `TransformPipeline` → Rust SmartCrusher (surfaced as a hook + MCP tool).
> The excision deleted whole subsystems since the prior refresh: the ML text compressor and its
> `[ml]` extra, HTML extraction and its `[html]` extra, the telemetry/compression-feedback plane,
> the HuggingFace/Mistral tokenizer backends (tokenizers are tiktoken + family-calibrated
> estimators only), the code compressor (large distinct code now takes the reversible CCR offload),
> the `RouterRuntime` per-request carrier, and the Rust regex content-detector mirror. Earlier
> sweeps made the CCR marker grammar single-owned: the canonical `compute_key`/`marker_for` in
> `ccr/mod.rs` (and the `blake3` dep) were DELETED; every Rust marker now flows through
> `ccr/markers.rs`, every Python consumer through `furl_ctx/ccr/marker_grammar.py`. `ContentRouter`
> had six clean seams extracted (`router_cache.py`, `router_split.py`, `router_policy.py` + the
> `CompressionStrategy` enum, plus `router_dispatch.py` `StrategyDispatcher` and
> `router_ccr_mirror.py` `CcrMirror`), but the orchestrator kept its responsibilities and sits at
> ~2400 LOC — the extraction relieved line-count, not coupling. It is still a god-object: a known,
> deliberately-deferred large refactor. Function-name anchors are authoritative; line numbers may
> drift ±~15 from later edits — if a line looks off, grep the `fn`/`def` name. The map orients;
> always trust the real code.

## 1. PIPELINE

End-to-end flow: `compress(messages,model)` (`furl_ctx/compress.py:342`) → `TransformPipeline.apply` (`furl_ctx/transforms/pipeline.py:189`, assembling CacheAligner → CrossMessageDeduper → ContentRouter at `pipeline.py:117/124/134`) → `ContentRouter.compress` (`furl_ctx/transforms/content_router.py:668`, the orchestrator entry) which detects content type via `_detect_content` (`content_router.py:270`) — Rust `detect_content_type` first (`content_router.py:298`, falling back to the Python regex detector at `:316`) — then routes pure vs mixed content through `_compress_mixed`/`_compress_pure` (`content_router.py:997/1104`) and per-strategy dispatch in `_apply_strategy_to_content` (`content_router.py:1153`), sending JSON-arrays to SmartCrusher across the PyO3 bridge. JSON goes to `SmartCrusher.crush_array` (`crates/furl-core/src/transforms/smart_crusher/crusher.rs:847`): tier-1 lossless compaction (`compaction/compactor.rs:131` → `formatter.rs:288`), tier-2 lossy row-drop planned by `planning.rs:create_plan` (`:100`) + `orchestration.rs:prioritize_indices` (`:185`), then `persist_dropped` (`crusher.rs:1373`) writes per-row chunks + whole-blob to the CCR store and emits the `<<ccr:HASH N_rows_offloaded>>` sentinel via `marker_for_rows_offloaded` (`crusher.rs:1499`). CCR storage lives behind the `CcrStore` trait (`crates/furl-core/src/ccr/mod.rs:38`); Python mirrors hashes into `CompressionStore` (`furl_ctx/cache/compression_store.py`) so `furl_retrieve` resolves them. Prompt-cache fidelity is held by `CacheAligner` (`cache_aligner.py:258`) on the Python side plus the frozen-prefix count `_compute_frozen_message_count` (`compress.py:167`) — the pure-Python owner of that logic (the orphaned Rust `cache_control.rs::compute_frozen_count` was deleted).

## 2. SUBSYSTEM MAP

**smart_crusher core (keep/drop + CCR emission)**
- `crusher.rs:847` — `crush_array` — dispatch lossless-vs-lossy, route by RoutingPolicy (MinTokens), return CrushArrayResult.
- `crusher.rs:1083` — `crush_array_lossy` — entropy-floor override, plan→execute→persist→optional survivor re-render.
- `crusher.rs:1373` — `persist_dropped` — per-row chunks + row-index FIRST, whole-blob LAST, emit `<<ccr:HASH N_rows_offloaded>>` + `<<ccr:HASH#rows N_chunks>>`.
- `crusher.rs:129` — `ccr_sentinel_map` — build `{_ccr_dropped, _ccr_rows?}` sentinel (recovery pointer unconditional on drop).
- `crusher.rs:1915` — `ccr_backed_keep_budget` — effective_max = adaptive_k/2, floor 5, cap adaptive_k.
- `orchestration.rs:185` — `prioritize_indices` — dedup→fill→union critical (errors+outliers+anomalies+query-pins+singletons)→novelty fill; may return >budget.

**planning + analyzer (strategy selection)**
- `planning.rs:100` — `create_plan` — dispatcher to plan_smart_sample/top_n/cluster_sample/time_series.
- `planning.rs:531` — `apply_query_signals` — deterministic anchors + high-relevance pins (never positionally dropped).
- `analyzer.rs:421` — `analyze_crushability` — 11-case decision tree; only `unique_entities_no_signal`/`medium_uniqueness_no_signal` eligible for entropy-floor override.
- `analyzer.rs:649` — `select_strategy` — crushability+pattern → Skip/TimeSeries/ClusterSample/TopN/SmartSample.

**compaction (lossless columnar)**
- `compaction/compactor.rs:131` — `compact` — array→IR (Table|Buckets|Untouched).
- `compaction/compactor.rs:187` — `build_homogeneous_table` — STRICT-ORDER stamps: constant→arith→iso→decimal→dict→head-dict→affix (round-trip proven at stamp time).
- `compaction/encodings.rs:29/202/285/401/460` — `parse_iso_strict`/`encode_iso_column`/`encode_decimal_cell`/`common_affix`/`split_head` — reversible primitives (pure string ops, no float math).
- `compaction/formatter.rs:288` — `write_table` — CSV-schema grammar `[N]{col:type,...}` + `__dict/__affix/__head:` preamble + ditto-marked rows.
- `compaction/formatter.rs:618` — `format_ccr_marker` — opaque-blob `<<ccr:HASH,KIND,SIZE>>`; now a thin shim that delegates to `markers.rs::marker_for_opaque` (`:619`).

**CCR marker grammar — single-owner (Rust produces, Python parses)**
- `ccr/markers.rs:36/43/53/60/68` — `marker_for_rows_offloaded`/`marker_for_row_index`/`marker_for_opaque`/`marker_for_diff`/`marker_for_retrieve_more` — the SINGLE construction point for every Rust marker. Owns the *grammar*, not the hash: producers compute their own key and pass `hash` in. Every Rust producer routes through here (crusher.rs:1467/1499, compaction/walker.rs:231, formatter.rs:619, diff_compressor.rs:478, log_compressor.rs:721, search_compressor.rs:305), pinned byte-for-byte by the in-module equivalence tests (`markers.rs:88-163`).
- `furl_ctx/ccr/marker_grammar.py:133/139/145` — `BRACKET_RETRIEVE_PATTERN`/`GENERIC_BRACKET_PATTERN`/`DOUBLE_ANGLE_PATTERN` + `marker_patterns()` (`:148`) — the SINGLE Python consumer spec. Accepted widths: 12 (sha256[:6], crusher rows) and 24 (md5[:24], diff/log/search). Imported by `furl_ctx/ccr/tool_injection.py:21` and the recovery walkers.

**CCR storage**
- `ccr/mod.rs:38` — `CcrStore` trait — put/get/len, Send+Sync. (The old canonical `compute_key`/`marker_for` and the `blake3` dep were DELETED; hashing now lives at each producer call site — see § hash parity.)
- One backend ships (`InMemoryCcrStore`); the dead SQLite/Redis `from_config`/`CcrBackendConfig` factory was deleted — recovery is request-window-scoped (`CCR-RETENTION.md`).
- `ccr/backends/in_memory.rs:173/250` — `put`/`get` — FIFO capacity eviction, lazy TTL via remove_if (TOCTOU-safe).

**other transforms + compaction stage**
- `log_compressor.rs:289` — `FormatDetector::detect` / `log_compressor.rs:365` — `LevelClassifier::classify` — AhoCorasick format detect + per-line log-level classifier.
- `diff_compressor.rs:850` — `score_hunks` — change-density + context-word + priority weights.
- `search_compressor.rs:332` — `parse_search_results` — byte-prefix parser (Windows drive + dash filenames).
- `compaction/mod.rs:121` — `CompactionStage::run` — array → (Compaction IR, rendered CSV-schema string); the lossless tier-1 entry. (The old `pipeline/{orchestrator,traits}.rs` `CompressionPipeline`/`OffloadTransform` were DELETED — no separate offload-pipeline trait survives; offloading is inlined in `crusher.rs::persist_dropped`.)
- `smart_crusher/traits.rs:73/119` — `Constraint`/`Observer` traits — the surviving extension points (keep/drop constraints + crush observers).

**routing / tokenizer / relevance**
- `tokenizer/registry.rs:55` — `get_tokenizer` — Tiktoken → Estimation dispatch (the HF tokenizer backend was excised; the estimator's chars-per-token density is calibrated per model family). Python mirror (`furl_ctx/tokenizers/registry.py:104`) dispatches tiktoken plus anthropic/google/cohere backends — all three are family-calibrated estimators (`registry.py:268/277/286`).
- `tokenizer/tiktoken_impl.rs:109` — `encoding_for` — o200k/cl100k/p50k/r50k by model prefix.
- `furl_ctx/compress.py:167` — `_compute_frozen_message_count` (Python) — only messages[].content `cache_control` blocks bump the floor; system/tools never. Pure-Python owner of frozen-prefix counting (the orphaned Rust `cache_control.rs::compute_frozen_count` was deleted).
- `relevance/bm25.rs:87` — `bm25_score` / `hybrid.rs:51` — `HybridScorer::score` — BM25 keyword scoring + the BM25-only boost (`boost_bm25_only`, `hybrid.rs:34`); the ML embedding tier was excised, so BM25 is the only scorer.
- `transforms/smart_crusher/config.rs:26` — `RoutingPolicy` — MinTokens (default, ties→lossless) vs LosslessFirst (legacy).

**ContentRouter extracted seams (6 clean seams lifted; orchestrator core stayed coupled — ~2400 LOC)**
- `furl_ctx/transforms/router_cache.py:30` — `CompressionCache` — per-content TTL+skip cache (get/put/mark_skip/invalidate) the router consults before recompressing.
- `furl_ctx/transforms/router_split.py:40/60` — `is_mixed_content`/`split_into_sections` — mixed-content section splitter (`ContentSection` + `_extract_json_block`).
- `furl_ctx/transforms/router_policy.py:26/40/64/78/92` — `CompressionStrategy` enum + `strategy_from_detection`/`strategy_from_detection_type`/`content_type_from_strategy`/`adaptive_min_ratio` — strategy mappings + the adaptive ratio, all re-exported from `content_router.py` (import at `content_router.py:72`).
- `furl_ctx/transforms/router_dispatch.py:42/63` — `StrategyDispatcher` (`apply`) — per-strategy compressor dispatch + the SMART_CRUSHER→LOG→passthrough no-savings fallback chain (TEXT resolves to passthrough — the ML text compressor was excised; the router's reversible CCR offload still catches large uncompressible content downstream). `ContentRouter._apply_strategy_to_content` (`content_router.py:1153`) is now a thin delegator that resolves the compressor-getters fresh on every call.
- `furl_ctx/transforms/router_ccr_mirror.py:47/59/137` — `CcrMirror` (`ensure_ccr_backed`/`extract_ccr_hashes`) — result-cache HIT re-mirror of `<<ccr:HASH>>` pointers back into the Python store + hash extraction. `ContentRouter._ensure_ccr_backed`/`_extract_ccr_hashes` (`content_router.py:1227/1246`) are thin delegators.

**public API**
- `furl_ctx/compress.py:342` — `compress` — one-liner entry; inflation guard reverts if tokens grow (`compress.py:500`).
- `furl_ctx/compress.py:92/135` — `CompressConfig`/`CompressResult` — config + metrics.
- `crates/furl-py/src/lib.rs:786/854` — `PySmartCrusher.crush`/`crush_array_json` — PyO3 bridge (GIL-released, validates at boundary).

## 3. CHANGE INDEX

- Add/modify a lossless column encoding → `compaction/compactor.rs:187` (build_homogeneous_table stamp order) + new `stamp_*` (`:395/:449/:490/:537/:611/:694/:832`) + `compaction/encodings.rs` encode/decode pair + `formatter.rs:288` render + `furl_ctx/transforms/csv_schema_decoder.py` Python decoder (byte-parity; `split_unquoted:259`, `_parse_iso:158`).
- Change keep/drop policy → `orchestration.rs:185` (prioritize_indices), `planning.rs` (plan_* signal sources, `create_plan:100`/`apply_query_signals:531`), `analyzer.rs:421` (crushability cases).
- Change CCR-backed keep budget → `crusher.rs:1915` (divisor/floor/cap), `crusher.rs:1108` (effective_max_items routing).
- Touch CCR offload / sentinel → `crusher.rs:1373` (persist_dropped, write order), `crusher.rs:129` (ccr_sentinel_map shape, build at `:133-140`), `crusher.rs:1467` (per-row chunk + `#rows` index marker via `marker_for_row_index`).
- Alter routing policy → `crusher.rs:856` (MinTokens match), `crusher.rs:1314` (render_token_count), `transforms/smart_crusher/config.rs:26` (RoutingPolicy enum).
- Change entropy-floor override → `crusher.rs:1092/1151` (CCR-backed crushability override gate: `allow_skip_override && skip_reason_is_no_signal`), `crusher.rs:1891` (no-signal eligibility doc).
- Change lossless thresholds → `transforms/smart_crusher/config.rs:198` (lossless_min_savings_ratio 0.30), `crusher.rs:1840/1876` (`SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES`=256, `LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES`=64).
- Change a CCR marker shape → `ccr/markers.rs:36/43/53/60/68` (the `marker_for_*` family — single Rust producer) + `furl_ctx/ccr/marker_grammar.py:133/139/145` (the consumer patterns) — keep the two in lockstep, pinned by `markers.rs:88-163` equivalence tests.
- Change a CCR hash → at the producer call site: `crusher.rs:2021` (`hash_canonical` = sha256[:6] → 12 hex, row + array keys) OR `md5_hex_24` (md5[:24] → 24 hex) in `diff_compressor.rs:1147`/`log_compressor.rs:1226`/`search_compressor.rs:657`. Python mirror key: `compression_store.py:317` (`store(..., explicit_hash=...)`). Accepted consumer widths {12,24}: `marker_grammar.py:74` (`HASH_WIDTHS`). (No central `compute_key` anymore — it was deleted with `blake3`.)
- Change content routing / per-type dispatch → `content_router.py:668` (ContentRouter.compress orchestrator), `content_router.py:1153` (`_apply_strategy_to_content`), `content_router.py:298/316` (Rust detect + regex fallback), `transforms/detection.rs` (Rust `detect` chain + `ContentType`; the regex `content_detector.rs` parity mirror was deleted).
- Change frozen-count / cache contract → `furl_ctx/compress.py:167` (`_compute_frozen_message_count` — the pure-Python owner; walks `messages[].content` for `cache_control` blocks and returns the exclusive floor).
- Add a test (Rust) → `crates/furl-core/tests/ccr_roundtrip.rs:36` (`default_crusher_stores_dropped_rows`) / `tokenizer_proptest.rs:19` (`deterministic_per_instance`).
- Add a test (Python) → `tests/test_ccr_recovery_invariant.py:124` (`_recover_from_output` harness) / `tests/test_ccr_proportional_retrieval.py:190` (`test_granular_retrieval_stays_positive`).
- Run a benchmark → `benchmarks/run_bench.py` (baseline) / `verify/run.py` (adversarial 6-seed sweep) / `verify/measure.py` (strict byte-exact cost model).

## 4. CONTRACT-ENFORCEMENT SITES

- **Recovery invariant (no data loss):** marker emission is UNCONDITIONAL on drop — `crusher.rs:1373` (persist_dropped writes store + emits marker regardless of `enable_ccr_marker`). Verified Rust: `tests/ccr_roundtrip.rs:161` (distinct_inputs_produce_distinct_store_entries), `:340` (nested_array_inside_object_gets_marker_injected); lossless-win-no-write at `ccr_roundtrip.rs:112`. Verified Python: `tests/test_ccr_recovery_invariant.py:221` (marker-off surfaces pointer), `:265` (opaque-blob recovers), `:369` (lossy survivor table), `:124` (`_recover_from_output` across Rust `ccr_get` + Python `py_store.retrieve`). Round-trip decoder: `csv_schema_decoder.py` (`split_unquoted:259`, `_parse_iso:158`) / `verify/independent_recheck.py` (strict, no substring fallback).
- **Proportional retrieval (granular chunks):** `crusher.rs:1467` (per-row chunk + `{hash}#rows` index via `marker_for_row_index`). Asserted positive across 0/25/50% retrieval (parametrized): `tests/test_ccr_proportional_retrieval.py:190` (`test_granular_retrieval_stays_positive`); the whole-blob (OLD) vs granular (NEW) cost branches are inline at `:198/:210`; real cost model in `verify/measure.py`.
- **Prompt-cache ordering / byte-fidelity:** `furl_ctx/compress.py:167` (`_compute_frozen_message_count` — only `messages[].content` `cache_control` blocks bump the frozen floor; system/tools always hot). Python prefix-stability is held by `CacheAligner.apply` (`cache_aligner.py:258`), which never reorders/rewrites the frozen prefix and compares against the caller-supplied `previous_prefix_hash` kwarg (read at `cache_aligner.py:338`, surfaced as `stable_prefix_hash` in the result metrics). Enforced by `tests/test_cache_aligner_prefix_hash.py`, `tests/test_cache_aligner_hardening.py`, `tests/test_compress_frozen_prefix.py`.
- **Python↔Rust hash parity (per-producer, no central key):** there is NO single `compute_key` anymore — each producer owns its hash and the grammar lives in `markers.rs`. SmartCrusher rows/array: `hash_canonical` = sha256[:6] → 12 hex (`crusher.rs:2021`); diff/log/search: `md5_hex_24` = md5[:24] → 24 hex (`diff_compressor.rs:1147` etc., byte-pinned to Python `hashlib.md5(...)[:24]` at `diff_compressor.rs:1215` `md5_24_matches_python`). Python mirrors via `compression_store.store(..., explicit_hash=hash)` (`compression_store.py:317`; `smart_crusher.py:824` `_mirror_single_hash_to_python_store`, `:603` `_mirror_ccr_to_python_store`) and `diff_compressor.py:133` `_persist_to_python_ccr`. (The old `headroom-parity` runner crate + `make test-parity` target were removed in the standalone excise, and the multi-backend `ccr_backends.rs` byte-equal-keys harness went with the SQLite/Redis backends — with a single in-memory backend, parity is pinned by the `markers.rs:88-163` equivalence tests plus the per-producer hash tests above.)
- **apply() kwargs allowlist (typo guard):** `ContentRouter.apply` (`content_router.py:1318`) rejects any kwarg not in the module-level `_APPLY_ALLOWED_KWARGS` frozenset (`content_router.py:543`), so a misspelled per-request option fails loud instead of being silently ignored. The allowlist is the union of keys `apply()` reads directly and keys the pipeline broadcasts to every transform. (The per-request ML-compressor options and their frozen `RouterRuntime` carrier were deleted with the ML text compressor — no thread-local, no per-call runtime threading survives.)

## 5. BUILD / BENCH CHEATSHEET

```bash
# Build the PyO3 extension (required for hard imports: SmartCrusher, detect_content_type)
python -m pip install -e .            # maturin backend
scripts/build_rust_extension.sh       # idempotent; needs active venv + cargo
make verify-rust-core                 # rebuild if smartcrusher suspected broken

# Rust tests
cargo test -p furl-core --lib smart_crusher
cargo test -p furl-core --test ccr_roundtrip -- --nocapture
cargo test --workspace                # all crates incl. integration tests

# Python tests
pytest tests/                                         # full suite
pytest tests/test_ccr_recovery_invariant.py           # recovery invariant
pytest tests/test_ccr_proportional_retrieval.py       # proportional retrieval
pytest -m "not real_llm and not live"                 # fast unit only

# Benchmark + restore baseline
.venv/bin/python -m benchmarks.run_bench              # baseline on committed snapshots -> baseline_results.json + BASELINE.md
.venv/bin/python -m verify.run                        # adversarial 6-seed sweep, cold CCR per subprocess -> verify/raw_results.json
.venv/bin/python -m benchmarks.run_bench --refresh    # RE-CAPTURE live snapshots (overwrites benchmarks/data/*.raw.json)
# Restore baseline: re-run WITHOUT --refresh (uses committed snapshots), or `git checkout HEAD -- benchmarks/data/` to revert refreshed snapshots
```

## 6. DELIBERATE DECISIONS (by-design; the trigger that would reopen each)

- **Two CCR stores, not one.** Rust `CcrStore` (`ccr/mod.rs:38`, InMemory default) is the COMPUTE-side write buffer: `crusher.rs::persist_dropped` writes here and `ccr_get` reads typed bytes back over the FFI. Python `CompressionStore` (`compression_store.py`) is the MODEL-FACING retrieval surface the MCP `furl_retrieve` reads (`mcp_server.py:330/448`) — it adds built-in BM25 `search(hash, query)` + retrieval-feedback tracking that the bare Rust KV `ccr_get` lacks, so routing retrieve straight at Rust would regress search/feedback. Both are in-memory single-tier (default 1000 entries / 300s TTL, no durable spill — recovery is request-window-scoped, and an evicted miss is loud via `format_retrieval_miss_detail`, never silent). REOPEN IF: a non-MCP reader needs recovery, or the Python store stops offering anything the Rust store can't — then the split no longer earns its keep.
- **CCR-emission knobs live in Rust config, pinned on the Python surface.** `min_compression_ratio_for_ccr` (default 0.8) and siblings are Rust config fields; the Python compressors pass the Rust default through and do NOT re-expose them as tunables (`diff_compressor.py:93`, the `min_compression_ratio_for_ccr` passthrough comment; uniform across diff/search/log). Capability ceiling by intent — no consumer needs per-call CCR-aggressiveness tuning and the default matches the value the retired Python original inlined. REOPEN IF: a real caller needs per-call control over the CCR-emission threshold — then promote the knob to the Python surface.
Notes: `cargo test` cannot run the `furl-py` cdylib (`test=false` in Cargo.toml) — Python-side tests only. The core is ML-free with no feature flags (`default = []`); the ML backends (magika/embeddings, ONNX `ort`) and the SQLite/Redis CCR backends were excised — relevance is BM25-only and the CCR store is in-memory-only (the dead `from_config`/`CcrBackendConfig` factory was deleted). Default model gpt-4o (real tiktoken); benchmarks use RoutingPolicy.MinTokens with CompressConfig defaults.