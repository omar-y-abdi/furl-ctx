# FURL COMPRESSION ENGINE — NAVIGATION MAP

> **Verified 2026-07-03, post Great Excision + Phase-8 crusher split.** Furl is a
> standalone solo project (not a fork). The Anthropic proxy transport was removed earlier; the only
> live route is the Python `TransformPipeline` → Rust SmartCrusher (surfaced as a hook + MCP tool).
> The excision deleted whole subsystems since the prior refresh: the ML text compressor and its
> `[ml]` extra, HTML extraction and its `[html]` extra, the telemetry/compression-feedback plane,
> the HuggingFace/Mistral tokenizer backends (tokenizers are tiktoken + family-calibrated
> estimators only), the code compressor (large distinct code now takes the reversible CCR offload),
> the `RouterRuntime` per-request carrier, and the Rust regex content-detector mirror. Earlier
> sweeps made the CCR marker grammar single-owned: the canonical `compute_key`/`marker_for` in
> `ccr/mod.rs` (and the `blake3` dep) were DELETED; every Rust marker now flows through
> `ccr/markers.rs`, every Python consumer through `furl_ctx/ccr/marker_grammar.py`. Phase 8 split
> the monolithic `crusher.rs` into `smart_crusher/{walk,route,persist}.rs` — the old monolith is
> gone; every anchor below reflects that split. `ContentRouter` had six clean seams extracted
> (`router_cache.py`, `router_split.py`, `router_policy.py` + the `CompressionStrategy` enum, plus
> `router_dispatch.py` `StrategyDispatcher` and `router_ccr_mirror.py` `CcrMirror`), and Phase 8
> added four more files: `router_engine.py`, `router_blocks.py`, `router_message_policy.py`,
> `router_debug.py`. `signals/tiered.rs` was DELETED (SIMP-5b). Function-name anchors are
> authoritative; line numbers may drift ±~15 from later edits — if a line looks off, grep the
> `fn`/`def` name. The map orients; always trust the real code.

## 1. PIPELINE

End-to-end flow: `compress(messages,model)` (`furl_ctx/compress.py:397`) → `TransformPipeline.apply` (`furl_ctx/transforms/pipeline.py:189`, assembling CacheAligner → CrossMessageDeduper → ContentRouter at `pipeline.py:117/124/134`) → `ContentRouter.compress` (`furl_ctx/transforms/content_router.py:747`, the orchestrator entry) which detects content type via `_detect_content` (`content_router.py:224`) — Rust `detect_content_type` first (falling back to the Python regex detector) — then routes pure vs mixed content through `_compress_mixed`/`_compress_pure` (`content_router.py:813/836`) and per-strategy dispatch in `_apply_strategy_to_content` (`content_router.py:860`), sending JSON-arrays to SmartCrusher across the PyO3 bridge. JSON goes to `SmartCrusher.crush_array` (`crates/furl-core/src/transforms/smart_crusher/route.rs:147`): tier-1 lossless compaction (`compaction/compactor.rs:152` → `formatter.rs:293`), tier-2 lossy row-drop planned by `planning.rs:create_plan` (`:100`) + `orchestration.rs:prioritize_indices` (`:214`), then `persist_dropped` (`persist.rs:214`) writes the whole-blob to the CCR store and emits the `<<ccr:HASH N_rows_offloaded>>` sentinel via `marker_for_rows_offloaded` (`markers.rs:37`). CCR storage lives behind the `CcrStore` trait (`crates/furl-core/src/ccr/mod.rs:39`); Python mirrors hashes into `CompressionStore` (`furl_ctx/cache/compression_store.py`) so `furl_retrieve` resolves them. Prompt-cache fidelity is held by `CacheAligner` (`cache_aligner.py:258`) on the Python side plus the frozen-prefix count `_compute_frozen_message_count` (`compress.py:172`) — the pure-Python owner of that logic (the orphaned Rust `cache_control.rs::compute_frozen_count` was deleted).

> Note on the default chain: CacheAligner is opt-in and OFF by default via `CacheAlignerConfig.enabled=False`, so a default `compress()` assembles CrossMessageDeduper then ContentRouter only. Even when enabled, CacheAligner is detector-only and never rewrites or reorders messages. See `furl_ctx/config.py` and the gated append in `furl_ctx/transforms/pipeline.py`.

## 2. SUBSYSTEM MAP

**smart_crusher core (keep/drop + CCR emission) — Phase-8 split: monolithic crusher.rs → walk/route/persist**
- `route.rs:147` — `crush_array` — dispatch lossless-vs-lossy, route by RoutingPolicy (MinTokens), return CrushArrayResult.
- `route.rs:630` — `crush_array_lossy` — entropy-floor override, plan→execute→persist→optional survivor re-render.
- `persist.rs:214` — `persist_dropped` — write the whole-blob, emit `<<ccr:HASH N_rows_offloaded>>` (Design A: no per-row chunks, no `#rows` index).
- `persist.rs:34` — `ccr_sentinel_map` — build `{_ccr_dropped}` sentinel (recovery pointer unconditional on drop).
- `route.rs:1051` — `ccr_backed_keep_budget` — effective_max = adaptive_k/2, floor 5, cap adaptive_k.
- `route.rs:450` — `small_array_route` — EFF-3: fast path for arrays below the small-array threshold (bypasses full planning).
- `walk.rs:44` — `SmartCrusher::crush` — top-level entry dispatching to `smart_crush_content`.
- `walk.rs:104` — `smart_crush_content` — walks the content tree, dispatches arrays to `crush_array`.
- `walk.rs:183` — `process_value` — per-value dispatch (object/array/scalar routing).
- `orchestration.rs:214` — `prioritize_indices` — dedup→fill→union critical (errors+outliers+anomalies+query-pins+singletons)→novelty fill; may return >budget.

**planning + analyzer (strategy selection)**
- `planning.rs:100` — `create_plan` — dispatcher to plan_smart_sample/top_n/cluster_sample/time_series.
- `planning.rs:558` — `apply_query_signals` — deterministic anchors + high-relevance pins (never positionally dropped).
- `analyzer.rs:434` — `analyze_crushability` — 11-case decision tree; only `unique_entities_no_signal`/`medium_uniqueness_no_signal` eligible for entropy-floor override.
- `analyzer.rs:669` — `select_strategy` — crushability+pattern → Skip/TimeSeries/ClusterSample/TopN/SmartSample.

**compaction (lossless columnar)**
- `compaction/compactor.rs:152` — `compact` — array→IR (Table|Buckets|Untouched).
- `compaction/compactor.rs:208` — `build_homogeneous_table` — STRICT-ORDER stamps: constant→arith→iso→decimal→dict→head-dict→affix (round-trip proven at stamp time).
- `compaction/encodings.rs:29/202/285/401/460` — `parse_iso_strict`/`encode_iso_column`/`encode_decimal_cell`/`common_affix`/`split_head` — reversible primitives (pure string ops, no float math).
- `compaction/formatter.rs:293` — `write_table` — CSV-schema grammar `[N]{col:type,...}` + `__dict/__affix/__head:` preamble + ditto-marked rows.
- `compaction/formatter.rs:628` — `format_ccr_marker` — opaque-blob `<<ccr:HASH,KIND,SIZE>>`; thin shim delegating to `markers.rs::marker_for_opaque` (`:54`).
- `compaction/mod.rs:127` — `CompactionStage::run` — array → (Compaction IR, rendered CSV-schema string); the lossless tier-1 entry.

**CCR marker grammar — single-owner (Rust produces, Python parses)**
- `ccr/markers.rs:37/54/61/98` — `marker_for_rows_offloaded`/`marker_for_opaque`/`marker_for_diff`/`marker_for_retrieve_more` — the SINGLE construction point for every Rust marker. Owns the *grammar*, not the hash: producers compute their own key and pass `hash` in. Every Rust producer routes through here, pinned byte-for-byte by the in-module equivalence tests. (The `marker_for_row_index` producer for the `#rows` shape was removed with the granular offload, F8/#168.)
- `furl_ctx/ccr/marker_grammar.py:143/149/155` — `BRACKET_RETRIEVE_PATTERN`/`GENERIC_BRACKET_PATTERN`/`DOUBLE_ANGLE_PATTERN` + `marker_patterns()` (`:158`) — the SINGLE Python consumer spec. Accepted widths (`HASH_WIDTHS` at `marker_grammar.py:72`): 12 (sha256[:6], crusher rows) and 24 (md5[:24], diff/log/search).

**CCR storage**
- `ccr/mod.rs:39` — `CcrStore` trait — put/get/len, Send+Sync. (The old canonical `compute_key`/`marker_for` and the `blake3` dep were DELETED; hashing now lives at each producer call site — see § hash parity.)
- One backend ships (`InMemoryCcrStore`); the dead SQLite/Redis `from_config`/`CcrBackendConfig` factory was deleted — recovery is request-window-scoped (`CCR-RETENTION.md`).
- `ccr/backends/in_memory.rs:184/261` — `put`/`get` — FIFO capacity eviction, lazy TTL via remove_if (TOCTOU-safe).
- `ccr/persist.rs` — NEW: CCR persistence helpers shared between smart_crusher and other producers.

**CCR hash utilities (Phase-8 ARCH-8 extraction)**
- `util/pyjson.rs` — NEW: Python-JSON parity utilities (moved from `anchor_selector.rs` per ARCH-8; Python↔Rust JSON round-trip helpers used by hash_canonical).

**other transforms + compaction stage**
- `log_compressor.rs:290` — `FormatDetector::detect` / `log_compressor.rs:366` — `LevelClassifier::classify` — AhoCorasick format detect + per-line log-level classifier.
- `diff_compressor.rs:929` — `score_hunks` — change-density + context-word + priority weights.
- `search_compressor.rs:405` — `parse_search_results` — byte-prefix parser (Windows drive + dash filenames).
- `smart_crusher/planning.rs:174` — `detect_error_items_for_preservation`/`detect_structural_outliers` — keep/drop constraint detection, now inlined direct calls (the `Constraint`/`Observer` traits in `traits.rs`/`constraints.rs`/`observer.rs` were deleted).

**new Rust transforms (Phase-8)**
- `transforms/text_crusher.rs:450` — `TextCrusher` struct; `compress` at `:473` — Rust-side text compression (Python wrapper at `text_crusher.py`).
- `transforms/tag_protector.rs:476` — `protect_tags`; `restore_tags` at `:702` — HTML/XML tag fence-posting before compression, restored after.

**routing / tokenizer / relevance**
- `tokenizer/registry.rs:76` — `get_tokenizer` — Tiktoken → Estimation dispatch (the HF tokenizer backend was excised; the estimator's chars-per-token density is calibrated per model family). Python mirror (`furl_ctx/tokenizers/registry.py:169`) dispatches tiktoken plus anthropic/google/cohere backends — all three are family-calibrated estimators (`registry.py:84/94/104`).
- `tokenizer/tiktoken_impl.rs:109` — `encoding_for` — o200k/cl100k/p50k/r50k by model prefix.
- `furl_ctx/compress.py:224`: `_compute_frozen_message_count`, Python, only messages[].content `cache_control` blocks bump the floor, never system or tools. Pure-Python owner of frozen-prefix counting; the orphaned Rust `cache_control.rs::compute_frozen_count` was deleted.
- `relevance/bm25.rs:87` — `bm25_score` / `hybrid.rs:53` — `HybridScorer::score` — BM25 keyword scoring + the BM25-only boost (`boost_bm25_only`, `hybrid.rs:36`); the ML embedding tier was excised, so BM25 is the only scorer.
- `transforms/smart_crusher/config.rs:33` — `RoutingPolicy` — MinTokens (default, ties→lossless) vs LosslessFirst (legacy). `lossless_min_savings_ratio` default 0.30 at `config.rs:211`.
- `route.rs:982/1010` — `SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES` (256) / `LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES` (64) — lossless/survivor byte-floor constants (was crusher.rs).

**ContentRouter extracted seams (10 seams total: 6 original + 4 Phase-8)**
- `furl_ctx/transforms/router_cache.py:109` — `CompressionCache` — per-content TTL+skip cache (get/put/mark_skip/invalidate). `CacheDisposition` ADT at `router_cache.py:92` (was in `content_router.py`; moved Phase-8).
- `furl_ctx/transforms/router_split.py:40/60` — `is_mixed_content`/`split_into_sections` — mixed-content section splitter (`ContentSection` at `:22`, `_extract_json_block` at `:164`).
- `furl_ctx/transforms/router_policy.py:30/60/95/110/125` — `CompressionStrategy` enum + `strategy_from_detection`/`strategy_from_detection_type`/`content_type_from_strategy`/`adaptive_min_ratio` — strategy mappings + the adaptive ratio, all re-exported from `content_router.py`.
- `furl_ctx/transforms/router_dispatch.py:67/88` — `StrategyDispatcher` (`apply`) — per-strategy compressor dispatch + the SMART_CRUSHER→LOG→passthrough no-savings fallback chain.
- `furl_ctx/transforms/router_ccr_mirror.py:47/59/142` — `CcrMirror` (`ensure_ccr_backed`/`extract_ccr_hashes`) — result-cache HIT re-mirror of `<<ccr:HASH>>` pointers back into the Python store + hash extraction.
- `furl_ctx/transforms/router_engine.py:213/231` — NEW: `RoutingDecision` dataclass / `RouterCompressionResult` — engine-layer types for the router core (the `RouterHooks` Protocol was deleted; hooks are the concrete `ContentRouter`).
- `furl_ctx/transforms/router_blocks.py:96/60` — NEW: `ContentBlockWalker` / `BlockCompressFn` type alias — block-level walker abstraction used by the router to iterate content blocks (the single-implementer `Protocol` was collapsed to a plain `Callable` alias).
- `furl_ctx/transforms/router_message_policy.py:238` — NEW: `MessageDisposition` ADT (`Frozen:179`, `ProtectedMsg:185`, `Small:198`, `NonString:204`) + `classify_message` — message-level classification ADT replacing scattered conditionals (the `MessagePolicyConfig` Protocol was deleted).
- `furl_ctx/transforms/router_debug.py:45/49` — NEW: `_router_debug_dumps`/`_log_router_debug` — debug logging utilities extracted from the router orchestrator.

**new Python transforms (Phase-8)**
- `furl_ctx/transforms/text_crusher.py:101` — `TextCrusher` class — Python wrapper for Rust TextCrusher; `TextCrusherConfig` at `:51`, `TextCrushResult` at `:76`.
- `protect_tags`/`restore_tags` — Rust tag-protector, exposed to Python directly as PyO3 bindings (`crates/furl-py/src/lib.rs:1840`); the `furl_ctx/transforms/tag_protector.py` wrapper module was deleted.
- `furl_ctx/transforms/code_aware_compressor.py` — `CodeAwareCompressor` — opt-in tree-sitter-backed code compressor; `CodeLanguage` enum at `:158`.
- `furl_ctx/transforms/_ccr_persist.py:24` — `persist_to_python_ccr` — single Python entry for mirroring a CCR hash+payload into the Python `CompressionStore`.
- `furl_ctx/transforms/compressor_registry.py:42` — `CompressorRegistry` — maps strategy→compressor instances; replaces ad-hoc compressor construction inside router dispatch.

**new Python cache modules (Phase-8)**
- `furl_ctx/cache/backends/sqlite.py:189` — `SqliteBackend` — optional durable SQLite backend for `CompressionStore` (replaces the excised multi-backend factory).
- `furl_ctx/cache/retrieval_feedback.py:182` — `RetrievalFeedback` — tracks CCR retrieval patterns; `FeedbackHints` at `:164`, `ShapeKey` at `:135`.

**public API**
- `furl_ctx/compress.py:493`: `compress`, one-liner entry; inflation guard reverts if tokens grow at `compress.py:770`.
- `furl_ctx/compress.py:98/162`: `CompressConfig`/`CompressResult`, config + metrics.
- `crates/furl-py/src/lib.rs:905/963/1069` — `PySmartCrusher` / `crush` / `crush_array_json` — PyO3 bridge (GIL-released, validates at boundary).

## 3. CHANGE INDEX

- Add/modify a lossless column encoding → `compaction/compactor.rs:208` (build_homogeneous_table stamp order) + new `stamp_*` + `compaction/encodings.rs` encode/decode pair + `formatter.rs:293` render + `furl_ctx/transforms/csv_schema_decoder.py` Python decoder (byte-parity; `split_unquoted:259`, `_parse_iso:158`).
- Change keep/drop policy → `orchestration.rs:214` (prioritize_indices), `planning.rs` (plan_* signal sources, `create_plan:100`/`apply_query_signals:558`), `analyzer.rs:434` (crushability cases).
- Change CCR-backed keep budget → `route.rs:1051` (ccr_backed_keep_budget — divisor/floor/cap) and the effective_max_items routing upstream in `route.rs`.
- Touch CCR offload / sentinel → `persist.rs:214` (persist_dropped, whole-blob write), `persist.rs:34` (ccr_sentinel_map shape).
- Alter routing policy → `route.rs` (MinTokens match, render_token_count), `transforms/smart_crusher/config.rs:33` (RoutingPolicy enum).
- Change entropy-floor override → `route.rs` (CCR-backed crushability override gate: `allow_skip_override && skip_reason_is_no_signal`), `route.rs` (no-signal eligibility doc).
- Change lossless thresholds → `transforms/smart_crusher/config.rs:211` (lossless_min_savings_ratio 0.30), `route.rs:982/1010` (`SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES`=256, `LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES`=64).
- Change a CCR marker shape → `ccr/markers.rs:37/54/61/98` (the `marker_for_*` family — single Rust producer) + `furl_ctx/ccr/marker_grammar.py:143/149/155` (the consumer patterns) — keep the two in lockstep, pinned by the `markers.rs` equivalence tests.
- Change a CCR hash → at the producer call site: `persist.rs::hash_canonical` (sha256[:6] → 12 hex, row + array keys) OR `md5_hex_24` (md5[:24] → 24 hex) in `diff_compressor.rs`/`log_compressor.rs`/`search_compressor.rs`. Python mirror key: `compression_store.py:387` (`store(..., explicit_hash=...)`). Accepted consumer widths {12,24}: `marker_grammar.py:72` (`HASH_WIDTHS`). (No central `compute_key` anymore — it was deleted with `blake3`.)
- Change content routing / per-type dispatch → `content_router.py:747` (ContentRouter.compress orchestrator), `content_router.py:860` (`_apply_strategy_to_content`), `content_router.py:224` (detect), `transforms/detection.rs` (Rust `detect` chain + `ContentType`; the regex `content_detector.rs` parity mirror was deleted).
- Change frozen-count / cache contract → `furl_ctx/compress.py:224`: `_compute_frozen_message_count`, the pure-Python owner; walks `messages[].content` for `cache_control` blocks and returns the exclusive floor.
- Add a test (Rust) → `crates/furl-core/tests/ccr_roundtrip.rs:36` (`default_crusher_stores_dropped_rows`) / `tokenizer_proptest.rs:19` (`deterministic_per_instance`).
- Add a test (Python) → `tests/test_ccr_recovery_invariant.py:119` (`_recover_from_output` harness) / `tests/test_ccr_proportional_retrieval.py` (`test_row_drop_whole_blob_recovers_byte_exact`).
- Run a benchmark → `benchmarks/run_bench.py` (baseline) / `verify/run.py` (adversarial 6-seed sweep) / `verify/measure.py` (strict byte-exact cost model).

## 4. CONTRACT-ENFORCEMENT SITES

- **Recovery invariant (no data loss):** marker emission is UNCONDITIONAL on drop — `persist.rs:214` (persist_dropped writes store + emits marker regardless of `advertise_retrieval_tool`). Verified Rust: `tests/ccr_roundtrip.rs:177` (distinct_inputs_produce_distinct_store_entries), `:364` (nested_array_inside_object_gets_marker_injected); lossless-win-no-write at `ccr_roundtrip.rs:117`. Verified Python: `tests/test_ccr_recovery_invariant.py:216` (marker-off surfaces pointer), `:260` (opaque-blob recovers), `:290` (lossy survivor table), `:119` (`_recover_from_output` across Rust `ccr_get` + Python `py_store.retrieve`). Round-trip decoder: `csv_schema_decoder.py` (`split_unquoted:259`, `_parse_iso:158`) / `verify/independent_recheck.py` (strict, no substring fallback). COR-14 caveat: the encoder's nested-uniform flatten is unrecorded on the wire, so decoded rows carry dotted top-level keys — reconstruction is value-exact under dotted keys, and the recheck compares both sides un-flattened (`independent_recheck._unflatten_dotted`).
- **Row recovery is whole-blob only (NO proportional retrieval):** a row-drop stores ONLY the whole-blob parent (`persist.rs:214`); a bare `furl_retrieve(HASH)` returns the full array and `query=`/`select_field=` narrow it. A granular per-row `{hash}#rows` index was once emitted as a *proportional-retrieve* optimization but never worked on the model's path — the `HASH#rows` key fails `is_valid_ccr_hash` and the chunks were never mirrored to the Python store — so it was removed (F8/#168). Pinned by `tests/test_ccr_proportional_retrieval.py` (`test_row_drop_whole_blob_recovers_byte_exact` through the PRODUCTION store; `test_whole_blob_retrieval_cost_goes_negative` at ≥25%) — the honest cost `verify/measure.py` now charges.
- **Prompt-cache ordering / byte-fidelity:** `furl_ctx/compress.py:172` (`_compute_frozen_message_count` — only `messages[].content` `cache_control` blocks bump the frozen floor; system/tools always hot). Python prefix-stability is held by `CacheAligner.apply` (`cache_aligner.py:258`), which never reorders/rewrites the frozen prefix and compares against the caller-supplied `previous_prefix_hash` kwarg (read at `cache_aligner.py:341`, surfaced as `stable_prefix_hash` in the result metrics). Enforced by `tests/test_cache_aligner_prefix_hash.py`, `tests/test_cache_aligner_hardening.py`, `tests/test_compress_frozen_prefix.py`.
- **Python↔Rust hash parity (per-producer, no central key):** there is NO single `compute_key` anymore — each producer owns its hash and the grammar lives in `markers.rs`. SmartCrusher rows/array: `hash_canonical` = sha256[:6] → 12 hex (home: `smart_crusher/persist.rs:493`, pinned by `persist.rs::tests::hash_canonical_pinned_vectors` at `:771` + wire-form twin at `:807`, and cross-checked from Python by `tests/test_ccr_hash_parity_vectors.py` (`test_pinned_vectors_match_the_rust_literals:115`, `test_wire_form_vectors_match_the_rust_literals:125`)); diff/log/search: `md5_hex_24` = md5[:24] → 24 hex (`diff_compressor.rs` etc., byte-pinned to Python `hashlib.md5(...)[:24]`). Python mirrors via `compression_store.store(..., explicit_hash=hash)` (`compression_store.py:387`; `smart_crusher.py:912` `_mirror_single_hash_to_python_store`, `:743` `_mirror_ccr_to_python_store`) and `_ccr_persist.py:24` `persist_to_python_ccr`.
- **apply() kwargs allowlist (typo guard):** `ContentRouter.apply` (`content_router.py:1000`) rejects any kwarg not in the module-level `_APPLY_ALLOWED_KWARGS` frozenset (`content_router.py:511`), so a misspelled per-request option fails loud instead of being silently ignored.

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
pytest tests/test_ccr_recovery_invariant.py           # recovery invariant (27 tests)
pytest tests/test_ccr_proportional_retrieval.py       # whole-blob recovery + honest cost
pytest tests/test_ccr_hash_parity_vectors.py          # Python↔Rust hash parity vectors
pytest -m "not real_llm and not live"                 # fast unit only

# Benchmark + restore baseline
.venv/bin/python -m benchmarks.run_bench              # baseline on committed snapshots -> baseline_results.json + BASELINE.md
.venv/bin/python -m verify.run                        # adversarial 6-seed sweep, cold CCR per subprocess -> verify/raw_results.json
.venv/bin/python -m benchmarks.run_bench --refresh    # RE-CAPTURE live snapshots (overwrites benchmarks/data/*.raw.json)
# Restore baseline: re-run WITHOUT --refresh (uses committed snapshots), or `git checkout HEAD -- benchmarks/data/` to revert refreshed snapshots
```

## 6. DELIBERATE DECISIONS (by-design; the trigger that would reopen each)

- **Two CCR stores, not one.** Rust `CcrStore` (`ccr/mod.rs:39`, InMemory default) is the COMPUTE-side write buffer: `persist.rs::persist_dropped` writes here and `ccr_get` reads typed bytes back over the FFI. Python `CompressionStore` (`compression_store.py`) is the MODEL-FACING retrieval surface the MCP `furl_retrieve` reads (`mcp_server.py:330/448`) — it adds built-in BM25 `search(hash, query)` + retrieval-feedback tracking that the bare Rust KV `ccr_get` lacks, so routing retrieve straight at Rust would regress search/feedback. The Rust store is in-memory single-tier, default 1000 entries and 1800s TTL. The Python `CompressionStore` runs over a pluggable backend: `InMemoryBackend` on the library default, and `SqliteBackend` on the MCP server, the `furl` CLI, and the plugin, selected by `FURL_CCR_BACKEND`. It also takes an optional durable spill tier via `FURL_CCR_SPILL=1`, which demotes evicted entries instead of deleting them. See `CCR-RETENTION.md`. An evicted or expired miss is loud via `format_retrieval_miss_detail`, never silent. REOPEN IF: a non-MCP reader needs recovery, or the Python store stops offering anything the Rust store can't — then the split no longer earns its keep.
- **CCR-emission knobs live in Rust config, pinned on the Python surface.** `min_compression_ratio_for_ccr` (default 0.8) and siblings are Rust config fields; the Python compressors pass the Rust default through and do NOT re-expose them as tunables (`diff_compressor.py:93`, the `min_compression_ratio_for_ccr` passthrough comment; uniform across diff/search/log). Capability ceiling by intent — no consumer needs per-call CCR-aggressiveness tuning and the default matches the value the retired Python original inlined. REOPEN IF: a real caller needs per-call control over the CCR-emission threshold — then promote the knob to the Python surface.
Notes: `cargo test` cannot run the `furl-py` cdylib (`test=false` in Cargo.toml) — Python-side tests only. The core is ML-free with no feature flags (`default = []`); the ML backends (magika/embeddings, ONNX `ort`) and the SQLite/Redis CCR backends were excised — relevance is BM25-only and the Rust core's CCR store is in-memory-only (the dead `from_config`/`CcrBackendConfig` factory was deleted). The Python `CompressionStore` is separate and keeps its `SqliteBackend` plus optional spill. See `CCR-RETENTION.md`. Default model gpt-4o (real tiktoken); benchmarks use RoutingPolicy.MinTokens with CompressConfig defaults.
