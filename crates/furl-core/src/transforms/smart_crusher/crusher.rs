//! `SmartCrusher` struct — top-level entry point for compression.
//!
//! Owns the `config`, `anchor_selector`, `scorer`, and `analyzer`
//! singletons that every per-message call needs. Constructed once
//! per process; the struct is `Send + Sync` so it can sit behind an
//! `Arc` in a multi-threaded engine.
//!
//! Together with its sibling submodules this ports three Python entry
//! points (ARCH-4: one `impl SmartCrusher` block per concern, split as
//! pure moves with zero behavior change):
//!
//! - `_execute_plan` (line 3617) → `SmartCrusher::execute_plan` (here)
//! - `_crush_array`  (line 2400) → `SmartCrusher::crush_array` (`route`)
//! - `_crush_mixed_array` (line 2914) → `SmartCrusher::crush_mixed_array`
//!   (`walk`)
//!
//! This file is the rump: the `SmartCrusher`/`CrushArrayResult` types,
//! the constructors, and `execute_plan`. The recursive JSON walk lives
//! in `walk.rs`, lossless/lossy routing in `route.rs`, and the
//! CCR persist/sentinel invariants in `persist.rs`.
//!
//! # Stubs that match Python's "everything-disabled" path
//!
//! Python's `_crush_array` historically called into cross-user pattern
//! learning, per-tool feedback hints, CCR (compress-cache-retrieve
//! store), and telemetry. The learning/feedback/telemetry systems have
//! since been deleted from the Python side; CCR is the one that
//! remains live. The like-for-like port at Stage 3c.1 mirrored
//! Python's behavior **with those subsystems disabled**, which is now
//! simply the behavior:
//!
//! - **Learned recommendations**: never produced; nothing overrides
//!   `effective_max_items` or injects preserve_fields/strategy/level.
//! - **Feedback hints**: never produced; default `effective_max_items`.
//! - **CCR**: wired separately (live) — see `ccr_store()`.
//! - **`_compress_text_within_items`**: pass-through (returns input
//!   unchanged) since text compression has its own port pipeline.
//! - **`summarize_dropped_items`**: empty string.
//!
//! Parity fixtures were recorded with those subsystems disabled on the
//! Python side, locking byte-equal output.

use std::sync::Arc;

use serde_json::Value;

use super::analyzer::SmartAnalyzer;
use super::builder::SmartCrusherBuilder;
use super::compaction::CompactionStage;
use super::config::SmartCrusherConfig;
use super::planning::SmartCrusherPlanner;
use super::types::{CompressionPlan, DroppedRef};
use crate::ccr::CcrStore;
use crate::relevance::RelevanceScorer;
use crate::transforms::anchor_selector::AnchorSelector;

/// Return type for `crush_array`.
///
/// Two operating paths feed the same result type:
///
/// - **Lossless path** — input compacted to a smaller inline form
///   (e.g. CSV+schema). Nothing dropped; `compacted` is populated;
///   `ccr_hash` is `None` (no retrieval needed because everything is
///   already in the prompt).
/// - **Lossy path** — input compressed by row-dropping. `items` holds
///   the kept subset; `ccr_hash` is `Some(hash)` so the runtime can
///   cache the **full original** keyed by that hash and serve it back
///   to the LLM via a retrieval tool call. **No data is lost** —
///   "lossy" here means "compressed view inline; full payload cached
///   for tool retrieval," matching Python's CCR-Dropped semantics.
///
/// The runtime (PyO3 bridge) owns the cache; this crate
/// computes the hash and emits a marker so the prompt knows where to
/// look.
pub struct CrushArrayResult {
    /// Kept items. For the lossless path this is the full original
    /// (nothing was dropped). For the lossy path this is the surviving
    /// subset; the rest is retrievable via `ccr_hash`.
    pub items: Vec<Value>,
    /// Strategy debug string. One of:
    /// - `"none:adaptive_at_limit"` / `"skip:<reason>"` — passthrough
    /// - `"lossless:table"` — lossless wins (always `table`: the accept
    ///   gates are restricted to decoder-verifiable flat tables until
    ///   the reference decoder covers `Buckets`/`Nested` — COR-13)
    /// - `"smart_sample"` / `"top_n"` / `"cluster"` / `"time_series"` —
    ///   lossy path with row-dropping.
    pub strategy_info: String,
    /// 12-char SHA-256 hex prefix of the **full original input**.
    /// Populated when the lossy path dropped rows; the runtime is
    /// expected to cache the original items keyed by this hash so a
    /// retrieval tool can serve them back. `None` when nothing was
    /// dropped (lossless path or below adaptive_k boundary).
    pub ccr_hash: Option<String>,
    /// Marker text inserted into the prompt to advertise the CCR
    /// pointer (e.g. `<<ccr:abc123def456 42_rows_offloaded>>`). Empty
    /// when `ccr_hash` is `None`.
    pub dropped_summary: String,
    /// Rendered bytes from the compaction stage when the **lossless
    /// path** won. `None` for the lossy path or when compaction wasn't
    /// configured.
    pub compacted: Option<String>,
    /// Top-level [`Compaction`](super::compaction::Compaction) variant
    /// tag. Mirrors `compacted` —
    /// populated only when lossless won, and always `"table"` today:
    /// `"buckets"`/`"ccr"` shapes are declined from the lossless tier
    /// until the reference decoder covers them (COR-13).
    pub compaction_kind: Option<&'static str>,
    /// Compact granular-retrieval marker (`<<ccr:HASH#rows N_chunks>>`)
    /// carried alongside the whole-blob `dropped_summary`. Surfaced in
    /// the `_ccr_rows` field of the `{"_ccr_dropped": ...}` sentinel so a
    /// consumer can resolve the per-blob row index and retrieve ONE row
    /// at a time instead of paying for the whole offloaded blob. `None`
    /// when nothing was dropped or no store was configured.
    pub row_index_marker: Option<String>,
    /// Typed recovery refs for THIS result's shipped render (§4.2): the
    /// row-drop ref mirroring `ccr_hash`/`row_index_marker` plus — when
    /// the compacted render carries `<<ccr:HASH,KIND,SIZE>>`
    /// substitutions — one [`DroppedRef::Opaque`] per substitution, in
    /// render order. Pure side-output: the values are exactly those the
    /// emission sites already computed, so every rendered byte is
    /// identical to before this field existed. Empty when the result is
    /// a passthrough / pure-lossless render with no substitutions.
    pub dropped_refs: Vec<DroppedRef>,
}

/// Top-level SmartCrusher.
///
/// Pluggable extension:
/// - `scorer` — relevance scoring (`HybridScorer` by default).
///
/// Error-item and structural-outlier preservation are hardwired into
/// the planner (see `planning.rs`); they are no longer pluggable.
///
/// Compose via [`SmartCrusherBuilder`]; or call `SmartCrusher::new()`
/// for the OSS default composition.
pub struct SmartCrusher {
    pub config: SmartCrusherConfig,
    pub anchor_selector: AnchorSelector,
    pub scorer: Box<dyn RelevanceScorer + Send + Sync>,
    pub analyzer: SmartAnalyzer,
    /// Optional lossless-first compaction stage. When
    /// set, `crush_array` runs compaction up front and short-circuits
    /// the lossy path on success. When `None` (default OSS), parity
    /// with the lossy-only pipeline is preserved exactly.
    pub compaction: Option<CompactionStage>,
    /// Optional CCR store. When set, the lossy path stashes the **full
    /// original** array into the store keyed by `ccr_hash` before
    /// returning — the runtime can then serve dropped rows back via
    /// retrieval tool calls. When `None`, hashes are still emitted but
    /// nothing is stored (legacy / parity mode).
    ///
    /// `Arc` so callers can keep their own handle to the same store
    /// (e.g. the runtime holds it for retrieval lookups while
    /// SmartCrusher writes through it).
    pub ccr_store: Option<Arc<dyn CcrStore>>,
    /// Tokenizer used by the `MinTokens` routing policy to size the two
    /// candidate renderings (lossless vs lossy-recoverable) of a
    /// compressible array. Bytes mislead — a fewer-byte render can
    /// tokenize larger (hex vs base64) — so the routing choice is made
    /// on real token counts. Defaults to a `gpt-4o` tiktoken counter
    /// (the engine's benchmark model); the absolute model is immaterial
    /// to the CHOICE since only the relative ranking of the two renders
    /// matters, and tiktoken is the honest, deterministic metric.
    pub tokenizer: Box<dyn crate::tokenizer::Tokenizer>,
}

impl SmartCrusher {
    /// Construct with the OSS default composition: scorer +
    /// **lossless-first compaction stage**. Calling
    /// `crush_array` runs the dispatch:
    ///
    /// 1. Try the lossless compactor.
    /// 2. If savings ratio ≥ `config.lossless_min_savings_ratio`
    ///    (default `0.30`), ship lossless — `compacted` populated,
    ///    `ccr_hash = None`, nothing dropped.
    /// 3. Otherwise fall through to the lossy path — drop rows,
    ///    populate `ccr_hash` with a hash of the full original so the
    ///    runtime can cache the payload for tool retrieval.
    ///
    /// **No data is ever lost.** The lossy path moves dropped rows to
    /// CCR cache, not to nowhere — same semantics as Python's
    /// SmartCrusher with CCR enabled.
    pub fn new(config: SmartCrusherConfig) -> Self {
        SmartCrusherBuilder::new(config)
            .with_default_oss_setup()
            .with_default_compaction()
            .with_default_ccr_store()
            .build()
    }

    /// Construct WITHOUT the compaction stage:
    /// `crush_array` skips the lossless attempt and runs the lossy
    /// path directly (still with CCR-Dropped retrieval markers).
    /// Used by:
    ///
    /// - The 17 legacy parity fixtures (recorded against the
    ///   lossy-only path; using this constructor preserves byte-equal
    ///   coverage).
    /// - Callers who explicitly don't want lossless attempts (e.g.
    ///   workloads where the compactor's overhead isn't worth the
    ///   modest tabular wins).
    pub fn without_compaction(config: SmartCrusherConfig) -> Self {
        SmartCrusherBuilder::new(config)
            .with_default_oss_setup()
            .with_default_ccr_store()
            .build()
    }

    /// Construct like [`SmartCrusher::new`] but with the compaction
    /// stage's formatter chosen by name (`"csv-schema"`, `"json"`,
    /// `"markdown-kv"`). `None` for unknown names — callers own the
    /// fallback/error policy. `"csv-schema"` is equivalent to `new`.
    pub fn with_compaction_format(config: SmartCrusherConfig, format_name: &str) -> Option<Self> {
        let stage = CompactionStage::from_format_name(format_name)?;
        Some(
            SmartCrusherBuilder::new(config)
                .with_default_oss_setup()
                .with_compaction(stage)
                .with_default_ccr_store()
                .build(),
        )
    }

    /// Construct directly from owned parts. Used by
    /// [`SmartCrusherBuilder::build`] — not part of the public stable
    /// API. Prefer the builder.
    #[doc(hidden)]
    #[allow(clippy::too_many_arguments)]
    pub fn from_parts(
        config: SmartCrusherConfig,
        anchor_selector: AnchorSelector,
        scorer: Box<dyn RelevanceScorer + Send + Sync>,
        analyzer: SmartAnalyzer,
        compaction: Option<CompactionStage>,
        ccr_store: Option<Arc<dyn CcrStore>>,
        tokenizer: Box<dyn crate::tokenizer::Tokenizer>,
    ) -> Self {
        SmartCrusher {
            config,
            anchor_selector,
            scorer,
            analyzer,
            compaction,
            ccr_store,
            tokenizer,
        }
    }

    /// Handle to the CCR store, if configured. Used by the runtime
    /// (PyO3 bridge) to look up originals when retrieval
    /// tool calls fire.
    pub fn ccr_store(&self) -> Option<&Arc<dyn CcrStore>> {
        self.ccr_store.as_ref()
    }

    pub(super) fn planner(&self) -> SmartCrusherPlanner<'_> {
        SmartCrusherPlanner::new(
            &self.config,
            &self.anchor_selector,
            &*self.scorer,
            &self.analyzer,
        )
    }

    /// Execute a `CompressionPlan` against `items`, returning the
    /// kept-items list in original-array order. Mirrors Python's
    /// `_execute_plan` (line 3617-3633).
    ///
    /// Schema-preserving: each kept item is cloned unchanged. No
    /// summary objects, generated fields, or wrapper metadata.
    pub fn execute_plan(&self, plan: &CompressionPlan, items: &[Value]) -> Vec<Value> {
        let mut indices = plan.keep_indices.clone();
        indices.sort_unstable();
        indices
            .into_iter()
            .filter(|&idx| idx < items.len())
            .map(|idx| items[idx].clone())
            .collect()
    }
}

/// Shared test fixtures for the smart-crusher submodules' co-located
/// suites (`walk` / `route` / `persist` and this module's own tests).
/// Bodies are byte-identical to the pre-split fixtures; only the
/// fn-local `use` statements were hoisted to module imports.
#[cfg(test)]
pub(super) mod test_support {
    use std::sync::Arc;

    use super::super::builder::SmartCrusherBuilder;
    use super::super::config::SmartCrusherConfig;
    use super::SmartCrusher;
    use crate::ccr::{CcrStore, InMemoryCcrStore};

    pub(crate) fn crusher() -> SmartCrusher {
        SmartCrusher::new(SmartCrusherConfig::default())
    }

    /// Build a default-config crusher with an in-memory CCR store
    /// attached, returning both so tests can inspect the store.
    pub(crate) fn crusher_with_store() -> (SmartCrusher, Arc<InMemoryCcrStore>) {
        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
            .with_default_oss_setup()
            .with_default_compaction()
            .with_ccr_store(store_dyn)
            .build();
        (c, store)
    }

    /// Build a store-backed crusher with `lossless_only` plus any extra
    /// config the test needs, returning the concrete store handle so
    /// tests can assert it never grows.
    pub(crate) fn lossless_only_crusher(
        cfg: SmartCrusherConfig,
    ) -> (SmartCrusher, Arc<InMemoryCcrStore>) {
        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        let c = SmartCrusherBuilder::new(cfg)
            .with_default_oss_setup()
            .with_default_compaction()
            .with_ccr_store(store_dyn)
            .build();
        (c, store)
    }
}

#[cfg(test)]
mod tests {
    use super::test_support::crusher;
    use super::*;
    use serde_json::json;

    // ---------- execute_plan ----------

    #[test]
    fn execute_plan_empty_indices_returns_empty() {
        let c = crusher();
        let plan = CompressionPlan::default();
        let items: Vec<Value> = (0..5).map(|i| json!({"id": i})).collect();
        let result = c.execute_plan(&plan, &items);
        assert!(result.is_empty());
    }

    #[test]
    fn execute_plan_returns_items_in_sorted_index_order() {
        let c = crusher();
        let items: Vec<Value> = (0..10).map(|i| json!({"id": i})).collect();
        let plan = CompressionPlan {
            keep_indices: vec![5, 2, 8, 0],
            ..CompressionPlan::default()
        };
        let result = c.execute_plan(&plan, &items);
        assert_eq!(result.len(), 4);
        assert_eq!(result[0]["id"], 0);
        assert_eq!(result[1]["id"], 2);
        assert_eq!(result[2]["id"], 5);
        assert_eq!(result[3]["id"], 8);
    }

    #[test]
    fn execute_plan_skips_out_of_bounds() {
        let c = crusher();
        let items: Vec<Value> = (0..3).map(|i| json!({"id": i})).collect();
        let plan = CompressionPlan {
            keep_indices: vec![0, 5, 2],
            ..CompressionPlan::default()
        };
        let result = c.execute_plan(&plan, &items);
        assert_eq!(result.len(), 2);
    }

    #[test]
    fn crusher_construction_default() {
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        assert_eq!(c.config.max_items_after_crush, 15);
    }

    #[test]
    fn crusher_with_custom_scorer() {
        use crate::relevance::BM25Scorer;
        let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
            .with_scorer(Box::new(BM25Scorer::default()))
            .build();
        // Sanity: crushing still works with a swapped scorer.
        let items: Vec<Value> = (0..30).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "anything", 1.0);
        assert!(result.items.len() <= 30);
    }
}
