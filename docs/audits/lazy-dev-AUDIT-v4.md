# lazy-dev-AUDIT-v4.md — 4th-Pass Reachability Simplification Audit

**Tree:** POST-CUT, branch `verify/phase2-audit-report` @ `de3fd231` · 44,217 LOC (Rust 23,146 + Python 21,071) · proxy/ excluded (condemned, deleted in step 3).

## Headline — the tree is now essentially lean; this pass hunted reachability, not tool hits

The static tools came back **clean**. cargo build -p headroom-core: **0 warnings, 0 dead_code, 0 `#[allow(dead_code)]`**. vulture: **2 hits** (`mcp_server.py:905 direct_mode`, `_telemetry_noop.py:47`). ruff: **34 line-level nits** (29 RUF100, 5 ERA001). Line-level dead code is gone. So v4 traced **4-vector reachability from `compress()` + the MCP/hook entries** — finding what imports cleanly but is never invoked on the live route.

**Genuinely-removable-NOW fat is modest — the honest 4th-pass result:**

| Bucket | LOC | Free? |
|---|---|---|
| **Tier 1 — safe to cut now** | **~3,650** | Yes (delete + trim subpackage `__init__`) |
| **Tier 2 — cut after 1 untangle edge** | **~879** | After de-dup edge |
| **Tier 3 — surface-deprecation tail** | **~6,500** | **No** — needs API deprecation cycle + version bump |
| **Tier 0 — archaeology** | **~130** | Yes (doc cruft / dead tests) |
| Decision-gate roadmap (NOT cut) | ~520 | Only if roadmap formally abandoned |

Tier-1-safe-now is **~3.65k of 44,217 (~8%)**, and ~1.4k of that is the CCR proxy-plane that only becomes dead **after** step 3. The big numbers (cache-optimizer 2.5k, anchor_selector 0.77k, code_compressor 2k, embedding scorers 0.5k) are **public-export surface** — removable only as a coordinated deprecation, never a free delete. v4's real value: it confirms the tree is lean, maps the surface-deprecation tail, and **catches two deptry false-positives** (KompressCompressor, ComponentTracker) — see Refuted appendix.

---

## TIER 1 — SAFE NOW (verified no live caller, minimal untangle)

| # | What | Paths | ~LOC | Rung | Tag | Reachability | Evidence |
|---|---|---|---|---|---|---|---|
| 1 | **ccr/response_handler.py** — proxy response-interception (CCRResponseHandler/StreamingCCRHandler) | headroom/ccr/response_handler.py | 896 | 1 | delete | test-only **(needs review)** | Sole non-self importers: ccr/__init__ re-export + `tests/test_ccr_eviction_loud_miss.py`. mcp_server.py refs are **comments** (L347,452), not imports. Proxy (only prod caller) condemned. ⚠ test asserts the "loud CCR miss" recovery invariant — migrate that test before deletion. |
| 2 | **anchor_selector.py** → *see Tier 3 #1* — has top-level `__all__` export, reclassified | — | — | — | — | — | Moved to Tier 3 (public-export surface). |
| 3 | **ccr/context_tracker.py** — multi-turn ContextTracker | headroom/ccr/context_tracker.py | 660 | 1 | delete | dead | Panel confirmed×3. Only ccr/__init__ re-exports it; **zero** other importers, **zero** tests. Proxy was sole prod caller (condemned). Not in top-level `__all__`. |
| 4 | **compression_units.py** — orphaned provider-adapter / unit-routing API | headroom/transforms/compression_units.py | 364 | 1 | delete | test-only | **Off-surface** — my grep confirms `transforms/__init__` does NOT reference it (api-surface lens's "_LAZY_EXPORTS exports CompressionUnit" is a self-refutation; the string entry would have matched). Only `tests/test_compression_units.py` + `test_compression_determinism.py` import it. |
| 5 | **ccr/batch_store.py** — BatchContextStore (batch-API CCR) | headroom/ccr/batch_store.py | 313 | 1 | delete | dead | Panel confirmed×3. Only ccr/__init__ re-exports; **zero** tests, **zero** mcp_server refs. Prior caller `archive/batch_processor.py` already cut. Subsumes its `get_memory_stats()` (51 LOC) — do not double-count. |
| 6 | **utils.py** — 13 orphaned proxy-era helpers | headroom/utils.py | 110 | 1 | archaeology | dead | `generate_request_id, fast_hash, compute_messages_hash, compute_prefix_hash, format_timestamp, parse_timestamp, create_dropped_context_marker, create_truncated_marker, extract_markers, safe_json_loads, safe_json_dumps, estimate_cost, format_cost` — 0 prod/test hits. 4 live fns stay. Surgical intra-file delete. |
| 7 | **get_memory_stats()** — compression_store.py MemoryTracker method | headroom/cache/compression_store.py:866-897 | 32 | 1 | delete | dead | Panel confirmed×3. Only "caller" is a docstring example in component_tracker.py:173. mcp_server.py uses live `get_stats()`. |
| 8 | **MLModelRegistry.get_siglip** — image-embedding tier (engine is text-only) | models/ml_models.py (181-224, 388-393, docstring 19-20), models/__init__.py (`__all__`+`_LAZY_EXPORTS`), models/config.py (siglip field 63-64 + 400MB limit) | 60 | 1 | delete | dead | Panel confirmed×3. L20 caller is a **docstring** (verified). No image compressor exists; SIGLIP unreachable from compress(). Siblings `get_sentence_transformer`/`get_spacy` stay LIVE. Note: this trims a small models/`__all__` export but with confirmed 0 callers and no top-level re-export — Tier-1-eligible. |
| 9 | **CacheAlignerConfig** — 8 of 9 fields dead post detector-only refactor (PR-A2/P2-23) | headroom/config.py:27-100 | 55 | 1 | deadflag | dead | Panel confirmed×3. cache_aligner.py:254 reads ONLY `.enabled`. Reduce to single field. ⚠ update test fixtures constructing old fields. |
| 10 | **CCRConfig** — 7 of 9 fields never read | headroom/config.py:393-441 | 20 | 1 | deadflag | dead | Panel confirmed×3. smart_crusher.py:346 reads ONLY `.enabled` + `.inject_retrieval_marker`. Narrow to 2 fields. |
| 11 | **model_context_limits + get_context_limit()** | headroom/config.py:467-469,499-515 | 20 | 1 | deadflag | dead | Panel confirmed×3. No call site in any compressor. |
| 12 | **CacheOptimizerConfig class + HeadroomConfig.cache_optimizer** | headroom/config.py:368-389,472 | 25 | 1 | deadflag | dead | Panel confirmed×3. Live cache config is `CacheConfig` (cache/base.py:68). Co-deletes with cache-optimizer cluster (Tier 3 #2). |
| 13 | **PrefixFreezeConfig + prefix_freeze** | headroom/config.py:442-458,474 | 22 | 1 | deadflag | dead | Panel confirmed×3. Sole consumer is `archive/prefix_tracker.py` (excluded). |
| 14 | **HeadroomMode enum + default_mode** | headroom/config.py:13-18,466 | 8 | 1 | deadflag | dead | Panel confirmed×3. No mode dispatch in TransformPipeline.apply(). |
| 15 | **SemanticCacheLayer docstring cruft** | headroom/cache/__init__.py:13,20 | 8 | 1 | archaeology | dead | Verified: class exists ONLY in `archive/semantic.py` + this docstring example. Pure doc cruft referencing a removed class. No code edge. |
| 16 | **direct_mode param** — documented "Ignored (kept for compat)" | headroom/ccr/mcp_server.py:905 | 2 | 1 | deadflag | dead **(needs review)** | Panel confirmed×2/uncertain×1. Also the **vulture hit**. Body never reads it. ⚠ grep callers for positional `direct_mode=True` first. |
| 17 | **store_url field** | headroom/config.py:465 | 1 | 1 | deadflag | dead | Panel confirmed×3. Verified: declaration only, no live consumer. |
| 18 | **smart_crusher sub-config field** — ContentRouter bypasses it | headroom/config.py:470 | 1 | 1 | deadflag | dead | Panel confirmed×3. content_router.py:1641 builds fresh `SmartCrusherConfig()`. |
| 19 | **output_buffer_tokens** — docstring-only | headroom/config.py:479 | 3 | 1 | deadflag | dead | Panel confirmed×3. Only a pipeline.py docstring mention. |
| 20 | **pipeline_extensions + discover_pipeline_extensions** | headroom/config.py:496-497 | 2 | 1 | deadflag | dead | Panel confirmed×3. compress.py builds `PipelineExtensionManager(discover=False)` directly. |
| 21 | **content_router_enabled InitVar** — deprecated compat arg | headroom/config.py:484 | 4 | 1 | deadflag | dead **(needs review)** | Panel **refuted** by 1 of 3 reviewers. Verified InitVar present. ⚠ grep external HeadroomConfig callers passing this kwarg before removal. |
| 22 | **Rust `create_scorer` factory** — zero callers, not in pyo3 bridge | crates/headroom-core/src/relevance/mod.rs:38-82 | 33 | 1 | delete | dead | Panel confirmed×3. Verified: only def + doc comment; Python has its own `create_scorer`. |
| 23 | **Rust `ccr::compute_key`** — test-only | crates/headroom-core/src/ccr/mod.rs:69-76 | 8 | 1 | delete | test-only | Panel confirmed×3. All hits in `tests/ccr_backends.rs` + own `#[cfg(test)]`. SmartCrusher uses InMemoryCcrStore interface. |
| 24 | **Rust `ccr::marker_for`** — zero non-test callers | crates/headroom-core/src/ccr/mod.rs:81-83 | 4 | 1 | delete | dead | Panel confirmed×3. Verified: def + one `#[cfg(test)]` assert only. Not in pyo3 bridge. |
| 25 | **Rust `default_batch_score`** — test-only pub fn | crates/headroom-core/src/relevance/base.rs:70-79 | 10 | 1 | yagni | test-only | Only call site inside `#[cfg(test)]`. Remove `pub use` from relevance/mod.rs. |
| 26 | **Rust `SmartCrusher::builder()`** — pub entry, zero prod callers | crates/.../smart_crusher/crusher.rs:297-299 | 3 | 1 | yagni | test-only | `new()` calls `SmartCrusherBuilder::new()` directly. One `#[test]` caller (crusher.rs:1849). |
| 27 | **Rust `SmartCrusher::with_scorer`** — one-test convenience ctor | crates/.../smart_crusher/crusher.rs:305-316 | 12 | 1 | yagni | test-only | Only caller is `#[test]` crusher.rs:2042. (Builder-level `with_scorer` is live — different.) |

**Tier 1 subtotal: ~2,759 LOC** (of which ccr response_handler/context_tracker/batch_store = ~1,869 are the proxy-coupled CCR plane that becomes dead at step 3).

---

## TIER 2 — CUT AFTER UNTANGLE (vestigial + 1 co-requisite edge)

| # | What | Paths | ~LOC | Rung | Tag | Reachability | Exact untangle |
|---|---|---|---|---|---|---|---|
| 1 | **Duplicate measure.py** — byte-identical across verify/ + verify/heldout/ | verify/measure.py + verify/heldout/measure.py | 879 | 4 | dup | test-only | Both files diff-clean, 879 LOC each. **Consolidate to canonical verify/measure.py**; repoint 4 imports: `verify/heldout/worker.py:26`, `verify/heldout/run.py:~120`, `verify/heldout/strict_recheck.py`, `verify/heldout/encprobe.py` from `verify.heldout.measure` → `verify.measure`. (Counts one copy = 879.) |
| 2 | **SmartCrusherConfig.relevance + .anchor** in config.py — logic moved to Rust | headroom/config.py:102-200,295,345,348 | 80 | 2 ★ | deadflag | dead **(needs review)** | Live `SmartCrusherConfig` (smart_crusher.py) lacks these fields. **Untangle:** confirm Rust PyO3 bridge does not accept these as serialized config, then remove `RelevanceScorerConfig`/`AnchorConfig` from config.py. **★ unverified replacement — confirm edge-case parity at apply time** (leans on Rust twin). |

**Tier 2 subtotal: ~959 LOC.**

---

## TIER 3 — SURFACE DEPRECATION (public `__all__`/`_LAZY_EXPORTS`, ~0 runtime use — drop exports + docs + version bump, NOT a free delete)

| # | What (lenses) | Paths | ~LOC behind | Rung | Tag | Evidence |
|---|---|---|---|---|---|---|
| 1 | **anchor_selector.py** — Python twin of LIVE anchor_selector.rs (api-surface + cross-lang-dup + feature-reach-py) | headroom/transforms/anchor_selector.py | 770 | 3/5 ★ | surface | **In top-level `headroom/__init__.__all__` + transforms/`__init__` `_LAZY_EXPORTS`** (verified `transforms/__init__.py` references it; only other ref is `benchmarks/imp2_ab.py`). Never instantiated live; SmartCrusher delegates to Rust. Py↔Rust hash-parity invariant is enforced by the **Rust** `compute_item_hash` — no tests/ import the Python one (parity not touched). **★ unverified replacement — confirm edge-case parity at apply time.** Drop 6 symbols from both `__all__`s + delete benchmark import. |
| 2 | **Cache-optimizer cluster** — Anthropic/OpenAI/Google CacheOptimizer + Registry + base.py (feature-reach-py + api-surface + cross-lang-dup) | cache/anthropic.py(517)+openai.py(584)+google.py(884)+registry.py(175)+base.py(342) | 2,502 | 1/5 | surface | Live prefix work is `CacheAligner` (zero reference to CacheOptimizer/Registry). 7+ symbols in top-level `__all__`+`_LAZY_EXPORTS`. registry auto-registers on import but `.get()` has zero callers outside cache/. **Zero test coverage.** ⚠ `CacheOptimizerRegistry` auto-register pattern may imply external plugin consumers — audit before removal. Co-deletes config #12. |
| 3 | **code_compressor.py** — config-gated AST compressor, superseded by code-graph MCP (feature-reach-py + cross-lang-dup) | headroom/transforms/code_compressor.py + content_router CODE_AWARE branches + 7 transforms/__init__ entries + [code] extra + thread-safety test | 2,036 | 1/4 | deadflag | Double-gated OFF: `enable_code_aware=False` ("Disabled: use code graph MCP tools instead", content_router.py:450) + `prefer_code_aware_for_code=False`. CODE_AWARE always remaps to KOMPRESS. Both flags **user-flippable** → product retirement decision, not pure dead-code. In top-level `__all__`. Co-deletes compression_summary (Tier 3 #6). |
| 4 | **EmbeddingScorer + HybridScorer + create_scorer + embedding_available** (feature-reach-py + api-surface) | relevance/embedding.py + relevance/hybrid.py | 533 | 1/5 | surface | Only `BM25Scorer` is live (compression_store.py:48 direct import). 4 symbols in top-level `__all__`+`_LAZY_EXPORTS`. smart_crusher.py mentions HybridScorer in **comments** only. Removing also drops `[relevance]` extra (fastembed, numpy). |
| 5 | **cache/compression_cache.py** — CompressionCache, name-collides with content_router's local class (api-surface + feature-reach-py) | headroom/cache/compression_cache.py | 315 | 1/5 | surface **(needs review)** | In top-level `__all__` + cache/`_LAZY_EXPORTS`. content_router.py:193 defines its OWN local `CompressionCache` (independent). Only live importer: `tests/test_compression_cache.py`. ⚠ generic export name — audit external SDK consumers before removal. |
| 6 | **compression_summary.py** — sole prod caller is code_compressor.py:1058 (feature-reach-py + dead-feature) | headroom/transforms/compression_summary.py | 243 | 1 | dep | **Subsumes** `summarize_dropped_items` (80 LOC — do not add). Co-delete in the **same** changeset as code_compressor (Tier 3 #3) to avoid broken import. |
| 7 | **SimulationResult + RequestMetrics** — config.py dataclasses, 0 uses outside `__all__` | headroom/config.py | 60 | 1 | surface | Proxy was only plausible consumer (condemned). In top-level `__all__`+`_LAZY_EXPORTS`. |
| 8 | **providers/base.py:Provider ABC** — factory-of-zero, no concrete subclasses | headroom/providers/base.py | 45 | 5 | yagni | In providers/`_LAZY_EXPORTS`. Only used as a TYPE_CHECKING param annotation in pipeline.py. `TokenCounter` Protocol in same file IS live — keep. Retype pipeline param to `object \| None`. |
| 9 | **RelevanceScorerConfig + CacheOptimizerConfig** — exported config types rejected/ignored at runtime | config.py + smart_crusher.py:291-298 | 35 | 4 | deadflag | smart_crusher raises `NotImplementedError` if `relevance_config`/`scorer` passed. `cache_optimizer_config` value never read. API break — major bump or deprecation. |
| 10 | **CompressionObserver Protocol** — structural Protocol, duck-typed | headroom/transforms/observability.py | 77 | 6 | surface **(needs review)** | Absence of runtime import is **expected** for a structural Protocol — low confidence (0.6). In transforms/`__all__`+`_LAZY_EXPORTS`. ⚠ check mypy/pyright stubs reference it before removal. |
| 11 | **Rust transforms/mod.rs re-exports** — LogLine/FileMatches/SearchMatch/ProtectStats, no external consumer | crates/.../transforms/mod.rs:44-53 | 4 | 4 | surface | Zero imports in headroom-py or downstream crates. Types stay accessible via origin modules. |

**Tier 3 subtotal: ~6,420 LOC behind the surface (NOT free — coordinated deprecation + version bump).**

---

## TIER 0 — ARCHAEOLOGY (dup, dead tests, doc cruft, stray artifacts)

| # | What | Paths | ~LOC | Tag | Note |
|---|---|---|---|---|---|
| 1 | **Dead tests for cut features** | tests/test_compression_units.py, tests/test_ccr_eviction_loud_miss.py, tests/test_adaptive_sizer_parity.py | — | dead-test | Delete alongside their cut targets. `test_ccr_eviction_loud_miss` asserts the **loud-miss recovery invariant** — migrate to mcp_server retrieve path before deletion. |
| 2 | **ast-grep-cli core dep** — declared for CodeCompressor but unused (it uses tree-sitter) | pyproject.toml:51 + tools.json:94 | 0 | deadflag **(needs review)** | No execution site. tools.json entry is for `headroom tools doctor` reporting. ⚠ keep unless doctor command is also cut; possibly demote to optional extra. (conf 0.5) |
| 3 | (already counted) SemanticCacheLayer docstring — see Tier 1 #15 | — | — | — | Listed in Tier 1 (no code edge). |

**Tier 0 net new: doc cruft + dead-test removal (~130 LOC of tests, dep tidy).**

---

## DECISION-GATE ROADMAP (NOT a cut tier — `safe_to_cut_now=false`, delete ONLY if roadmap formally abandoned)

These are feature-gated / explicitly-documented roadmap infra. They are **invisible to cargo** (never compiled in the default wheel) — pure reachability findings. Do NOT inflate Tier 1 with these.

| What | Paths | ~LOC | Gate |
|---|---|---|---|
| **Rust SQLite + Redis CCR backends + from_config** (rust-reach + cross-lang-dup — **one cluster, dedup'd**) | crates/.../ccr/backends/sqlite.rs(205)+redis.rs(146)+mod.rs from_config + ccr/mod.rs:36 + Cargo.toml rusqlite/redis | ~450 | **(needs review)** Panel refuted×1/uncertain×1. Test-only; mod.rs doc: "shape the proxy will pass once Phase C wires config" — Phase C targets the condemned proxy. `InMemoryCcrStore` is LIVE (HARD INVARIANT, keep). Cut when Phase C abandoned + remove rusqlite hard dep. |
| **magika_detector.rs** — off-by-default ONNX classifier | crates/.../transforms/magika_detector.rs | 344 | `default=[]`, absent from shipped wheel. Tier-2/3 fallback (unidiff/keyword) is live. Same framing as embeddings-gated items. |
| **SmartCrusherConfig moved-to-Rust** — see Tier 2 #2 | — | — | listed in Tier 2. |
| **Rust `signals::Tiered<T>`** — documented ML-tier-stacking roadmap | crates/.../signals/tiered.rs | 70 | Zero prod instantiations; explicitly roadmap infra. |
| **Rust embeddings ctors** — `EmbeddingScorer::try_new`, `HybridScorer::with_scorers` | relevance/embedding.rs, hybrid.rs | 35 | Behind `#[cfg(feature="embeddings")]`, `default=[]`. Intended prod entry points once feature opt-in. Decision gate with Python EmbeddingScorer. |
| **_create_default_ccr_backend** — entry-point plugin loader, no group registered | cache/compression_store.py:1209-1239 | 32 | Reads `HEADROOM_CCR_BACKEND` (never set), `entry_points(group="headroom.ccr_backend")` (no group in pyproject) → always returns None. Deliberate unwired extension seam. |
| **CompressionPolicy F2.2 fields** — `volatile_token_threshold`, `max_lossy_ratio` | transforms/compression_policy.py:103,111 | 15 | Self-documented "Plumbed but unconsumed in F2.2". Parity tests reference them — keep until F2.2 decided. |

---

## TRAP — DO NOT CUT (hard invariant)

| What | Paths | ~LOC | Why kept |
|---|---|---|---|
| **csv_schema_decoder.py** — byte-exact reverse decoder | headroom/transforms/csv_schema_decoder.py | 591 | **(needs review)** Verifies a HARD INVARIANT (default lossless decoder / 100% byte-exact CCR recovery). Zero prod callers but `tests/test_ccr_recovery_invariant.py:36` + verify/ + benchmarks/ depend on it to PROVE the live Rust CSV-schema encoder is reversible. At most relocate to a tests/ fixture — **must NOT be deleted while the invariant test depends on it.** 591 LOC looks juicy; it is not free. |

---

## Refuted appendix — looked dead, is live (this pass earning its keep)

| What | Why it survived | Evidence |
|---|---|---|
| **KompressCompressor** (kompress_compressor.py) | deptry DEP001 torch/safetensors is a **false positive** | LIVE default fallback for TEXT/KOMPRESS/SOURCE_CODE via `ContentRouter._try_ml_compressor()`. torch/safetensors are optional extras (ONNX path via onnxruntime+transformers), guarded by `is_kompress_available()` try/except. Graceful passthrough if absent. |
| **ComponentTracker** (component_tracker.py) | deptry psutil hit is a **false positive**; v1-scout "unreferenced" note was stale | LIVE callers: compression_store.py:51 + batch_store.py:21 (`from ..component_tracker import ComponentStats`) — runtime method-body imports, not TYPE_CHECKING. psutil is try/except-guarded. |
| **content_router_enabled InitVar** | 1 of 3 reviewers **refuted** the cut | Kept in Tier 1 with **(needs review)** — grep external HeadroomConfig callers passing the kwarg before removal. |
| **Rust SqliteCcrStore cluster** | panel refuted×1/uncertain×1 | Moved to Decision-Gate (roadmap), not a cut tier. `InMemoryCcrStore` is the live store (hard invariant). |

---

## OPTIONAL — high-churn, NOT recommended (complexity-lint quarantine)

Pure line-level churn on a hardened, invariant-bound engine — **not** a cut, regression risk outweighs value:
- 29 × RUF100 (unused-noqa)
- 5 × ERA001 (commented-out — prior pass found 6/7 ERA hits were false-positive comment-headers; per-case check needed)

(Note: `direct_mode` is a **real Tier-1 row** #16 — it is also the vulture hit — NOT quarantine.)

---

## NOT-RUN lenses (coverage gaps)

`[]` — no lens reported a coverage gap. All lenses (feature-reach-py, rust-reach, api-surface, dup, cross-lang-dup, dead-config, dead) returned findings.

## What v4 found that passes 1–3 missed

1. **Two deptry false-positives** confirmed live (KompressCompressor, ComponentTracker) — prevents a 9.6k-context mistake.
2. **The full surface-deprecation tail quantified**: ~6.4k LOC behind public `__all__`/`_LAZY_EXPORTS` that *imports clean* (tools blind) but is never instantiated — anchor_selector (770), compression_cache (315), Provider ABC, observability Protocol are **new** surface beyond the already-flagged cache-optimizer/embedding sets.
3. **Cross-language duplication map**: Python twins (anchor_selector, adaptive_sizer) shadowing live Rust; Rust persistence tier (sqlite/redis) shadowing live Python CompressionStore — none wired.
4. **The csv_schema_decoder invariant trap** — 591 LOC that *looks* dead but guards byte-exact recovery.
5. **Honest verdict: the tree is now essentially lean.** Tier-1-safe-now is ~8% and mostly proxy-coupled CCR plumbing that dies at step 3. The remaining fat is gated behind product/API decisions, not free deletes.

---

**REPORT-ONLY.** Applying any row is a separate gated archive+test step; proxy/ is excluded (condemned in step 3).