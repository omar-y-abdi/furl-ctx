# FURL COMPRESSION ENGINE — NAVIGATION MAP

> **Anchors verified 2026-07-25.** Furl is a standalone solo project (not a fork). The only live
> route is the Python `TransformPipeline` → Rust SmartCrusher, surfaced as a hook + MCP tool.
>
> **Things that do not exist — stop looking for them.** No ML text compressor and no `[ml]` extra;
> no HTML extraction and no `[html]` extra; no telemetry/compression-feedback plane; no
> HuggingFace/Mistral tokenizer backends (tokenizers are tiktoken + family-calibrated estimators
> only); no Rust code compressor (large distinct code takes the reversible CCR offload); no
> `RouterRuntime` carrier; no Rust regex content-detector mirror; no `signals/tiered.rs`; no
> monolithic `crusher.rs` — it retains only the `SmartCrusher` struct, its constructors and
> `execute_plan`, while `crush`/`crush_array`/`persist_dropped` live in
> `smart_crusher/{walk,route,persist}.rs`; no central
> `compute_key`/`marker_for` in `ccr/mod.rs` and no `blake3` dep — every Rust marker flows through
> `ccr/markers.rs`, every Python consumer through `furl_ctx/ccr/marker_grammar.py`; no
> `ccr/backends/` directory (the single Rust backend is `ccr/in_memory.rs`); no
> `smart_crusher/hashing.rs` (folded into `planning.rs`); no `furl_ctx/transforms/tag_protector.py`
> and no PyO3 tag-protector bindings (`protect_tags`/`restore_tags`/`is_html_tag`/
> `known_html_tag_names` are Rust-internal only).
>
> Function-name anchors are authoritative; line numbers may drift from later edits — if a line
> looks off, grep the `fn`/`def` name. The map orients; always trust the real code.

## 1. PIPELINE

End-to-end flow: `compress(messages,model)` (`furl_ctx/compress.py:640`) → `TransformPipeline.apply` (`furl_ctx/transforms/pipeline.py:183`, assembling CacheAligner → CrossMessageDeduper → ContentRouter at `pipeline.py:111/118/128`) → `ContentRouter.compress` (`furl_ctx/transforms/content_router.py:756`, the orchestrator entry) which detects content type via `_detect_content` (`content_router.py:231`) — Rust `detect_content_type` first (falling back to the Python regex detector) — then routes pure vs mixed content through `_compress_mixed`/`_compress_pure` (`content_router.py:822/845`) and per-strategy dispatch in `_apply_strategy_to_content` (`content_router.py:869`), sending JSON-arrays to SmartCrusher across the PyO3 bridge. JSON goes to `SmartCrusher.crush_array` (`crates/furl-core/src/transforms/smart_crusher/route.rs:145`): tier-1 lossless compaction (`compaction/compactor.rs:154` → `formatter.rs:311`), tier-2 lossy row-drop planned by `planning.rs:create_plan` (`:70`) + `orchestration.rs:prioritize_indices` (`:213`), then `persist_dropped` (`persist.rs:166`) writes the whole-blob to the CCR store and emits the `<<ccr:HASH N_rows_offloaded>>` sentinel via `marker_for_rows_offloaded` (`markers.rs:36`). CCR storage lives behind the `CcrStore` trait (`crates/furl-core/src/ccr/mod.rs:40`); Python mirrors hashes into `CompressionStore` (`furl_ctx/cache/compression_store.py`) so `furl_retrieve` resolves them. Prompt-cache fidelity is held by `CacheAligner` (`cache_aligner.py:254`) on the Python side plus the frozen-prefix count `_compute_frozen_message_count` (`compress.py:373`) — the pure-Python owner of that logic (the orphaned Rust `cache_control.rs::compute_frozen_count` was deleted).

> Note on the default chain: CacheAligner is opt-in and OFF by default via `CacheAlignerConfig.enabled=False`, so a default `compress()` assembles CrossMessageDeduper then ContentRouter only. Even when enabled, CacheAligner is detector-only and never rewrites or reorders messages. See `furl_ctx/config.py` and the gated append in `furl_ctx/transforms/pipeline.py`.

## 2. SUBSYSTEM MAP

**smart_crusher core (keep/drop + CCR emission) — walk/route/persist**
- `route.rs:145` — `crush_array` — dispatch lossless-vs-lossy, route by RoutingPolicy (MinTokens), return CrushArrayResult.
- `route.rs:628` — `crush_array_lossy` — entropy-floor override, plan→execute→persist→optional survivor re-render.
- `persist.rs:166` — `persist_dropped` — write the whole-blob, emit `<<ccr:HASH N_rows_offloaded>>` (Design A: no per-row chunks, no `#rows` index).
- `persist.rs:31` — `ccr_sentinel_map` — build `{_ccr_dropped}` sentinel (recovery pointer unconditional on drop).
- `route.rs:1171` — `ccr_backed_keep_budget` — effective_max = adaptive_k/2, floor 5, cap adaptive_k.
- `route.rs:448` — `small_array_route` — fast path for arrays below the small-array threshold (bypasses full planning).
- `walk.rs:43` — `SmartCrusher::crush` — top-level entry dispatching to `smart_crush_content`.
- `walk.rs:83` — `smart_crush_content` — walks the content tree, dispatches arrays to `crush_array`.
- `walk.rs:162` — `process_value` — per-value dispatch (object/array/scalar routing).
- `orchestration.rs:213` — `prioritize_indices` — dedup→fill→union critical (errors+outliers+anomalies+query-pins+singletons)→novelty fill; may return >budget.

**planning + analyzer (strategy selection)**
- `planning.rs:70` — `create_plan` — dispatcher to plan_smart_sample/top_n/cluster_sample/time_series.
- `planning.rs:541` — `apply_query_signals` — deterministic anchors + high-relevance pins (never positionally dropped).
- `analyzer.rs:434` — `analyze_crushability` — 11-case decision tree; only `unique_entities_no_signal`/`medium_uniqueness_no_signal` eligible for entropy-floor override.
- `analyzer.rs:666` — `select_strategy` — crushability+pattern → Skip/TimeSeries/ClusterSample/TopN/SmartSample.

**compaction (lossless columnar)**
- `compaction/compactor.rs:154` — `compact` — array→IR (Table|Buckets|Untouched).
- `compaction/compactor.rs:210` — `build_homogeneous_table` — STRICT-ORDER stamps: constant→arith→iso→decimal→dict→head-dict→affix (round-trip proven at stamp time).
- `compaction/encodings.rs:29/202/285/401/460` — `parse_iso_strict`/`encode_iso_column`/`encode_decimal_cell`/`common_affix`/`split_head` — reversible primitives (pure string ops, no float math).
- `compaction/formatter.rs:311` — `write_table` — CSV-schema grammar `[N]{col:type,...}` + `__dict/__affix/__head:` preamble + ditto-marked rows.
- `compaction/formatter.rs:700` — `format_ccr_marker` — opaque-blob `<<ccr:HASH,KIND,SIZE>>`; thin shim delegating to `markers.rs::marker_for_opaque` (`:68`).
- `compaction/mod.rs:126` — `CompactionStage::run` — array → (Compaction IR, rendered CSV-schema string); the lossless tier-1 entry.

**CCR marker grammar — single-owner (Rust produces, Python parses)**
- `ccr/markers.rs:36/68/81/118` — `marker_for_rows_offloaded`/`marker_for_opaque`/`marker_for_diff`/`marker_for_retrieve_more` — the SINGLE construction point for every Rust marker. Owns the *grammar*, not the hash: producers compute their own key and pass `hash` in. Every Rust producer routes through here, pinned byte-for-byte by the in-module equivalence tests. (There is no `marker_for_row_index` — the `#rows` shape went with the granular offload.)
- `furl_ctx/ccr/marker_grammar.py:149/173/179` — `BRACKET_RETRIEVE_PATTERN`/`GENERIC_BRACKET_PATTERN`/`DOUBLE_ANGLE_PATTERN` + `marker_patterns()` (`:245`) — the SINGLE Python consumer spec. Accepted widths (`HASH_WIDTHS` at `marker_grammar.py:78`): 12 (sha256[:6], crusher rows) and 24 (md5[:24], diff/log/search).

**CCR storage**
- `ccr/mod.rs:40` — `CcrStore` trait — put/get/len, Send+Sync. (`len` is a telemetry counter, so there is deliberately no `is_empty`, and no `capacity` accessor. Hashing lives at each producer call site — see § hash parity.)
- One backend ships (`InMemoryCcrStore`); there is no backend factory and no `ccr/backends/` directory — recovery is request-window-scoped (`CCR-RETENTION.md`).
- `ccr/in_memory.rs:184/299` — `put`/`get` — FIFO capacity eviction, lazy TTL via remove_if (TOCTOU-safe).
- `ccr/persist.rs` — CCR persistence helpers shared between smart_crusher and other producers.

**CCR hash utilities**
- `util/pyjson.rs` — Python↔Rust JSON round-trip parity helpers used by `hash_canonical`.

**other transforms + compaction stage**
- `log_compressor.rs:273` — `FormatDetector::detect` / `log_compressor.rs:347` — `LevelClassifier::classify` — AhoCorasick format detect + per-line log-level classifier.
- `diff_compressor.rs:908` — `score_hunks` — change-density + context-word + priority weights.
- `search_compressor.rs:435` — `parse_search_results` — byte-prefix parser (Windows drive + dash filenames).
- `smart_crusher/outliers.rs:249/60` — `detect_error_items_for_preservation`/`detect_structural_outliers` — keep/drop constraint detection, called directly from `planning.rs:173/174` (there are no `Constraint`/`Observer` traits).

**other Rust transforms**
- `transforms/text_crusher.rs:464` — `TextCrusher` struct; `compress` at `:483` — Rust-side text compression (Python wrapper at `text_crusher.py`).
- `transforms/tag_protector.rs:441` — `protect_tags`; `restore_tags` at `:646` — HTML/XML tag fence-posting before compression, restored after. Rust-internal only: there is no Python binding and no Python wrapper module.

**routing / tokenizer / relevance**
- `tokenizer/registry.rs:83` — `get_tokenizer` — Tiktoken → Estimation dispatch (the estimator's chars-per-token density is calibrated per model family). Python mirror (`furl_ctx/tokenizers/registry.py:241`) dispatches tiktoken plus anthropic/google/cohere backends via the `_factories` table (`registry.py:198`) — all three are family-calibrated estimators (`_create_anthropic:142`, `_create_fixed_estimation:170`).
- `tokenizer/tiktoken_impl.rs:101` — `encoding_for` — o200k/cl100k/p50k/r50k by model prefix.
- `furl_ctx/compress.py:373`: `_compute_frozen_message_count`, Python, only messages[].content `cache_control` blocks bump the floor, never system or tools. Pure-Python owner of frozen-prefix counting.
- `relevance/bm25.rs:78` — `bm25_score` / `hybrid.rs:53` — `HybridScorer::score` — BM25 keyword scoring + the BM25-only boost (`boost_bm25_only`, `hybrid.rs:36`); BM25 is the only scorer.
- `transforms/smart_crusher/config.rs:33` — `RoutingPolicy` — MinTokens (default, ties→lossless) vs LosslessFirst (legacy). `lossless_min_savings_ratio` default 0.30 at `config.rs:192`.
- `route.rs:1102/1130` — `SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES` (256) / `LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES` (64) — lossless/survivor byte-floor constants.

**ContentRouter extracted seams (10 files)**
- `furl_ctx/transforms/router_cache.py:109` — `CompressionCache` — per-content TTL+skip cache (get/put/mark_skip/invalidate). `CacheDisposition` ADT at `router_cache.py:92`.
- `furl_ctx/transforms/router_split.py:92/124` — `is_mixed_content`/`split_into_sections` — mixed-content section splitter (`ContentSection` at `:23`, `_extract_json_block` at `:232`).
- `furl_ctx/transforms/router_policy.py:37/53/88/103/118` — `CompressionStrategy` enum + `strategy_from_detection`/`strategy_from_detection_type`/`content_type_from_strategy`/`adaptive_min_ratio` — strategy mappings + the adaptive ratio, all re-exported from `content_router.py`.
- `furl_ctx/transforms/router_dispatch.py:67/88` — `StrategyDispatcher` (`apply`) — per-strategy compressor dispatch + the SMART_CRUSHER→LOG→passthrough no-savings fallback chain.
- `furl_ctx/transforms/router_ccr_mirror.py:50/62/149` — `CcrMirror` (`ensure_ccr_backed`/`extract_ccr_hashes`) — result-cache HIT re-mirror of `<<ccr:HASH>>` pointers back into the Python store + hash extraction.
- `furl_ctx/transforms/router_engine.py:213/231` — `RoutingDecision` dataclass / `RouterCompressionResult` — engine-layer types for the router core (hooks are the concrete `ContentRouter`, not a Protocol).
- `furl_ctx/transforms/router_blocks.py:65/60` — `ContentBlockWalker` / `BlockCompressFn` type alias — block-level walker abstraction used by the router to iterate content blocks.
- `furl_ctx/transforms/router_message_policy.py:237` — `classify_message` + the `MessageDisposition` ADT at `:227` (`Frozen:163`, `ProtectedMsg:169`, `Small:182`, `NonString:188`) — message-level classification replacing scattered conditionals.
- `furl_ctx/transforms/router_debug.py:45/49` — `_router_debug_dumps`/`_log_router_debug` — debug logging utilities extracted from the router orchestrator.

**other Python transforms**
- `furl_ctx/transforms/text_crusher.py:108` — `TextCrusher` class — Python wrapper for Rust TextCrusher; `TextCrusherConfig` at `:51`, `TextCrushResult` at `:83`.
- `furl_ctx/transforms/code_aware_compressor.py:527` — `CodeAwareCompressor` — opt-in tree-sitter-backed code compressor; `CodeLanguage` enum at `:154`.
- `furl_ctx/transforms/_ccr_persist.py:28` — `persist_to_python_ccr` — single Python entry for mirroring a CCR hash+payload into the Python `CompressionStore`.
- `furl_ctx/transforms/compressor_registry.py:42` — `CompressorRegistry` — maps strategy→compressor instances; replaces ad-hoc compressor construction inside router dispatch.

**Python cache modules**
- `furl_ctx/cache/backends/sqlite.py:230` — `SqliteBackend` — optional durable SQLite backend for `CompressionStore`.
- `furl_ctx/cache/retrieval_feedback.py:181` — `RetrievalFeedback` — tracks CCR retrieval patterns; `FeedbackHints` at `:163`, `ShapeKey` at `:134`.

**public API**
- `furl_ctx/compress.py:640`: `compress`, one-liner entry; inflation guard reverts if tokens grow at `compress.py:914`.
- `furl_ctx/compress.py:104/216`: `CompressConfig`/`CompressResult`, config + metrics (`OpaqueOffload` at `:168`).
- `crates/furl-py/src/lib.rs:710/768/843` — `PySmartCrusher` / `crush` / `crush_array_json` — PyO3 bridge (GIL-released, validates at boundary).

## 3. CHANGE INDEX

- Add/modify a lossless column encoding → `compaction/compactor.rs:210` (build_homogeneous_table stamp order) + new `stamp_*` + `compaction/encodings.rs` encode/decode pair + `formatter.rs:311` render + `furl_ctx/transforms/csv_schema_decoder.py` Python decoder (byte-parity; `split_unquoted:405`, `_parse_iso:271`).
- Change keep/drop policy → `orchestration.rs:213` (prioritize_indices), `planning.rs` (plan_* signal sources, `create_plan:70`/`apply_query_signals:541`), `analyzer.rs:434` (crushability cases).
- Change CCR-backed keep budget → `route.rs:1171` (ccr_backed_keep_budget — divisor/floor/cap) and the effective_max_items routing upstream in `route.rs`.
- Touch CCR offload / sentinel → `persist.rs:166` (persist_dropped, whole-blob write), `persist.rs:31` (ccr_sentinel_map shape).
- Alter routing policy → `route.rs` (MinTokens match, render_token_count), `transforms/smart_crusher/config.rs:33` (RoutingPolicy enum).
- Change entropy-floor override → `route.rs` (CCR-backed crushability override gate: `allow_skip_override && skip_reason_is_no_signal`), `route.rs` (no-signal eligibility doc).
- Change lossless thresholds → `transforms/smart_crusher/config.rs:192` (lossless_min_savings_ratio 0.30), `route.rs:1102/1130` (`SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES`=256, `LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES`=64).
- Change a CCR marker shape → `ccr/markers.rs:36/68/81/118` (the `marker_for_*` family — single Rust producer) + `furl_ctx/ccr/marker_grammar.py:149/173/179` (the consumer patterns) — keep the two in lockstep, pinned by the `markers.rs` equivalence tests.
- Change a CCR hash → at the producer call site: `persist.rs:302` (`hash_canonical`, sha256[:6] → 12 hex, row + array keys) OR `md5_hex_24` (md5[:24] → 24 hex) in `diff_compressor.rs`/`log_compressor.rs`/`search_compressor.rs`. Python mirror key: `compression_store.py:570` (`store(..., explicit_hash=...)`). Accepted consumer widths {12,24}: `marker_grammar.py:78` (`HASH_WIDTHS`). There is no central `compute_key`.
- Change content routing / per-type dispatch → `content_router.py:756` (ContentRouter.compress orchestrator), `content_router.py:869` (`_apply_strategy_to_content`), `content_router.py:231` (detect), `transforms/detection.rs:40` (Rust `detect` chain + `ContentType`, whose only variants are `GitDiff`/`PlainText`; there is no regex parity mirror).
- Change frozen-count / cache contract → `furl_ctx/compress.py:373`: `_compute_frozen_message_count`, the pure-Python owner; walks `messages[].content` for `cache_control` blocks and returns the exclusive floor.
- Add a test (Rust) → `crates/furl-core/tests/ccr_roundtrip.rs:36` (`default_crusher_stores_dropped_rows`) / `tokenizer_proptest.rs:19` (`deterministic_per_instance`).
- Add a test (Python) → `tests/test_ccr_recovery_invariant.py:98` (`_recover_from_output` harness) / `tests/test_ccr_proportional_retrieval.py` (`test_row_drop_whole_blob_recovers_byte_exact`).
- Run a benchmark → `benchmarks/run_bench.py` (baseline) / `verify/run.py` (adversarial 6-seed sweep) / `verify/measure.py` (strict byte-exact cost model).

## 4. CONTRACT-ENFORCEMENT SITES

- **Recovery invariant (no data loss):** marker emission is UNCONDITIONAL on drop — `persist.rs:166` (persist_dropped writes store + emits marker regardless of `advertise_retrieval_tool`). Verified Rust: `tests/ccr_roundtrip.rs:177` (distinct_inputs_produce_distinct_store_entries), `:332` (nested_array_inside_object_gets_marker_injected); lossless-win-no-write at `ccr_roundtrip.rs:117`. Verified Python: `tests/test_ccr_recovery_invariant.py:195` (marker-off surfaces pointer), `:239` (opaque-blob recovers), `:268` (lossy survivor table), `:98` (`_recover_from_output` across Rust `ccr_get` + Python `py_store.retrieve`). Round-trip decoder: `csv_schema_decoder.py` (`split_unquoted:405`, `_parse_iso:271`) / `verify/independent_recheck.py` (strict, no substring fallback). Caveat: the encoder's nested-uniform flatten is unrecorded on the wire, so decoded rows carry dotted top-level keys — reconstruction is value-exact under dotted keys, and the recheck compares both sides un-flattened (`independent_recheck._unflatten_dotted`).
- **Row recovery is whole-blob only (NO proportional retrieval):** a row-drop stores ONLY the whole-blob parent (`persist.rs:166`); a bare `furl_retrieve(HASH)` returns the full array and `query=`/`select_field=` narrow it. A granular per-row `{hash}#rows` index was once emitted as a *proportional-retrieve* optimization but never worked on the model's path — the `HASH#rows` key fails `is_valid_ccr_hash` and the chunks were never mirrored to the Python store — so it was removed. Pinned by `tests/test_ccr_proportional_retrieval.py` (`test_row_drop_whole_blob_recovers_byte_exact` through the PRODUCTION store; `test_whole_blob_retrieval_cost_goes_negative` at ≥25%) — the honest cost `verify/measure.py` now charges.
- **Prompt-cache ordering / byte-fidelity:** `furl_ctx/compress.py:373` (`_compute_frozen_message_count` — only `messages[].content` `cache_control` blocks bump the frozen floor; system/tools always hot). Python prefix-stability is held by `CacheAligner.apply` (`cache_aligner.py:254`), which never reorders/rewrites the frozen prefix and compares against the caller-supplied `previous_prefix_hash` kwarg (read at `cache_aligner.py:333`, surfaced as `stable_prefix_hash` in the result metrics). Enforced by `tests/test_cache_aligner_prefix_hash.py`, `tests/test_cache_aligner_hardening.py`, `tests/test_compress_frozen_prefix.py`.
- **Python↔Rust hash parity (per-producer, no central key):** each producer owns its hash and the grammar lives in `markers.rs`. SmartCrusher rows/array: `hash_canonical` = sha256[:6] → 12 hex (home: `smart_crusher/persist.rs:302`, pinned by `persist.rs::tests::hash_canonical_pinned_vectors` at `:450` + wire-form twin at `:488`, and cross-checked from Python by `tests/test_ccr_hash_parity_vectors.py`); diff/log/search: `md5_hex_24` = md5[:24] → 24 hex (`diff_compressor.rs` etc., byte-pinned to Python `hashlib.md5(...)[:24]`). Python mirrors via `compression_store.store(..., explicit_hash=hash)` (`compression_store.py:570`; `smart_crusher.py:767` `_mirror_single_hash_to_python_store`, `:630` `_mirror_ccr_to_python_store`) and `_ccr_persist.py:28` `persist_to_python_ccr`.
- **apply() kwargs allowlist (typo guard):** `ContentRouter.apply` (`content_router.py:1000`) rejects any kwarg not in the module-level `_APPLY_ALLOWED_KWARGS` frozenset (`content_router.py:512`), so a misspelled per-request option fails loud instead of being silently ignored.

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
pytest tests/test_ccr_recovery_invariant.py           # recovery invariant (25 tests)
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

- **Two CCR stores, not one.** Rust `CcrStore` (`ccr/mod.rs:40`, InMemory default) is the COMPUTE-side write buffer: `persist.rs::persist_dropped` writes here and `ccr_get` reads typed bytes back over the FFI. Python `CompressionStore` (`compression_store.py`) is the MODEL-FACING retrieval surface the MCP `furl_retrieve` reads (`mcp_server.py:1654/2385` — `_retrieve_content`/`_handle_retrieve`) — it adds built-in BM25 `search(hash, query)` + retrieval-feedback tracking that the bare Rust KV `ccr_get` lacks, so routing retrieve straight at Rust would regress search/feedback. The Rust store is in-memory single-tier, default 1000 entries and 1800s TTL. The Python `CompressionStore` runs over a pluggable backend: `InMemoryBackend` on the library default, and `SqliteBackend` on the MCP server, the `furl` CLI, and the plugin, selected by `FURL_CCR_BACKEND`. It also takes an optional durable spill tier via `FURL_CCR_SPILL=1`, which demotes evicted entries instead of deleting them. See `CCR-RETENTION.md`. An evicted or expired miss is loud via `format_retrieval_miss_detail`, never silent. REOPEN IF: a non-MCP reader needs recovery, or the Python store stops offering anything the Rust store can't — then the split no longer earns its keep.
- **CCR-emission knobs live in Rust config, pinned on the Python surface.** `min_compression_ratio_for_ccr` (default 0.8) and siblings are Rust config fields; the Python compressors pass the Rust default through and do NOT re-expose them as tunables (`diff_compressor.py:93`, the `min_compression_ratio_for_ccr` passthrough comment; uniform across diff/search/log). Capability ceiling by intent — no consumer needs per-call CCR-aggressiveness tuning and the default matches the value the retired Python original inlined. REOPEN IF: a real caller needs per-call control over the CCR-emission threshold — then promote the knob to the Python surface.
Notes: `cargo test` cannot run the `furl-py` cdylib (`test=false` in Cargo.toml) — Python-side tests only. The core is ML-free with no feature flags (`default = []`); the ML backends (magika/embeddings, ONNX `ort`) and the SQLite/Redis CCR backends were excised — relevance is BM25-only and the Rust core's CCR store is in-memory-only (the dead `from_config`/`CcrBackendConfig` factory was deleted). The Python `CompressionStore` is separate and keeps its `SqliteBackend` plus optional spill. See `CCR-RETENTION.md`. Default model gpt-4o (real tiktoken); benchmarks use RoutingPolicy.MinTokens with CompressConfig defaults.
