# HEADROOM COMPRESSION ENGINE — NAVIGATION MAP

> **Verified 2026-06-24, post mass-repair — reflects the standalone tree.** Headroom is a
> standalone solo project (not a fork). The Anthropic proxy transport was removed earlier; the only
> live route is the Python `TransformPipeline` → Rust SmartCrusher (surfaced as a hook + MCP tool).
> Since the prior refresh the repo was dead-code-swept and the CCR marker grammar was made
> single-owned: the canonical `compute_key`/`marker_for` in `ccr/mod.rs` (and the `blake3` dep) were
> DELETED; every Rust marker now flows through `ccr/markers.rs`, every Python consumer through
> `headroom/ccr/marker_grammar.py`. `ContentRouter` had six clean seams extracted (`router_cache.py`,
> `router_split.py`, `router_policy.py` + the `CompressionStrategy` enum, plus `router_dispatch.py`
> `StrategyDispatcher` and `router_ccr_mirror.py` `CcrMirror`), but the orchestrator
> kept its responsibilities and now sits at ~2308 LOC — the extraction relieved
> line-count, not coupling. It is still a god-object: a known, deliberately-deferred large refactor. Function-name anchors are authoritative; line numbers may drift ±~15 from later edits — if a
> line looks off, grep the `fn`/`def` name. The map orients; always trust the real code.

## 1. PIPELINE

End-to-end flow: `compress(messages,model)` (`headroom/compress.py:191`) → `TransformPipeline.apply` (`headroom/transforms/pipeline.py:175`, assembling CacheAligner → CrossMessageDeduper → ContentRouter at `pipeline.py:102/109/119`) → `ContentRouter.compress` (`headroom/transforms/content_router.py:742`, the orchestrator entry) which detects content type via `_detect_content` (`content_router.py:191`) — Rust `detect_content_type` first (`content_router.py:219`, falling back to the regex detector at `:237`) — then routes pure vs mixed content through `_compress_mixed`/`_compress_pure` (`content_router.py:942/1020`) and per-strategy dispatch in `_apply_strategy_to_content` (`content_router.py:1063`), sending JSON-arrays to SmartCrusher across the PyO3 bridge. JSON goes to `SmartCrusher.crush_array` (`crates/headroom-core/src/transforms/smart_crusher/crusher.rs:695`): tier-1 lossless compaction (`compaction/compactor.rs:131` → `formatter.rs:258`), tier-2 lossy row-drop planned by `planning.rs:create_plan` (`:98`) + `orchestration.rs:prioritize_indices` (`:185`), then `persist_dropped` (`crusher.rs:1147`) writes per-row chunks + whole-blob to the CCR store and emits the `<<ccr:HASH N_rows_offloaded>>` sentinel via `marker_for_rows_offloaded` (`crusher.rs:1239`). CCR storage lives behind the `CcrStore` trait (`crates/headroom-core/src/ccr/mod.rs:38`); Python mirrors hashes into `CompressionStore` (`headroom/cache/compression_store.py`) so `headroom_retrieve` resolves them. Prompt-cache fidelity is held by `CacheAligner` (`cache_aligner.py:265`) on the Python side plus the frozen-prefix count `_compute_frozen_message_count` (`compress.py:163`) — the pure-Python owner of that logic (the orphaned Rust `cache_control.rs::compute_frozen_count` was deleted).

## 2. SUBSYSTEM MAP

**smart_crusher core (keep/drop + CCR emission)**
- `crusher.rs:695` — `crush_array` — dispatch lossless-vs-lossy, route by RoutingPolicy (MinTokens), return CrushArrayResult.
- `crusher.rs:892` — `crush_array_lossy` — entropy-floor override, plan→execute→persist→optional survivor re-render.
- `crusher.rs:1147` — `persist_dropped` — per-row chunks + row-index FIRST, whole-blob LAST, emit `<<ccr:HASH N_rows_offloaded>>` + `<<ccr:HASH#rows N_chunks>>`.
- `crusher.rs:126` — `ccr_sentinel_map` — build `{_ccr_dropped, _ccr_rows?}` sentinel (recovery pointer unconditional on drop).
- `crusher.rs:1554` — `ccr_backed_keep_budget` — effective_max = adaptive_k/2, floor 5, cap adaptive_k.
- `orchestration.rs:185` — `prioritize_indices` — dedup→fill→union critical (errors+outliers+anomalies+query-pins+singletons)→novelty fill; may return >budget.

**planning + analyzer (strategy selection)**
- `planning.rs:98` — `create_plan` — dispatcher to plan_smart_sample/top_n/cluster_sample/time_series.
- `planning.rs:529` — `apply_query_signals` — deterministic anchors + high-relevance pins (never positionally dropped).
- `analyzer.rs:419` — `analyze_crushability` — 11-case decision tree; only `unique_entities_no_signal`/`medium_uniqueness_no_signal` eligible for entropy-floor override.
- `analyzer.rs:647` — `select_strategy` — crushability+pattern → Skip/TimeSeries/ClusterSample/TopN/SmartSample.

**compaction (lossless columnar)**
- `compaction/compactor.rs:131` — `compact` — array→IR (Table|Buckets|Untouched).
- `compaction/compactor.rs:175` — `build_homogeneous_table` — STRICT-ORDER stamps: constant→arith→iso→decimal→dict→head-dict→affix (round-trip proven at stamp time).
- `compaction/encodings.rs:29/202/285/401/460` — `parse_iso_strict`/`encode_iso_column`/`encode_decimal_cell`/`common_affix`/`split_head` — reversible primitives (pure string ops, no float math).
- `compaction/formatter.rs:258` — `write_table` — CSV-schema grammar `[N]{col:type,...}` + `__dict/__affix/__head:` preamble + ditto-marked rows.
- `compaction/formatter.rs:561` — `format_ccr_marker` — opaque-blob `<<ccr:HASH,KIND,SIZE>>`; now a thin shim that delegates to `markers.rs::marker_for_opaque` (`:562`).

**CCR marker grammar — single-owner (Rust produces, Python parses)**
- `ccr/markers.rs:36/43/53/60/68` — `marker_for_rows_offloaded`/`marker_for_row_index`/`marker_for_opaque`/`marker_for_diff`/`marker_for_retrieve_more` — the SINGLE construction point for every Rust marker. Owns the *grammar*, not the hash: producers compute their own key and pass `hash` in. Every Rust producer routes through here (crusher.rs:1212/1239, walker.rs:187, formatter.rs:562, diff_compressor.rs:478, log_compressor.rs:664, search_compressor.rs:305), pinned byte-for-byte by the in-module equivalence tests (`markers.rs:102-160`).
- `headroom/ccr/marker_grammar.py:114/120/126` — `BRACKET_RETRIEVE_PATTERN`/`GENERIC_BRACKET_PATTERN`/`DOUBLE_ANGLE_PATTERN` + `marker_patterns()` (`:131`) — the SINGLE Python consumer spec. Accepted widths: 12 (sha256[:6], crusher rows) and 24 (md5[:24], diff/log/search). Imported by `headroom/ccr/tool_injection.py:21` and the recovery walkers.

**CCR storage**
- `ccr/mod.rs:38` — `CcrStore` trait — put/get/len, Send+Sync. (The old canonical `compute_key`/`marker_for` and the `blake3` dep were DELETED; hashing now lives at each producer call site — see § hash parity.)
- One backend ships (`InMemoryCcrStore`); the dead SQLite/Redis `from_config`/`CcrBackendConfig` factory was deleted — recovery is request-window-scoped (`CCR-RETENTION.md`).
- `ccr/backends/in_memory.rs:168/245` — `put`/`get` — FIFO capacity eviction, lazy TTL via remove_if (TOCTOU-safe).

**other transforms + compaction stage**
- `log_compressor.rs:289` — `FormatDetector::detect` / `log_compressor.rs:365` — `LevelClassifier::classify` — AhoCorasick format detect + per-line log-level classifier.
- `diff_compressor.rs:844` — `score_hunks` — change-density + context-word + priority weights.
- `search_compressor.rs:332` — `parse_search_results` — byte-prefix parser (Windows drive + dash filenames).
- `compaction/mod.rs:120` — `CompactionStage::run` — array → (Compaction IR, rendered CSV-schema string); the lossless tier-1 entry. (The old `pipeline/{orchestrator,traits}.rs` `CompressionPipeline`/`OffloadTransform` were DELETED — no separate offload-pipeline trait survives; offloading is inlined in `crusher.rs::persist_dropped`.)
- `smart_crusher/traits.rs:73/119` — `Constraint`/`Observer` traits — the surviving extension points (keep/drop constraints + crush observers).

**routing / tokenizer / relevance**
- `tokenizer/registry.rs:69` — `get_tokenizer` — HF-registry → Tiktoken → Estimation dispatch.
- `tokenizer/tiktoken_impl.rs:109` — `encoding_for` — o200k/cl100k/p50k/r50k by model prefix.
- `headroom/compress.py:163` — `_compute_frozen_message_count` (Python) — only messages[].content `cache_control` blocks bump the floor; system/tools never. Pure-Python owner of frozen-prefix counting (the orphaned Rust `cache_control.rs::compute_frozen_count` was deleted).
- `relevance/bm25.rs:87` — `bm25_score` / `hybrid.rs:182` — `compute_alpha` — keyword scoring + adaptive alpha.
- `transforms/smart_crusher/config.rs:26` — `RoutingPolicy` — MinTokens (default, ties→lossless) vs LosslessFirst (legacy).

**ContentRouter extracted seams (6 clean seams lifted; orchestrator core stayed coupled — now ~2308 LOC)**
- `headroom/transforms/router_cache.py:18` — `CompressionCache` — per-content TTL+skip cache (get/put/mark_skip/invalidate) the router consults before recompressing.
- `headroom/transforms/router_split.py:40/60` — `is_mixed_content`/`split_into_sections` — mixed-content section splitter (`ContentSection` + `_extract_json_block`).
- `headroom/transforms/router_policy.py:26/41/71/85/101` — `CompressionStrategy` enum + `strategy_from_detection`/`strategy_from_detection_type`/`content_type_from_strategy`/`adaptive_min_ratio` — strategy mappings + the adaptive ratio, all re-exported from `content_router.py` (import at `content_router.py:63`).
- `headroom/transforms/router_dispatch.py:43/65` — `StrategyDispatcher` (`apply`) — per-strategy compressor dispatch + the SMART_CRUSHER→KOMPRESS→LOG→passthrough no-savings fallback chain. `ContentRouter._apply_strategy_to_content` (`content_router.py:1063`) is now a thin delegator that resolves the compressor-getters fresh and binds `_try_kompress`/`_record_to_toin` (with per-call `runtime`) into closures passed in per call.
- `headroom/transforms/router_ccr_mirror.py:47/59/137` — `CcrMirror` (`ensure_ccr_backed`/`extract_ccr_hashes`) — result-cache HIT re-mirror of `<<ccr:HASH>>` pointers back into the Python store + hash extraction. `ContentRouter._ensure_ccr_backed`/`_extract_ccr_hashes` (`content_router.py:1230/1248`) are thin delegators.

**public API**
- `headroom/compress.py:191` — `compress` — one-liner entry; inflation guard reverts if tokens grow (`compress.py:306`).
- `headroom/compress.py:76/137` — `CompressConfig`/`CompressResult` — config + metrics.
- `crates/headroom-py/src/lib.rs:738/784` — `PySmartCrusher.crush`/`crush_array_json` — PyO3 bridge (GIL-released, validates at boundary).

## 3. CHANGE INDEX

- Add/modify a lossless column encoding → `compaction/compactor.rs:175` (build_homogeneous_table stamp order) + new `stamp_*` (e.g. `:425/:466/:513/:587/:670/:808`) + `compaction/encodings.rs` encode/decode pair + `formatter.rs:258` render + `headroom/transforms/csv_schema_decoder.py` Python decoder (byte-parity; `split_unquoted:216`, `_parse_iso:115`).
- Change keep/drop policy → `orchestration.rs:185` (prioritize_indices), `planning.rs` (plan_* signal sources, `create_plan:98`/`apply_query_signals:529`), `analyzer.rs:419` (crushability cases).
- Change CCR-backed keep budget → `crusher.rs:1554` (divisor/floor/cap), `crusher.rs:913` (effective_max_items routing).
- Touch CCR offload / sentinel → `crusher.rs:1147` (persist_dropped, write order), `crusher.rs:126` (ccr_sentinel_map shape, build at `:128-135`), `crusher.rs:1212` (per-row chunk + `#rows` index marker via `marker_for_row_index`).
- Alter routing policy → `crusher.rs:854` (MinTokens match), `crusher.rs:1107` (render_token_count), `transforms/smart_crusher/config.rs:26` (RoutingPolicy enum).
- Change entropy-floor override → `crusher.rs:920/956` (CCR-backed crushability override gate: `allow_skip_override && skip_reason_is_no_signal`), `crusher.rs:1539` (no-signal eligibility doc).
- Change lossless thresholds → `transforms/smart_crusher/config.rs:205` (lossless_min_savings_ratio 0.30), `crusher.rs:1507/1524` (`SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES`=256, `LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES`=64).
- Change a CCR marker shape → `ccr/markers.rs:36/43/53/60/68` (the `marker_for_*` family — single Rust producer) + `headroom/ccr/marker_grammar.py:114/120/126` (the consumer patterns) — keep the two in lockstep, pinned by `markers.rs:102-160` equivalence tests.
- Change a CCR hash → at the producer call site: `crusher.rs:1607` (`hash_canonical` = sha256[:6] → 12 hex, row + array keys) OR `md5_hex_24` (md5[:24] → 24 hex) in `diff_compressor.rs:1141`/`log_compressor.rs:1143`/`search_compressor.rs:633`. Python mirror key: `compression_store.py:314` (`store(..., explicit_hash=...)`). Accepted consumer widths {12,24}: `marker_grammar.py:65-69`. (No central `compute_key` anymore — it was deleted with `blake3`.)
- Change content routing / per-type dispatch → `content_router.py:742` (ContentRouter.compress orchestrator), `content_router.py:1063` (`_apply_strategy_to_content`), `content_router.py:219/237` (Rust detect + regex fallback), `content_detector.rs:221` (detect_content_type).
- Change frozen-count / cache contract → `headroom/compress.py:163` (`_compute_frozen_message_count` — the pure-Python owner; walks `messages[].content` for `cache_control` blocks and returns the exclusive floor).
- Add a test (Rust) → `crates/headroom-core/tests/ccr_roundtrip.rs:36` (`default_crusher_stores_dropped_rows`) / `tokenizer_proptest.rs:19` (`deterministic_per_instance`).
- Add a test (Python) → `tests/test_ccr_recovery_invariant.py:124` (`_recover_from_output` harness) / `tests/test_ccr_proportional_retrieval.py:157` (`test_granular_retrieval_stays_positive`).
- Run a benchmark → `benchmarks/run_bench.py` (baseline) / `verify/run.py` (adversarial 6-seed sweep) / `verify/measure.py` (strict byte-exact cost model).

## 4. CONTRACT-ENFORCEMENT SITES

- **Recovery invariant (no data loss):** marker emission is UNCONDITIONAL on drop — `crusher.rs:1147` (persist_dropped writes store + emits marker regardless of `enable_ccr_marker`). Verified Rust: `tests/ccr_roundtrip.rs:161` (distinct_inputs_produce_distinct_store_entries), `:295` (nested_array_inside_object_gets_marker_injected); lossless-win-no-write at `ccr_roundtrip.rs:112`. Verified Python: `tests/test_ccr_recovery_invariant.py:221` (marker-off surfaces pointer), `:265` (opaque-blob recovers), `:342` (lossy survivor table), `:124` (`_recover_from_output` across Rust `ccr_get` + Python `py_store.retrieve`). Round-trip decoder: `csv_schema_decoder.py` (`split_unquoted:216`, `_parse_iso:115`) / `verify/independent_recheck.py` (strict, no substring fallback).
- **Proportional retrieval (granular chunks):** `crusher.rs:1212` (per-row chunk + `{hash}#rows` index via `marker_for_row_index`). Asserted positive across 0/25/50% retrieval (parametrized): `tests/test_ccr_proportional_retrieval.py:157` (`test_granular_retrieval_stays_positive`); the whole-blob (OLD) vs granular (NEW) cost branches are inline at `:165/:173`; real cost model in `verify/measure.py`.
- **Prompt-cache ordering / byte-fidelity:** `headroom/compress.py:163` (`_compute_frozen_message_count` — only `messages[].content` `cache_control` blocks bump the frozen floor; system/tools always hot). Python prefix-stability is held by `CacheAligner.apply` (`cache_aligner.py:251`), which never reorders/rewrites the frozen prefix and tracks `_previous_prefix_hash` (`cache_aligner.py:229`). Enforced by `tests/test_cache_aligner_prefix_hash.py`, `tests/test_cache_aligner_hardening.py`, `tests/test_compress_frozen_prefix.py`.
- **Python↔Rust hash parity (per-producer, no central key):** there is NO single `compute_key` anymore — each producer owns its hash and the grammar lives in `markers.rs`. SmartCrusher rows/array: `hash_canonical` = sha256[:6] → 12 hex (`crusher.rs:1607`); diff/log/search: `md5_hex_24` = md5[:24] → 24 hex (`diff_compressor.rs:1141` etc., byte-pinned to Python `hashlib.md5(...)[:24]` at `diff_compressor.rs:1209`). Python mirrors via `compression_store.store(..., explicit_hash=hash)` (`compression_store.py:314`; `smart_crusher.py:817` `_mirror_single_hash_to_python_store`, `:690` `_mirror_ccr_to_python_store`) and `diff_compressor.py:129` `_persist_to_python_ccr`. Backend-swap byte-equal keys: `tests/ccr_backends.rs:116` (`backend_swap_byte_equal_keys`) — the surviving cross-backend byte-equal-keys check. (The old `headroom-parity` runner crate + `make test-parity` target / `FIXTURES` var were removed in the standalone excise; no separate parity harness exists in the current tree.)
- **apply() kwargs allowlist (typo guard):** `ContentRouter.apply` (`content_router.py:1420`) rejects any kwarg not in the module-level `_APPLY_ALLOWED_KWARGS` frozenset (`content_router.py:518`), so a misspelled per-request option fails loud instead of being silently ignored. Per-request options (`target_ratio`/`force_kompress`/`kompress_model`) are read off kwargs into the frozen `RouterRuntime` value via `RouterRuntime.from_kwargs` (`content_router.py:114`) and threaded by argument down compress/dispatcher/`_try_kompress`/`_record_to_toin` — no thread-local, no main→worker replay (workers receive the same immutable value by argument).

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

- **Two CCR stores, not one.** Rust `CcrStore` (`ccr/mod.rs:38`, InMemory default) is the COMPUTE-side write buffer: `crusher.rs::persist_dropped` writes here and `ccr_get` reads typed bytes back over the FFI. Python `CompressionStore` (`compression_store.py`) is the MODEL-FACING retrieval surface the MCP `headroom_retrieve` reads (`mcp_server.py:330/362`) — it adds built-in BM25 `search(hash, query)` + retrieval-feedback tracking that the bare Rust KV `ccr_get` lacks, so routing retrieve straight at Rust would regress search/feedback. Both are in-memory single-tier (default 1000 entries / 300s TTL, no durable spill — recovery is request-window-scoped, and an evicted miss is loud via `format_retrieval_miss_detail`, never silent). REOPEN IF: a non-MCP reader needs recovery, or the Python store stops offering anything the Rust store can't — then the split no longer earns its keep.
- **CCR-emission knobs live in Rust config, pinned on the Python surface.** `min_compression_ratio_for_ccr` (default 0.8) and siblings are Rust config fields; the Python compressors pass the Rust default through and do NOT re-expose them as tunables (`diff_compressor.py:93`, the "Rust-only knob" comment; uniform across diff/search/log). Capability ceiling by intent — no consumer needs per-call CCR-aggressiveness tuning and the default matches the value the retired Python original inlined. REOPEN IF: a real caller needs per-call control over the CCR-emission threshold — then promote the knob to the Python surface.
Notes: `cargo test` cannot run the `headroom-py` cdylib (`test=false` in Cargo.toml) — Python-side tests only. The core is ML-free with no feature flags (`default = []`); the ML backends (magika/embeddings, ONNX `ort`) and the SQLite/Redis CCR backends were excised — relevance is BM25-only and the CCR store is in-memory-only (the dead `from_config`/`CcrBackendConfig` factory was deleted). Default model gpt-4o (real tiktoken); benchmarks use RoutingPolicy.MinTokens with CompressConfig defaults.