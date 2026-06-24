//! `SmartCrusher` struct — top-level entry point for compression.
//!
//! Owns the `config`, `anchor_selector`, `scorer`, and `analyzer`
//! singletons that every per-message call needs. Constructed once
//! per process; the struct is `Send + Sync` so it can sit behind an
//! `Arc` in a multi-threaded engine.
//!
//! This module ports three Python entry points:
//!
//! - `_execute_plan` (line 3617) → `SmartCrusher::execute_plan`
//! - `_crush_array`  (line 2400) → `SmartCrusher::crush_array`
//! - `_crush_mixed_array` (line 2914) → `SmartCrusher::crush_mixed_array`
//!
//! # Stubs that match Python's "everything-disabled" path
//!
//! Python's `_crush_array` calls into TOIN (cross-user pattern
//! learning), feedback (per-tool compression hints), CCR (compress-
//! cache-retrieve store), and telemetry. All four are large separate
//! systems with their own state. For the like-for-like port at Stage
//! 3c.1, we mirror Python's behavior **when those subsystems are
//! disabled**:
//!
//! - **TOIN**: never produces a recommendation, never overrides
//!   `effective_max_items`, never injects preserve_fields/strategy/level.
//! - **Feedback**: never produces hints; default `effective_max_items`.
//! - **CCR**: `enabled=false`; result has `ccr_hash = None`.
//! - **Telemetry**: no-op.
//! - **`_compress_text_within_items`**: pass-through (returns input
//!   unchanged) since text compression has its own port pipeline.
//! - **`summarize_dropped_items`**: empty string.
//!
//! Parity fixtures are recorded with all four disabled on the
//! Python side, locking byte-equal output. The TOIN/CCR/feedback
//! integration ports are handled separately.

use std::sync::Arc;

use serde_json::Value;

use super::analyzer::SmartAnalyzer;
use super::builder::SmartCrusherBuilder;
use super::classifier::{classify_array, ArrayType};
use super::field_role::compute_exclude_set;
use super::compaction::{
    classify_cell, emit_opaque_ccr_marker, try_parse_json_container, CellClass, ClassifyConfig,
    Compaction, CompactionStage,
};
use super::config::{RoutingPolicy, SmartCrusherConfig};
use super::crushers::{compute_k_split, crush_number_array, crush_object, crush_string_array};
use super::planning::SmartCrusherPlanner;
use super::traits::{Constraint, CrushEvent, Observer};
use super::types::{ArrayAnalysis, CompressionPlan, CompressionStrategy, CrushResult};
use crate::ccr::CcrStore;
use crate::relevance::RelevanceScorer;
use crate::transforms::adaptive_sizer::compute_optimal_k;
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
    /// - `"lossless:table"` / `"lossless:buckets"` — lossless wins
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
    /// Top-level [`Compaction`] variant tag — `"table"`, `"buckets"`,
    /// `"ccr"`. Mirrors `compacted` — populated only when lossless won.
    pub compaction_kind: Option<&'static str>,
    /// Compact granular-retrieval marker (`<<ccr:HASH#rows N_chunks>>`)
    /// carried alongside the whole-blob `dropped_summary`. Surfaced in
    /// the `_ccr_rows` field of the `{"_ccr_dropped": ...}` sentinel so a
    /// consumer can resolve the per-blob row index and retrieve ONE row
    /// at a time instead of paying for the whole offloaded blob. `None`
    /// when nothing was dropped or no store was configured.
    pub row_index_marker: Option<String>,
}

/// Build the `{"_ccr_dropped": "<<ccr:HASH N_rows_offloaded>>",
/// "_ccr_rows": "<<ccr:HASH#rows N_chunks>>"}` sentinel object.
///
/// `_ccr_dropped` carries the byte-stable whole-blob recovery pointer
/// (unchanged — the single-blob retrieve path + parity hash still hold).
/// `_ccr_rows`, when present, is ONE compact marker naming the per-blob
/// row index so retrieval is PROPORTIONAL: the consumer resolves the
/// index, then fetches only the row(s) it needs (each `ccr_get(row_hash)`
/// returns just that single row) instead of the whole offloaded payload.
/// It is a single short string — surfacing it costs ~no prompt tokens
/// and does not perturb the lossy-vs-lossless token routing. `_ccr_rows`
/// is omitted when absent (no store configured), keeping the no-store
/// sentinel byte-identical to the historical shape.
fn ccr_sentinel_map(
    dropped_summary: &str,
    row_index_marker: Option<&str>,
) -> serde_json::Map<String, Value> {
    let mut sentinel = serde_json::Map::new();
    sentinel.insert(
        "_ccr_dropped".to_string(),
        Value::String(dropped_summary.to_string()),
    );
    if let Some(idx) = row_index_marker {
        sentinel.insert("_ccr_rows".to_string(), Value::String(idx.to_string()));
    }
    sentinel
}

/// Build the full sentinel `Value::Object` from a [`DroppedPersist`].
fn build_ccr_sentinel(persisted: &DroppedPersist) -> Value {
    Value::Object(ccr_sentinel_map(
        &persisted.marker,
        persisted.row_index_marker.as_deref(),
    ))
}

/// Result of [`SmartCrusher::persist_dropped`] — the CCR hash that keys
/// the stored full-original array plus the prompt-visible recovery
/// pointer text. `Some(_)` only when rows were actually dropped; the
/// store write (when a store is configured) has already happened by the
/// time this is returned.
struct DroppedPersist {
    /// 12-char SHA-256 hex prefix of the canonical full-original array.
    /// Always returned when something was dropped — callers may mirror
    /// or retrieve it.
    hash: String,
    /// `<<ccr:HASH N_rows_offloaded>>` recovery pointer. ALWAYS
    /// non-empty when rows were dropped (Defect 1): the pointer is the
    /// recovery key, not a UX flag, so it is surfaced unconditionally on
    /// every drop. The store write is likewise unconditional.
    marker: String,
    /// Compact GRANULAR-retrieval marker, e.g.
    /// `<<ccr:9f3a2b#rows 50_chunks>>`. ONE short string (not a list), so
    /// surfacing it costs ~no prompt tokens and never flips the
    /// lossy-vs-lossless routing decision. It names the per-blob ROW
    /// INDEX entry (`{hash}#rows`) the store now holds: a JSON array of
    /// the per-row hashes. The retrieval layer resolves it to fetch only
    /// the needed row(s) — `ccr_get(row_hash)` returns a 1-element array
    /// holding exactly that row — instead of paying for the whole blob.
    /// `None` when no store is configured to chunk into (the array hash +
    /// whole-blob `marker` still cover recovery in that case).
    row_index_marker: Option<String>,
}

/// Result of the lossy-recoverable render attempt in
/// [`SmartCrusher::crush_array_lossy`].
///
/// The routing layer needs to tell two cases apart:
/// - **Crushed** — a real DROP render exists (rows offloaded to CCR + a
///   surfaced `<<ccr:HASH>>` pointer). This is the candidate the
///   `MinTokens` policy sizes against the lossless render.
/// - **Skip** — the analyzer refused to crush the array (e.g. all-unique
///   entities with no signal). There is NO drop alternative; the carried
///   `CrushArrayResult` is the `skip:<reason>` passthrough, shipped only
///   when there's also no lossless render.
enum LossyOutcome {
    Crushed(CrushArrayResult),
    Skip(CrushArrayResult),
}

/// Top-level SmartCrusher.
///
/// Three pluggable extensions:
/// - `scorer` — relevance scoring (`HybridScorer` by default).
/// - `constraints` — must-keep predicates (`KeepErrorsConstraint` +
///   `KeepStructuralOutliersConstraint` by default).
/// - `observers` — decision-stream telemetry (`TracingObserver` by
///   default).
///
/// Compose via [`SmartCrusherBuilder`]; or call `SmartCrusher::new()`
/// for the OSS default composition.
pub struct SmartCrusher {
    pub config: SmartCrusherConfig,
    pub anchor_selector: AnchorSelector,
    pub scorer: Box<dyn RelevanceScorer + Send + Sync>,
    pub analyzer: SmartAnalyzer,
    pub constraints: Vec<Box<dyn Constraint>>,
    pub observers: Vec<Box<dyn Observer>>,
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
    /// Construct with the OSS default composition: scorer + constraints +
    /// observer + **lossless-first compaction stage**. Calling
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
        constraints: Vec<Box<dyn Constraint>>,
        observers: Vec<Box<dyn Observer>>,
        compaction: Option<CompactionStage>,
        ccr_store: Option<Arc<dyn CcrStore>>,
        tokenizer: Box<dyn crate::tokenizer::Tokenizer>,
    ) -> Self {
        SmartCrusher {
            config,
            anchor_selector,
            scorer,
            analyzer,
            constraints,
            observers,
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

    fn planner(&self) -> SmartCrusherPlanner<'_> {
        SmartCrusherPlanner::new(
            &self.config,
            &self.anchor_selector,
            &*self.scorer,
            &self.analyzer,
            &self.constraints,
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

    /// Top-level entry point. Mirrors Python `SmartCrusher.crush`
    /// (line 1581-1603) — used by `ContentRouter` when routing JSON
    /// arrays.
    ///
    /// Parses `content` as JSON, recursively processes it (compressing
    /// arrays at every depth via the appropriate per-type crusher),
    /// then re-serializes with Python-compatible formatting (`, ` and
    /// `: ` separators, ASCII-escaped non-ASCII).
    ///
    /// Returns a `CrushResult` with:
    /// - `compressed`: the re-serialized JSON.
    /// - `original`: the input string (unmodified).
    /// - `was_modified`: whether `compressed` differs from `content`'s
    ///   trimmed form.
    /// - `strategy`: combined strategy info from all crushed arrays
    ///   (or `"passthrough"`).
    pub fn crush(&self, content: &str, query: &str, bias: f64) -> CrushResult {
        let start = std::time::Instant::now();
        let (compressed, was_modified, info) = self.smart_crush_content(content, query, bias);
        let strategy = if info.is_empty() {
            "passthrough".to_string()
        } else {
            info
        };

        // Fire one event per top-level crush. Cheap when no observers
        // are configured (`for o in &[]` is a single null-pointer
        // check); cheap when only `TracingObserver` is configured if
        // the subscriber filters `debug` out (the default in
        // production). Custom observers — audit logs, Loop training
        // stream, metrics — pay whatever they pay.
        if !self.observers.is_empty() {
            let event = CrushEvent {
                strategy: strategy.clone(),
                input_bytes: content.len(),
                output_bytes: compressed.len(),
                elapsed_ns: start.elapsed().as_nanos() as u64,
                was_modified,
            };
            for observer in &self.observers {
                observer.on_event(&event);
            }
        }

        CrushResult {
            compressed,
            original: content.to_string(),
            was_modified,
            strategy,
        }
    }

    /// `SmartCrusher._smart_crush_content` (Python line 2243-2301).
    /// JSON-parse, recursively process, re-serialize. CCR marker
    /// injection is stubbed (CCR is disabled in this stage).
    ///
    /// Returns `(crushed_content, was_modified, info)`.
    pub fn smart_crush_content(
        &self,
        content: &str,
        query_context: &str,
        bias: f64,
    ) -> (String, bool, String) {
        // Parse — non-JSON content passes through unchanged.
        let Ok(parsed) = serde_json::from_str::<Value>(content) else {
            return (content.to_string(), false, String::new());
        };

        let (crushed, info) = self.process_value(&parsed, 0, query_context, bias);

        // Re-serialize with Python `safe_json_dumps` formatting:
        // compact `(",", ":")` separators + `ensure_ascii=False`,
        // preserving object-key insertion order. Matches the Python
        // SmartCrusher output bytes exactly.
        let result = crate::transforms::anchor_selector::python_safe_json_dumps(&crushed);
        let was_modified = result != content.trim();
        (result, was_modified, info)
    }

    /// Maximum recursion depth for nested JSON. Mirrors Python's
    /// `_MAX_PROCESS_DEPTH = 50`. Beyond this, values are returned as-is.
    const MAX_PROCESS_DEPTH: usize = 50;

    /// Recursively process a value, crushing arrays where appropriate.
    /// Mirrors Python `_process_value` (line 2307-2398).
    ///
    /// Returns `(processed_value, info_string)`. CCR markers are
    /// stubbed (Python's tuple has a third element for them — Rust's
    /// version omits since we never produce markers in this stage).
    pub fn process_value(
        &self,
        value: &Value,
        depth: usize,
        query_context: &str,
        bias: f64,
    ) -> (Value, String) {
        if depth >= Self::MAX_PROCESS_DEPTH {
            return (value.clone(), String::new());
        }

        let mut info_parts: Vec<String> = Vec::new();

        match value {
            Value::Array(arr) => {
                let n = arr.len();
                if n >= self.config.min_items_to_analyze {
                    let arr_type = classify_array(arr);
                    match arr_type {
                        ArrayType::DictArray => {
                            let result = self.crush_array(arr, query_context, bias);
                            // Lossless path won → substitute the array
                            // with the compacted string in place. This
                            // makes the lossless win visible to the
                            // public `crush()` API: the output JSON
                            // has a string where the array used to be.
                            // The wrapping JSON structure is preserved.
                            if let Some(rendered) = result.compacted {
                                info_parts.push(format!(
                                    "{}({}->len={})",
                                    result.strategy_info,
                                    n,
                                    rendered.len()
                                ));
                                return (Value::String(rendered), info_parts.join(","));
                            }
                            info_parts.push(format!(
                                "{}({}->{})",
                                result.strategy_info,
                                n,
                                result.items.len()
                            ));
                            // Lossy path with rows dropped → append a
                            // CCR-Dropped sentinel object as the last
                            // element of the kept-items array. This is
                            // the **only** place the LLM sees the
                            // `<<ccr:HASH ...>>` pointer in the prompt.
                            // Without this, the store has the data but
                            // no model can ever ask for it.
                            //
                            // Sentinel shape: `{"_ccr_dropped":
                            // "<<ccr:HASH N_rows_offloaded>>"}` —
                            // preserves "array-of-objects" shape so
                            // downstream consumers iterating with
                            // `x.get(...)` keep working; the well-known
                            // `_ccr_dropped` key signals metadata
                            // unambiguously.
                            let mut items = result.items;
                            if !result.dropped_summary.is_empty() {
                                // The `_ccr_rows` marker names the per-blob
                                // row index so retrieval is proportional
                                // (resolve index, fetch only needed rows);
                                // `_ccr_dropped` keeps the byte-stable
                                // whole-blob pointer.
                                let sentinel = ccr_sentinel_map(
                                    &result.dropped_summary,
                                    result.row_index_marker.as_deref(),
                                );
                                items.push(Value::Object(sentinel));
                            }
                            return (Value::Array(items), info_parts.join(","));
                        }
                        ArrayType::StringArray => {
                            let strs: Vec<&str> = arr.iter().filter_map(|v| v.as_str()).collect();
                            let (crushed, strategy) = crush_string_array(&strs, &self.config, bias);
                            info_parts.push(format!("{}({}->{})", strategy, n, crushed.len()));
                            let mut crushed_values: Vec<Value> =
                                crushed.into_iter().map(Value::String).collect();
                            // 1A (non-dict path): persist the full original
                            // + append a CCR-Dropped sentinel whenever rows
                            // were dropped, so every distinct string is
                            // recoverable via `ccr_get(hash)` — never
                            // silently lost. Store write is unconditional
                            // (inside `persist_dropped`); the sentinel TEXT
                            // is gated by `enable_ccr_marker`.
                            let dropped = n.saturating_sub(crushed_values.len());
                            if let Some(sentinel) = self.ccr_dropped_sentinel(arr, dropped) {
                                crushed_values.push(sentinel);
                            }
                            return (Value::Array(crushed_values), info_parts.join(","));
                        }
                        ArrayType::NumberArray => {
                            let (crushed, strategy) = crush_number_array(arr, &self.config, bias);
                            info_parts.push(format!("{}({}->{})", strategy, n, crushed.len()));
                            let mut crushed = crushed;
                            // 1A (non-dict path): same guarantee as the
                            // string branch — persist + sentinel on drop.
                            let dropped = n.saturating_sub(crushed.len());
                            if let Some(sentinel) = self.ccr_dropped_sentinel(arr, dropped) {
                                crushed.push(sentinel);
                            }
                            return (Value::Array(crushed), info_parts.join(","));
                        }
                        ArrayType::MixedArray => {
                            let (crushed, strategy) =
                                self.crush_mixed_array(arr, query_context, bias);
                            info_parts.push(format!("{}({}->{})", strategy, n, crushed.len()));
                            let mut crushed = crushed;
                            // 1A (non-dict path): the mixed crusher drops
                            // str/number subgroup items (and its own
                            // dropped_summary was previously discarded).
                            // Persist the full original + sentinel here so
                            // every dropped item across all subgroups is
                            // recoverable.
                            let dropped = n.saturating_sub(crushed.len());
                            if let Some(sentinel) = self.ccr_dropped_sentinel(arr, dropped) {
                                crushed.push(sentinel);
                            }
                            return (Value::Array(crushed), info_parts.join(","));
                        }
                        // NestedArray, BoolArray, Empty → fall through
                        // to recursive descent.
                        _ => {}
                    }
                }

                // Below threshold or not crushable → recurse into items.
                let mut processed: Vec<Value> = Vec::with_capacity(n);
                for item in arr {
                    let (p_item, p_info) = self.process_value(item, depth + 1, query_context, bias);
                    processed.push(p_item);
                    if !p_info.is_empty() {
                        info_parts.push(p_info);
                    }
                }
                (Value::Array(processed), info_parts.join(","))
            }
            Value::Object(map) => {
                // First pass: recurse into values to compress nested arrays.
                let mut processed = serde_json::Map::new();
                for (k, v) in map {
                    let (p_val, p_info) = self.process_value(v, depth + 1, query_context, bias);
                    processed.insert(k.clone(), p_val);
                    if !p_info.is_empty() {
                        info_parts.push(p_info);
                    }
                }

                // Second pass: if the object itself has many keys,
                // compress at the key level.
                if processed.len() >= self.config.min_items_to_analyze {
                    let (crushed_dict, strategy) = crush_object(&processed, &self.config, bias);
                    if strategy != "object:passthrough" {
                        info_parts.push(strategy);
                        return (Value::Object(crushed_dict), info_parts.join(","));
                    }
                }

                (Value::Object(processed), info_parts.join(","))
            }
            // Strings: walker-equivalent handling. Delegates to
            // `process_string` which parses stringified-JSON containers
            // (recursing through `process_value`) and CCR-substitutes
            // opaque blobs (with store-write so retrieval works).
            Value::String(s) => self.process_string(s, depth, query_context, bias),
            // Other scalars — passthrough.
            _ => (value.clone(), String::new()),
        }
    }

    /// Walker-equivalent string handling. Mirrors `walker::walk_string`
    /// in `compaction/walker.rs` but lives on `SmartCrusher` so the
    /// public `crush()` path picks it up.
    ///
    /// Two cases:
    /// 1. **Stringified-JSON.** Strings that parse to a JSON object or
    ///    array → recurse via `process_value`, then re-emit the result
    ///    as a compact JSON string. The wrapping string is preserved
    ///    (so the parent JSON shape stays a string-typed field), but
    ///    its contents are processed end-to-end.
    /// 2. **Opaque blobs.** Strings classified as
    ///    [`CellClass::Opaque`] (long base64 / HTML / long-text) →
    ///    substitute with a `<<ccr:HASH,KIND,SIZE>>` marker. Same
    ///    format as `compaction::walker::format_ccr_marker` so
    ///    downstream consumers can pattern-match markers regardless
    ///    of which path emitted them.
    fn process_string(
        &self,
        s: &str,
        depth: usize,
        query_context: &str,
        bias: f64,
    ) -> (Value, String) {
        // 1. Stringified-JSON: parse, recurse, re-render.
        if let Some(parsed) = try_parse_json_container(s) {
            let (processed, sub_info) = self.process_value(&parsed, depth + 1, query_context, bias);
            // If recursion produced something different, re-emit.
            // Special case: if the recursion returned a `Value::String`
            // (lossless compaction substituted the array with a
            // rendered CSV+schema string), use that string directly.
            // Re-encoding it as JSON would produce a quoted string
            // literal — double-encoded — which is not what callers
            // expect in the wrapping field.
            if processed != parsed {
                let rendered = match &processed {
                    Value::String(rendered_str) => rendered_str.clone(),
                    _ => serde_json::to_string(&processed).unwrap_or_else(|_| s.to_string()),
                };
                let info = if sub_info.is_empty() {
                    "string_json".to_string()
                } else {
                    format!("string_json[{sub_info}]")
                };
                return (Value::String(rendered), info);
            }
        }

        // 2. Opaque blob: substitute with CCR marker AND stash the
        // original in the store so retrieval works. Hash + format
        // identical to walker.rs via the shared helper — zero drift.
        let cfg = ClassifyConfig::default();
        if let CellClass::Opaque(kind) = classify_cell(&Value::String(s.to_string()), &cfg) {
            let marker = emit_opaque_ccr_marker(s, &kind, self.ccr_store.as_ref());
            let kind_label = opaque_kind_label(&kind);
            return (Value::String(marker), format!("string_ccr:{kind_label}"));
        }

        // 3. Plain string — passthrough.
        (Value::String(s.to_string()), String::new())
    }

    /// Compress an array of dict items.
    ///
    /// Direct port of `_crush_array` (Python line 2400-2687) with the
    /// optional subsystems (TOIN / CCR / feedback / telemetry) wired
    /// in their disabled-by-default behavior. See module-level docs
    /// for the rationale.
    ///
    /// # Pipeline
    ///
    /// 1. Compute `item_strings` once (used as input to adaptive
    ///    sizing and downstream relevance scoring).
    /// 2. `compute_optimal_k` → `adaptive_k`.
    /// 3. If `n <= adaptive_k`, return passthrough.
    /// 4. `analyzer.analyze_array(items)` → `analysis`.
    /// 5. If `analysis.recommended_strategy == Skip`, return passthrough
    ///    with a `skip:<reason>` strategy string.
    /// 6. `planner.create_plan(analysis, items, query_context, ...)`.
    /// 7. `execute_plan(plan, items)` → result.
    /// 8. Strategy info = `analysis.recommended_strategy.as_str()`.
    pub fn crush_array(&self, items: &[Value], query_context: &str, bias: f64) -> CrushArrayResult {
        let item_strings: Vec<String> = items
            .iter()
            .map(|i| serde_json::to_string(i).unwrap_or_default())
            .collect();
        let item_str_refs: Vec<&str> = item_strings.iter().map(|s| s.as_str()).collect();

        let max_k = if self.config.max_items_after_crush > 0 {
            Some(self.config.max_items_after_crush)
        } else {
            None
        };
        let adaptive_k = compute_optimal_k(&item_str_refs, bias, 3, max_k);

        // Tier-1 boundary: array already small enough — nothing to
        // drop. Still worth a LOSSLESS look: a cleanly-tabular small
        // array (df/ps-style tool output) shrinks 30%+ with zero loss,
        // and small arrays are the COMMON case for tool output. Three
        // gates protect the passthrough default beyond the big-array
        // ratio check:
        // - no `OpaqueRef` substitution anywhere — on a small array
        //   every value must stay verbatim in the visible output
        //   (substituting a file's content with a CCR pointer would
        //   hide exactly what the model was asked to read);
        // - absolute saving ≥ `SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES` —
        //   the schema line must pay for itself; toy arrays stay
        //   passthrough;
        // - the same `lossless_min_savings_ratio` gate as the
        //   big-array attempt.
        if items.len() <= adaptive_k {
            if items.len() >= 2 {
                if let Some(stage) = &self.compaction {
                    let (c, rendered) = stage.run(items);
                    if c.was_compacted() && !c.contains_opaque_ref() {
                        let input_bytes = estimate_array_bytes(&item_strings);
                        let saved = input_bytes.saturating_sub(rendered.len());
                        let savings_ratio = if input_bytes > 0 {
                            saved as f64 / input_bytes as f64
                        } else {
                            0.0
                        };
                        if saved >= SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES
                            && savings_ratio >= self.config.lossless_min_savings_ratio
                        {
                            let kind = compaction_kind_str(&c);
                            return CrushArrayResult {
                                items: items.to_vec(), // nothing dropped
                                strategy_info: format!("lossless:{kind}"),
                                ccr_hash: None,
                                dropped_summary: String::new(),
                                compacted: Some(rendered),
                                compaction_kind: Some(kind),
                                row_index_marker: None,
                            };
                        }
                    }
                }
            }
            return CrushArrayResult {
                items: items.to_vec(),
                strategy_info: "none:adaptive_at_limit".to_string(),
                ccr_hash: None,
                dropped_summary: String::new(),
                compacted: None,
                compaction_kind: None,
                row_index_marker: None,
            };
        }

        // ── Lossless candidate ──
        //
        // Run the compaction stage ONCE if present. The lossless render keeps
        // every row (nothing dropped); it is a valid candidate only when
        // it actually compacted and clears the byte-savings gate — below
        // that gate the rendering is not worth shipping over either the
        // raw array or the lossy view, so it is not a real alternative.
        //
        // The single run also supplies `lossless_uses_opaque` (computed from
        // the same `Compaction` value below) so we do NOT call stage.run a
        // second time — that was the redundant hot-path double-compaction
        // eliminated by U8.
        let (lossless_candidate, lossless_uses_opaque) =
            if let Some(stage) = self.compaction.as_ref() {
                let (c, rendered) = stage.run(items);
                // Read `contains_opaque_ref` before `c` is potentially moved.
                let uses_opaque = c.contains_opaque_ref();
                let candidate = if c.was_compacted() {
                    let input_bytes = estimate_array_bytes(&item_strings);
                    let savings_ratio = if input_bytes > 0 {
                        1.0 - (rendered.len() as f64 / input_bytes as f64)
                    } else {
                        0.0
                    };
                    if savings_ratio >= self.config.lossless_min_savings_ratio {
                        let kind = compaction_kind_str(&c);
                        Some(CrushArrayResult {
                            items: items.to_vec(), // nothing dropped
                            strategy_info: format!("lossless:{kind}"),
                            ccr_hash: None,
                            dropped_summary: String::new(),
                            compacted: Some(rendered),
                            compaction_kind: Some(kind),
                            row_index_marker: None,
                        })
                    } else {
                        None
                    }
                } else {
                    None
                };
                (candidate, uses_opaque)
            } else {
                (None, false)
            };

        // ── Lossy-recoverable candidate ──
        //
        // Compress inline + cache the full original via CCR. The runtime
        // caller stashes the full input keyed by `ccr_hash` so a retrieval
        // tool can serve dropped rows back to the LLM on demand. **No data
        // is lost** — "lossy" means "compressed view inline; full payload
        // retrievable via CCR cache." When the array is not safe to crush
        // (the analyzer's `Skip` gate) there is NO lossy alternative — the
        // outcome carries the `skip:<reason>` passthrough so the routing
        // layer can ship it when there's also no lossless render.
        // Does the lossless compaction render rely on opaque-blob
        // substitution (heavy base64/hex fields replaced by an
        // `<<ccr:HASH,KIND,SIZE>>` pointer while EVERY row stays visible)?
        // That path is the dedicated, better-suited treatment for blob-
        // bearing rows: it preserves each row's light fields (id/name/…)
        // inline instead of dropping whole rows. When it applies, the
        // entropy-floor override must stand down so it does not hijack
        // blob data into a row-drop render — both are recoverable, but the
        // opaque-substitution view keeps strictly more visible per row.
        //
        // `lossless_uses_opaque` is derived from the SAME Compaction value
        // produced by the single stage.run call above (U8 dedup).

        let lossy = self.crush_array_lossy(
            items,
            query_context,
            &item_strings,
            adaptive_k,
            !lossless_uses_opaque,
        );

        // ── Route between the recoverable renders ──
        //
        // When BOTH a lossless render and a lossy DROP render exist they
        // are each 100% recoverable: lossless shows every row; lossy
        // surfaces a `<<ccr:HASH>>` pointer to the CCR-stored originals.
        // So the choice is a pure size decision with no information loss.
        // Under `MinTokens` (the default) ship the fewer-TOKEN render
        // (bytes mislead — hex vs base64); ties prefer lossless (more rows
        // visible). Under `LosslessFirst` keep the legacy behavior:
        // lossless wins whenever it cleared its gate.
        match (lossless_candidate, lossy) {
            (Some(lossless), LossyOutcome::Crushed(lossy)) => match self.config.routing_policy {
                RoutingPolicy::LosslessFirst => lossless,
                RoutingPolicy::MinTokens => {
                    let lossless_tokens = self.render_token_count(&lossless);
                    let lossy_tokens = self.render_token_count(&lossy);
                    // Lossy wins only when STRICTLY fewer tokens; ties (and
                    // lossless-fewer) → lossless: more rows visible at no
                    // extra token cost.
                    if lossy_tokens < lossless_tokens {
                        lossy
                    } else {
                        lossless
                    }
                }
            },
            // Lossless render valid but the array isn't droppable (Skip):
            // ship lossless — it shows every row losslessly. (A non-
            // droppable array should never drop, and lossless never drops.)
            (Some(lossless), LossyOutcome::Skip(_)) => lossless,
            // Only the lossy DROP render is valid → ship it (unchanged).
            (None, LossyOutcome::Crushed(lossy)) => lossy,
            // No lossless render and the array isn't droppable → the
            // `skip:<reason>` passthrough (preserves pre-routing behavior).
            (None, LossyOutcome::Skip(passthrough)) => passthrough,
        }
    }

    /// Build the lossy-recoverable render of `items` (row-drop + CCR
    /// sentinel). Returns [`LossyOutcome::Skip`] (carrying the
    /// `skip:<reason>` passthrough) when the array is not safe to crush
    /// (the analyzer's `Skip` gate) — there is no DROP render in that
    /// case. Otherwise returns [`LossyOutcome::Crushed`] with the
    /// row-dropped render. The store write + recovery pointer are emitted
    /// exactly as before via [`SmartCrusher::persist_dropped`]: a chosen
    /// lossy render is ALWAYS recoverable.
    ///
    /// Factored out of `crush_array` so the routing layer can size this
    /// candidate against the lossless one before deciding which to ship.
    /// Behavior is byte-identical to the pre-routing lossy path — only
    /// the place it is *called from* changed.
    fn crush_array_lossy(
        &self,
        items: &[Value],
        query_context: &str,
        item_strings: &[String],
        adaptive_k: usize,
        // When false, the entropy-floor crushability override stands down
        // (a better-suited lossless render — e.g. opaque-blob substitution
        // — exists for this array, so we must not hijack it into a drop).
        allow_skip_override: bool,
    ) -> LossyOutcome {
        // CCR-BACKED AGGRESSIVE BUDGET: when a CCR store is configured,
        // every dropped row is guaranteed recoverable (unconditional
        // persist + surfaced `<<ccr:HASH>>` pointer — the invariant the
        // adversarial loop locked). Under that guarantee the visible
        // sample only has to carry the *signal* — errors, outliers,
        // anomalies, query-relevant rows (all pinned beyond budget by
        // `prioritize_indices`) — not a generic cross-section, so the
        // keep budget is halved. Without a store the drop would be
        // unrecoverable and the budget stays at the full `adaptive_k`
        // (legacy / parity mode).
        let effective_max_items = if self.ccr_store.is_some() {
            ccr_backed_keep_budget(adaptive_k)
        } else {
            adaptive_k
        };
        let mut analysis = self.analyzer.analyze_array(items);

        // ── CCR-backed crushability override (entropy floor) ──
        //
        // The analyzer's crushability gate refuses to crush near-unique,
        // high-uniqueness arrays UNLESS a "signal" (a numeric anomaly, a
        // change point, an error keyword) happens to be present — see
        // `analyze_crushability` cases 2 & 4 (`unique_entities_no_signal`,
        // `medium_uniqueness_no_signal`). That gate was written for the
        // PERMANENT-LOSS world: with no signal telling us which distinct
        // row matters, dropping any of them risked losing it forever, so
        // the safe choice was to keep them all visible.
        //
        // Under the CCR recovery invariant that premise is gone. When a
        // store is configured every dropped row is persisted + surfaced
        // via a `<<ccr:HASH>>` pointer, so a smaller visible sample loses
        // NO information — the rest is retrievable from the output alone.
        // "We don't know which row matters" therefore no longer argues for
        // keeping everything visible; it argues for a bounded recoverable
        // sample. Worse, the gate is NON-DETERMINISTIC on exactly this
        // data: whether a uniformly-random integer column produces a >2σ
        // anomaly is a per-seed coin-flip, so the SAME near-unique shape
        // flips between "skip → ship all rows" (~34% reduction) and
        // "crush → drop+recover" (~94%) across seeds. That is the
        // erratic 24-94% scatter the verifier measured.
        //
        // Fix: when a CCR store guarantees recovery, do NOT skip on a
        // no-signal reason — re-derive the real pattern strategy (the one
        // `select_strategy` would pick if `crushable` were true) and crush.
        // This is deterministic (independent of the noise signals) and
        // aggressive at the entropy floor. The result still flows through
        // `MinTokens` routing below, so the lossy render only SHIPS when it
        // is actually fewer tokens — the override just makes the
        // recoverable candidate EXIST every time, not at the mercy of a
        // random anomaly. STRUCTURAL skips (mixed types, too-few-items,
        // non-dict) are NOT overridden: those arrays are genuinely
        // un-sampleable, not merely signal-free.
        if analysis.recommended_strategy == CompressionStrategy::Skip
            && allow_skip_override
            && self.config.crush_unique_entities_when_recoverable
            && self.ccr_store.is_some()
            && skip_reason_is_no_signal(&analysis)
        {
            let strategy = self.analyzer.select_strategy(
                &analysis.field_stats,
                &analysis.detected_pattern,
                items.len(),
                None, // bypass the crushability veto: recovery is CCR-backed
            );
            if strategy != CompressionStrategy::Skip {
                analysis.recommended_strategy = strategy;
            }
        }

        // Crushability gate: not safe to crush → no DROP candidate. Carry
        // the `skip:<reason>` passthrough so the caller can ship it when
        // there's also no lossless render.
        if analysis.recommended_strategy == CompressionStrategy::Skip {
            let reason = match &analysis.crushability {
                Some(c) => format!("skip:{}", c.reason),
                None => String::new(),
            };
            return LossyOutcome::Skip(CrushArrayResult {
                items: items.to_vec(),
                strategy_info: reason,
                ccr_hash: None,
                dropped_summary: String::new(),
                compacted: None,
                compaction_kind: None,
                row_index_marker: None,
            });
        }

        let plan = self.planner().create_plan(
            &analysis,
            items,
            query_context,
            None, // preserve_fields (TOIN — stubbed)
            Some(effective_max_items),
            Some(item_strings),
        );
        let mut result = self.execute_plan(&plan, items);

        // Field-aware multiplicity (DESIGN.md Imp2). When rows that are
        // identical-except-identity collapse under the stable-projection
        // hash, the kept representative carries a `_dup_count` so the
        // model knows N rows existed. This fires ONLY when real
        // duplication is present (group size > 1); for all-distinct data
        // (e.g. unique-subject git logs, search results) every group is
        // size 1, no key is added, and the output bytes are unchanged.
        let exclude = compute_exclude_set(&analysis.field_stats, items);
        if !exclude.is_empty() {
            annotate_dup_counts(&mut result, items, &exclude);
        }

        // CCR persistence + marker emission. **The store write is the
        // cornerstone of CCR's no-data-loss guarantee:** whenever rows
        // are dropped we hash the full original and stash it in the
        // configured store so a dropped needle is *always* recoverable
        // — never silently lost.
        let dropped_count = items.len().saturating_sub(result.len());
        let (ccr_hash, dropped_summary, row_index_marker) =
            match self.persist_dropped(items, dropped_count) {
                Some(persisted) => (
                    Some(persisted.hash),
                    persisted.marker,
                    persisted.row_index_marker,
                ),
                None => (None, String::new(), None),
            };

        // ── Survivor compaction: lossless re-encoding of the kept rows ──
        //
        // The lossy selection above decides WHICH rows stay visible; this
        // step only decides how those rows are RENDERED. When the
        // compaction stage can render the survivors as a CSV-schema
        // table that is meaningfully smaller than the JSON array form,
        // ship that rendering with the `{"_ccr_dropped": ...}` sentinel
        // appended as a final line. Every kept value stays verbatim in
        // the output and the recovery pointer still names the full
        // original. Gated on:
        // - no `OpaqueRef` substitution (survivor values must stay
        //   verbatim — same rule as the small-array lossless zone);
        // - absolute saving ≥ `LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES`
        //   vs the exact bytes the JSON form would ship.
        if !dropped_summary.is_empty() {
            if let Some(stage) = &self.compaction {
                let (c, rendered) = stage.run(&result);
                if c.was_compacted() && !c.contains_opaque_ref() {
                    let sentinel = ccr_sentinel_map(&dropped_summary, row_index_marker.as_deref());
                    let sentinel_line =
                        crate::transforms::anchor_selector::python_safe_json_dumps(
                            &Value::Object(sentinel.clone()),
                        );
                    let mut json_form_items = result.clone();
                    json_form_items.push(Value::Object(sentinel));
                    let json_form = crate::transforms::anchor_selector::python_safe_json_dumps(
                        &Value::Array(json_form_items),
                    );
                    let compact_len = rendered.len() + 1 + sentinel_line.len();
                    if json_form.len().saturating_sub(compact_len)
                        >= LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES
                    {
                        let kind = compaction_kind_str(&c);
                        let rendered_with_sentinel =
                            format!("{}\n{sentinel_line}", rendered.trim_end_matches('\n'));
                        return LossyOutcome::Crushed(CrushArrayResult {
                            items: result,
                            strategy_info: format!(
                                "{}+compact:{kind}",
                                analysis.recommended_strategy.as_str()
                            ),
                            ccr_hash,
                            dropped_summary,
                            compacted: Some(rendered_with_sentinel),
                            compaction_kind: Some(kind),
                            row_index_marker,
                        });
                    }
                }
            }
        }

        LossyOutcome::Crushed(CrushArrayResult {
            items: result,
            strategy_info: analysis.recommended_strategy.as_str().to_string(),
            ccr_hash,
            dropped_summary,
            compacted: None,
            compaction_kind: None,
            row_index_marker,
        })
    }

    /// Count the tokens of the FINAL model-visible string a
    /// `CrushArrayResult` renders to — the exact text `process_value`
    /// substitutes for this array. Used by the `MinTokens` routing policy
    /// to size the lossless vs lossy-recoverable candidates against each
    /// other. The two renders are:
    ///
    /// - `compacted = Some(s)` → the string `s` (lossless table, or lossy
    ///   survivor-compacted table whose last line is the sentinel).
    /// - `compacted = None` → the JSON array `[..items, {"_ccr_dropped":
    ///   marker}]` exactly as `process_value` emits it (the sentinel is
    ///   only appended when something was dropped).
    ///
    /// This mirrors `process_value`'s `DictArray` substitution so the
    /// token count reflects what the model actually sees, not an
    /// approximation.
    fn render_token_count(&self, result: &CrushArrayResult) -> usize {
        let rendered = self.render_result_string(result);
        self.tokenizer.count_text(&rendered)
    }

    /// Render a `CrushArrayResult` to the string `process_value`
    /// substitutes for the array (see [`SmartCrusher::render_token_count`]).
    fn render_result_string(&self, result: &CrushArrayResult) -> String {
        if let Some(s) = &result.compacted {
            return s.clone();
        }
        let mut items = result.items.clone();
        if !result.dropped_summary.is_empty() {
            let sentinel = ccr_sentinel_map(&result.dropped_summary, result.row_index_marker.as_deref());
            items.push(Value::Object(sentinel));
        }
        crate::transforms::anchor_selector::python_safe_json_dumps(&Value::Array(items))
    }

    /// Shared CCR persist + sentinel logic — the single source of truth
    /// for sub-step 1A's "kill silent loss" guarantee across **every**
    /// lossy array path (dict / string / number / mixed).
    ///
    /// When `dropped_count > 0`, serialize the FULL `original_items`
    /// array exactly once into canonical JSON, hash those bytes, and
    /// **unconditionally** write `(hash → canonical)` to the configured
    /// CCR store (if any), then **unconditionally** build the
    /// `<<ccr:HASH N_rows_offloaded>>` recovery pointer. The store write
    /// and pointer together are the cornerstone of the no-data-loss
    /// guarantee: a dropped needle is always retrievable via
    /// `ccr_get(hash)` AND nameable from the output via the pointer —
    /// never silently lost. Neither is gated by `enable_ccr_marker`
    /// (Defect 1): you cannot drop a distinct item without surfacing a
    /// recovery pointer to it.
    ///
    /// Returns `None` when nothing was dropped (no hash, no marker, no
    /// store write). Centralizing this here keeps the canonicalization
    /// and hash scheme byte-identical across all callers — the dict path
    /// behavior is unchanged, and the non-dict paths now inherit the
    /// exact same contract.
    fn persist_dropped(
        &self,
        original_items: &[Value],
        dropped_count: usize,
    ) -> Option<DroppedPersist> {
        if dropped_count == 0 {
            return None;
        }

        // Serialize the original array exactly ONCE. The hash is taken
        // over those bytes, and (if a store is configured) the same
        // bytes get stored — no redundant clone or re-serialize.
        let canonical = canonical_array_json(original_items);
        let hash = hash_canonical(&canonical);

        // ── GRANULAR per-row persist (proportional retrieval) ──
        //
        // Held-out audit (verify/heldout/REPORT.md, leniency #2): the
        // single whole-blob offload makes the FIRST needed row cost the
        // WHOLE payload — a single `<<ccr:HASH>>` retrieve returns every
        // dropped row at once, so effective savings can go NEGATIVE the
        // moment the model retrieves anything (logs@90 high: +55.7% @25%
        // → −10.3% worst-case). Fix: ALSO stash each original row under
        // its own canonical 1-element hash so retrieving ONE row fetches
        // exactly one row, not the whole blob.
        //
        // Storing EVERY row (not only the dropped ones) is intentional:
        // `persist_dropped` is told only `dropped_count`, not which rows
        // survived, and a kept row hashed here is harmless (it's already
        // visible). It guarantees that ANY row the model later asks for
        // — dropped or not — resolves to just that single row. Each
        // per-row entry is keyed by `hash_canonical` over the 1-element
        // canonical array, so `ccr_get(row_hash)` returns exactly `[row]`.
        //
        // The per-row hashes are NOT inlined into the prompt (that would
        // add O(n) pointer strings, bloat the lossy render, and flip the
        // min-tokens routing toward lossless). Instead they go into a
        // store-side ROW INDEX under `{hash}#rows` — a JSON array of the
        // row hashes — and the prompt carries ONE compact marker naming
        // that index. Retrieval: resolve the index (cheap), then fetch
        // only the needed rows. Prompt cost ~flat; retrieval proportional.
        //
        // ── Write ORDER matters under a bounded LRU ──
        // The per-row chunks + index are written FIRST, the whole-blob
        // LAST. Per-row chunks are redundant with the whole-blob (both
        // recover the same rows), so when the store is capacity-bound the
        // FIFO eviction sheds the redundant chunks before the whole-blob
        // — keeping the byte-stable single-blob recovery fallback alive
        // even if proportional retrieval degrades. Recovery is therefore
        // never worse than the pre-change single-blob guarantee.
        let mut row_index_marker: Option<String> = None;
        if let Some(store) = &self.ccr_store {
            let mut row_hashes: Vec<String> = Vec::with_capacity(original_items.len());
            for item in original_items {
                let row_canonical = canonical_array_json(std::slice::from_ref(item));
                let row_hash = hash_canonical(&row_canonical);
                store.put(&row_hash, &row_canonical);
                row_hashes.push(row_hash);
            }
            // Row index: `{hash}#rows` → ["rowhash0", "rowhash1", ...].
            // Stored as a JSON array of strings so the retrieval layer can
            // parse it and address each row independently.
            let index_key = format!("{hash}#rows");
            let index_payload = serde_json::to_string(&row_hashes).unwrap_or_default();
            store.put(&index_key, &index_payload);
            row_index_marker = Some(format!("<<ccr:{index_key} {dropped_count}_chunks>>"));
        }

        // ── Unconditional whole-blob persist (1A) — written LAST ──
        // The byte-stable recovery key the invariant + parity depend on.
        if let Some(store) = &self.ccr_store {
            store.put(&hash, &canonical);
        }

        // ── Unconditional recovery pointer (Defect 1) ──
        //
        // The `<<ccr:HASH N_rows_offloaded>>` marker is the RECOVERY
        // KEY, not a UX nicety: it is the only way a consumer holding
        // just the output can name the hash and pull the dropped rows
        // back. It MUST be surfaced whenever data is dropped, regardless
        // of `enable_ccr_marker`. The recovery invariant ("a dropped
        // item is recoverable from the output alone") cannot hold if the
        // pointer is suppressed while the rows are still dropped.
        //
        // `enable_ccr_marker` historically gated this text; that
        // conflated the *data-loss recovery pointer* with the heavier
        // *retrieval-tool injection* (advertising `headroom_retrieve`
        // into the request), which is owned by the router layer
        // (`CCRConfig.inject_tool` / `inject_retrieval_marker`), NOT by
        // the crusher. The crusher's job is to never drop a distinct
        // item without leaving a pointer to it; that pointer is now
        // emitted unconditionally on every drop.
        let marker = format!("<<ccr:{hash} {dropped_count}_rows_offloaded>>");

        Some(DroppedPersist {
            hash,
            marker,
            row_index_marker,
        })
    }

    /// Build the `{"_ccr_dropped": "<<ccr:HASH N_rows_offloaded>>"}`
    /// sentinel object for a non-dict array drop, persisting the full
    /// original via [`SmartCrusher::persist_dropped`] first.
    ///
    /// Returns `None` ONLY when nothing was dropped. Whenever rows were
    /// dropped, the sentinel is always produced (Defect 1): the
    /// recovery pointer is non-optional, so the output always carries a
    /// `<<ccr:HASH>>` the consumer can resolve via `ccr_get(hash)` —
    /// never a silent drop. This mirrors the dict path, where
    /// `dropped_summary` is now likewise always non-empty on a drop.
    fn ccr_dropped_sentinel(
        &self,
        original_items: &[Value],
        dropped_count: usize,
    ) -> Option<Value> {
        let persisted = self.persist_dropped(original_items, dropped_count)?;
        Some(build_ccr_sentinel(&persisted))
    }

    /// Compress a mixed-type array by grouping items by type and
    /// compressing each group with the appropriate handler.
    ///
    /// Direct port of `_crush_mixed_array` (Python line 2914-3013).
    ///
    /// Strategy:
    /// 1. Group by type (dict / str / number / list / null / bool / other).
    /// 2. For groups with >= `min_items_to_analyze` items: apply the
    ///    type-specific compressor.
    /// 3. For small groups: keep all items.
    /// 4. Reassemble in original order.
    ///
    /// Returns `(crushed_items, strategy_string)`.
    pub fn crush_mixed_array(
        &self,
        items: &[Value],
        query_context: &str,
        bias: f64,
    ) -> (Vec<Value>, String) {
        let n = items.len();
        if n <= 8 {
            return (items.to_vec(), "mixed:passthrough".to_string());
        }

        // Group by type, tracking original indices.
        let mut groups: GroupBuckets = GroupBuckets::default();
        for (i, item) in items.iter().enumerate() {
            groups.push(group_key(item), i, item.clone());
        }

        let mut keep_indices: std::collections::BTreeSet<usize> = std::collections::BTreeSet::new();
        let mut strategy_parts: Vec<String> = Vec::new();

        for (type_key, indices, values) in groups.into_iter() {
            // Small groups: keep all items.
            if values.len() < self.config.min_items_to_analyze {
                keep_indices.extend(&indices);
                continue;
            }

            match type_key {
                "dict" => {
                    let CrushArrayResult { items: crushed, .. } =
                        self.crush_array(&values, query_context, bias);
                    // Find which original indices survived by matching
                    // canonical-JSON serialization. Mirrors Python's
                    // `json.dumps(c, sort_keys=True, default=str)`-keyed
                    // set match.
                    let crushed_keys: std::collections::HashSet<String> =
                        crushed.iter().map(canonical_json_for_match).collect();
                    for (i, idx) in indices.iter().enumerate() {
                        if crushed_keys.contains(&canonical_json_for_match(&values[i])) {
                            keep_indices.insert(*idx);
                        }
                    }
                    strategy_parts.push(format!("dict:{}->{}", values.len(), crushed.len()));
                }
                "str" => {
                    let strs: Vec<&str> = values.iter().filter_map(|v| v.as_str()).collect();
                    let (crushed, _) = crush_string_array(&strs, &self.config, bias);
                    let crushed_set: std::collections::HashSet<&str> =
                        crushed.iter().map(|s| s.as_str()).collect();
                    for (i, idx) in indices.iter().enumerate() {
                        if let Some(s) = values[i].as_str() {
                            if crushed_set.contains(s) {
                                keep_indices.insert(*idx);
                            }
                        }
                    }
                    strategy_parts.push(format!("str:{}->{}", values.len(), crushed.len()));
                }
                "number" => {
                    // Python: just adaptive sampling + outlier detection
                    // (no summary prefix). Keeps first/last by index
                    // and items >variance_threshold σ from mean.
                    let item_strings: Vec<String> = values.iter().map(|v| v.to_string()).collect();
                    let item_refs: Vec<&str> = item_strings.iter().map(|s| s.as_str()).collect();
                    let (_kt, kf, kl, _) = compute_k_split(&item_refs, &self.config, bias);

                    let kf = kf.min(values.len());
                    let kl = kl.min(values.len().saturating_sub(kf));
                    let first_idx: Vec<usize> = indices.iter().take(kf).copied().collect();
                    let last_idx: Vec<usize> =
                        indices.iter().rev().take(kl).copied().collect::<Vec<_>>();
                    keep_indices.extend(&first_idx);
                    keep_indices.extend(&last_idx);

                    // Outliers via finite-only stats.
                    let finite: Vec<f64> = values
                        .iter()
                        .filter_map(|v| v.as_f64().filter(|f| f.is_finite()))
                        .collect();
                    if finite.len() > 1 {
                        if let Some(mean_v) = super::stats_math::mean(&finite) {
                            if let Some(std_v) = super::stats_math::sample_stdev(&finite) {
                                if std_v > 0.0 {
                                    let threshold = self.config.variance_threshold * std_v;
                                    for (i, val) in values.iter().enumerate() {
                                        if let Some(num) = val.as_f64().filter(|f| f.is_finite()) {
                                            if (num - mean_v).abs() > threshold {
                                                keep_indices.insert(indices[i]);
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    strategy_parts.push(format!("num:{}", values.len()));
                }
                _ => {
                    // list / bool / none / other → keep all items.
                    keep_indices.extend(&indices);
                }
            }
        }

        // Reassemble in original order.
        let result: Vec<Value> = keep_indices.iter().map(|&i| items[i].clone()).collect();
        let strategy = format!(
            "mixed:adaptive({}->{},{})",
            n,
            result.len(),
            strategy_parts.join(",")
        );
        (result, strategy)
    }
}

// ---------- helpers ----------

/// Group key that mirrors Python's `_crush_mixed_array` switch on
/// `isinstance`. Note the bool-before-number ordering: in Python, bool
/// is a subclass of int, but JSON treats them as distinct types, so we
/// don't have the Python ordering hazard.
fn group_key(item: &Value) -> &'static str {
    match item {
        Value::Object(_) => "dict",
        Value::String(_) => "str",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::Array(_) => "list",
        Value::Null => "none",
    }
}

/// Group buckets keyed by the type-string. Preserves first-occurrence
/// order across keys so dict/str/number/list/none/bool always come out
/// in the same order — matters because `keep_indices` is built
/// incrementally and Python iterates `groups.items()` (insertion order
/// in 3.7+).
#[derive(Default)]
struct GroupBuckets {
    entries: Vec<(&'static str, Vec<usize>, Vec<Value>)>,
    index_of: std::collections::HashMap<&'static str, usize>,
}

impl GroupBuckets {
    fn push(&mut self, key: &'static str, idx: usize, value: Value) {
        match self.index_of.get(key).copied() {
            Some(i) => {
                self.entries[i].1.push(idx);
                self.entries[i].2.push(value);
            }
            None => {
                self.index_of.insert(key, self.entries.len());
                self.entries.push((key, vec![idx], vec![value]));
            }
        }
    }
}

impl IntoIterator for GroupBuckets {
    type Item = (&'static str, Vec<usize>, Vec<Value>);
    type IntoIter = std::vec::IntoIter<Self::Item>;
    fn into_iter(self) -> Self::IntoIter {
        self.entries.into_iter()
    }
}

/// Serialize a `Value` for membership comparison. Mirrors Python's
/// `json.dumps(c, sort_keys=True, default=str)` used by
/// `_crush_mixed_array` to match crushed dict items back to their
/// original indices. The `default=str` fallback only matters for
/// non-JSON-serializable Python values; in serde_json land everything
/// is already JSON-native, so plain canonical JSON suffices.
fn canonical_json_for_match(value: &Value) -> String {
    crate::transforms::anchor_selector::python_json_dumps_sort_keys(value)
}

/// Stamp `_dup_count` on kept rows whose stable-projection-hash family
/// (over ALL original `items`, with `exclude` identity columns filtered)
/// has more than one member (DESIGN.md Imp2).
///
/// `_dup_count = N` records that N original rows shared this row's
/// value-bearing content (differing only in excluded identity columns).
/// Rows in a singleton family are left untouched, so all-distinct input
/// is byte-for-byte unchanged. The representative keeps its own real
/// varying values; the dropped duplicates remain CCR-recoverable from
/// the full-original store entry.
fn annotate_dup_counts(
    kept: &mut [Value],
    all_items: &[Value],
    exclude: &std::collections::BTreeSet<String>,
) {
    use crate::transforms::anchor_selector::stable_item_hash;

    // Family sizes over the WHOLE original array.
    let mut family_size: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    for item in all_items {
        if item.is_object() || item.is_array() {
            *family_size
                .entry(stable_item_hash(item, exclude))
                .or_insert(0) += 1;
        }
    }

    for row in kept.iter_mut() {
        if !row.is_object() {
            continue;
        }
        let h = stable_item_hash(row, exclude);
        let count = family_size.get(&h).copied().unwrap_or(1);
        if count > 1 {
            if let Some(obj) = row.as_object_mut() {
                // Don't clobber a real `_dup_count` field the caller
                // already had (extremely unlikely; defensive).
                obj.entry("_dup_count")
                    .or_insert_with(|| Value::from(count));
            }
        }
    }
}

/// Minimum ABSOLUTE byte saving required before a small array
/// (`len <= adaptive_k`, the tier-1 passthrough zone) ships the
/// lossless compacted rendering instead of passing through. The
/// big-array path uses the ratio gate alone; small arrays additionally
/// need the `[N]{cols}` schema line to pay for itself — re-encoding a
/// 3-row toy array to save a dozen bytes is churn, not compression.
const SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES: usize = 256;

/// Divisor applied to `adaptive_k` for the lossy keep budget when a CCR
/// store guarantees recovery of every dropped row. 2 (halving) keeps a
/// meaningful visible sample while the critical signals (errors /
/// outliers / anomalies / query pins) remain exempt from the budget.
const CCR_BACKED_KEEP_DIVISOR: usize = 2;

/// Floor for the CCR-backed keep budget. `min_items_to_analyze` (5) is
/// the engine's own notion of "too small to even analyze" — the visible
/// sample never shrinks below it.
const CCR_BACKED_KEEP_FLOOR: usize = 5;

/// Minimum ABSOLUTE byte saving required before the lossy path ships
/// its survivors as a CSV-schema rendering instead of a JSON array.
/// Same churn-protection rationale as the small-array gate: re-encoding
/// a handful of rows to save a few bytes is noise, not compression.
const LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES: usize = 64;

/// Is the analyzer's `Skip` a "no SIGNAL on distinct data" skip (as
/// opposed to a STRUCTURAL skip)?
///
/// The crushability gate produces two flavors of `Skip`:
/// - **no-signal skips** — the data IS sampleable (rows are well-formed
///   dicts with comparable fields) but the analyzer found no anomaly /
///   change-point / error-keyword to anchor the sample on, so under the
///   permanent-loss assumption it refused to drop. Reasons:
///   `unique_entities_no_signal`, `medium_uniqueness_no_signal`.
/// - **structural skips** — the array is genuinely un-sampleable
///   (too few items, non-dict items, mixed value types). Those carry
///   different reasons and MUST keep skipping even with CCR backing.
///
/// Only the no-signal flavor is eligible for the CCR-backed override:
/// recovery removes the loss risk that justified the veto, and the data
/// is structurally fine to sample. Matching on the reason string keeps
/// this decision in lockstep with `analyze_crushability`'s own labels —
/// a new structural reason is excluded by default (fail-closed).
fn skip_reason_is_no_signal(analysis: &ArrayAnalysis) -> bool {
    matches!(
        analysis.crushability.as_ref().map(|c| c.reason.as_str()),
        Some("unique_entities_no_signal") | Some("medium_uniqueness_no_signal")
    )
}

/// Lossy keep budget when every dropped row is CCR-recoverable.
/// `adaptive_k / 2`, floored at [`CCR_BACKED_KEEP_FLOOR`], never above
/// `adaptive_k` itself.
fn ccr_backed_keep_budget(adaptive_k: usize) -> usize {
    (adaptive_k / CCR_BACKED_KEEP_DIVISOR)
        .max(CCR_BACKED_KEEP_FLOOR)
        .min(adaptive_k)
}

#[cfg(test)]
mod ccr_budget_tests {
    use super::*;

    #[test]
    fn budget_halves_with_floor_and_cap() {
        assert_eq!(ccr_backed_keep_budget(15), 7); // default max_items_after_crush
        assert_eq!(ccr_backed_keep_budget(20), 10);
        assert_eq!(ccr_backed_keep_budget(10), 5); // floor met exactly
        assert_eq!(ccr_backed_keep_budget(8), 5); // floored at 5
        assert_eq!(ccr_backed_keep_budget(4), 4); // never above adaptive_k
        assert_eq!(ccr_backed_keep_budget(3), 3);
    }
}

/// Maps a `Compaction` to a stable kind tag exposed via `CrushArrayResult`.
fn compaction_kind_str(c: &Compaction) -> &'static str {
    match c {
        Compaction::Table { .. } => "table",
        Compaction::Buckets { .. } => "buckets",
        Compaction::OpaqueRef { .. } => "ccr",
        Compaction::Untouched(_) => "untouched",
    }
}

/// Approximate byte size of `[v0, v1, ...]` JSON serialization, given
/// each item's already-serialized form. Adds 2 for outer brackets and
/// 1 per inter-item comma. Used by the lossless savings-ratio check.
fn estimate_array_bytes(item_strings: &[String]) -> usize {
    let payload: usize = item_strings.iter().map(|s| s.len()).sum();
    let separators = item_strings.len().saturating_sub(1);
    payload + separators + 2
}

/// Serialize `[v0, v1, ...]` once into the canonical JSON form used by
/// the CCR retrieval contract. `serde_json` writes a slice of `Value` as
/// the same bytes it would write for `Value::Array(items.to_vec())`, so
/// we skip the array-wrapper allocation and the deep tree clone it
/// requires. Used by both the hash (input) and the store payload (write).
fn canonical_array_json(items: &[Value]) -> String {
    serde_json::to_string(items).unwrap_or_default()
}

/// 12-char SHA-256 hex prefix of an already-serialized canonical JSON
/// string. Caller is responsible for producing the canonical form via
/// [`canonical_array_json`] (or another byte-equal serializer) — the
/// hash is over the bytes, so a stable serializer is the contract.
fn hash_canonical(canonical: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(canonical.as_bytes());
    h.finalize()
        .iter()
        .take(6)
        .map(|b| format!("{b:02x}"))
        .collect()
}

// `hash_array_for_ccr` (a test-only `canonical_array_json + hash_canonical`
// convenience) lived here previously but had no callers — clippy flagged
// it as dead code. Reintroduce as a test fixture if a future test wants
// the one-liner; production callsites inline both steps so the canonical
// bytes can be reused for the store payload.

// ─── Walker-integration helpers (string handling) ──────────────────────
//
// Parse-as-JSON-container, marker formatting, and humanize-bytes used to
// live here as locals. They now live in `compaction::walker` so
// `walker.rs` and `process_value` share one canonical implementation —
// killing the drift risk where the two paths could format markers
// differently. `process_string` now calls `try_parse_json_container` and
// `emit_opaque_ccr_marker` directly. Only `opaque_kind_label` survives
// here because `process_string`'s `string_ccr:<kind>` strategy-info
// label is local to this module's debug-string convention.

fn opaque_kind_label(kind: &super::compaction::OpaqueKind) -> &str {
    use super::compaction::OpaqueKind;
    match kind {
        OpaqueKind::Base64Blob => "base64",
        OpaqueKind::LongString => "string",
        OpaqueKind::HtmlChunk => "html",
        OpaqueKind::Other(s) => s.as_str(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn crusher() -> SmartCrusher {
        SmartCrusher::new(SmartCrusherConfig::default())
    }

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

    // ---------- crush_array ----------

    #[test]
    fn crush_array_passthrough_when_below_adaptive_k() {
        let c = crusher();
        let items: Vec<Value> = (0..3).map(|i| json!({"id": i})).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(result.items.len(), 3);
        assert_eq!(result.strategy_info, "none:adaptive_at_limit");
        assert!(result.ccr_hash.is_none());
    }

    #[test]
    fn small_array_ships_lossless_when_savings_substantial() {
        // 8 rows — `compute_optimal_k` returns n for n <= 8, so this is
        // guaranteed inside the tier-1 passthrough zone (the SMALL
        // path, not the big-array lossless attempt). Enough repeated-
        // key overhead that the CSV rendering saves ≥ 256 bytes AND
        // ≥ the ratio gate → lossless ships, nothing dropped.
        let c = crusher();
        let items: Vec<Value> = (0..8)
            .map(|i| {
                json!({
                    "filesystem": format!("/dev/disk1s{i}"),
                    "kilobytes_total": 971350180,
                    "kilobytes_used": 543210 + i,
                    "capacity_percent": "85%",
                    "mounted_on": format!("/Volumes/vol_{i}"),
                })
            })
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(result.items.len(), 8, "nothing may be dropped");
        assert!(
            result.strategy_info.starts_with("lossless:table"),
            "got: {}",
            result.strategy_info
        );
        let compacted = result.compacted.expect("compacted must be set");
        assert!(compacted.starts_with("[8]{"), "got: {compacted}");
        assert!(result.ccr_hash.is_none());
        assert!(result.dropped_summary.is_empty());
    }

    #[test]
    fn small_toy_array_stays_passthrough_below_absolute_floor() {
        // 3 tiny rows save well above the RATIO gate but only ~a dozen
        // absolute bytes — the schema line doesn't pay for itself.
        let c = crusher();
        let items: Vec<Value> = (0..3).map(|i| json!({"id": i})).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(result.strategy_info, "none:adaptive_at_limit");
        assert!(result.compacted.is_none());
    }

    #[test]
    fn small_array_with_opaque_cells_stays_passthrough() {
        // A small array whose cells would be CCR-substituted (file
        // contents!) must NOT take the small-array lossless path — the
        // model needs those values visible verbatim.
        let c = crusher();
        let blob = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/".repeat(64);
        let items: Vec<Value> = (0..4)
            .map(|i| json!({"path": format!("src/f{i}.py"), "content": blob.clone()}))
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(result.strategy_info, "none:adaptive_at_limit");
        assert!(result.compacted.is_none());
        assert_eq!(result.items.len(), 4);
    }

    #[test]
    fn small_array_without_compaction_stage_stays_passthrough() {
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        let items: Vec<Value> = (0..8)
            .map(|i| {
                json!({
                    "filesystem": format!("/dev/disk1s{i}"),
                    "kilobytes_total": 971350180,
                    "kilobytes_used": 543210 + i,
                    "capacity_percent": "85%",
                    "mounted_on": format!("/Volumes/vol_{i}"),
                })
            })
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(result.strategy_info, "none:adaptive_at_limit");
        assert!(result.compacted.is_none());
    }

    #[test]
    fn crush_array_no_signal_with_ccr_store_crushes_recoverably() {
        // 30 unique dict items with ID-like fields → the analyzer's
        // crushability gate labels this `unique_entities_no_signal`.
        // Pre-fix that SKIPPED (returned all 30 rows). With a CCR store
        // configured (the `without_compaction` constructor installs the
        // default store) recovery is guaranteed, so the entropy-floor
        // override re-derives a real strategy and crushes — DETERMINISTIC
        // and aggressive, with every dropped row recoverable via the
        // surfaced `<<ccr:HASH>>` pointer. This is the routing fix that
        // collapses the 24-94% scatter on near-unique data.
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        let items: Vec<Value> = (0..30)
            .map(|i| json!({"id": i, "name": format!("user_{}", i)}))
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        // Aggressively crushed: far fewer survivors than the input.
        assert!(
            result.items.len() < items.len(),
            "expected a crush, got {} of {} rows",
            result.items.len(),
            items.len()
        );
        // Recovery invariant: a drop happened, so a CCR pointer is
        // surfaced and the store holds the full original (never silent).
        assert!(
            result.ccr_hash.is_some(),
            "dropped rows must carry a CCR recovery pointer"
        );
        assert!(
            !result.dropped_summary.is_empty(),
            "the `<<ccr:HASH>>` sentinel must be surfaced in the output"
        );
        assert!(
            !result.strategy_info.starts_with("skip:"),
            "no-signal + CCR store must crush, not skip; got {}",
            result.strategy_info
        );
    }

    #[test]
    fn crush_array_no_signal_without_ccr_store_still_skips() {
        // Same near-unique no-signal shape, but NO CCR store: a drop here
        // would be UNRECOVERABLE, so the override must NOT fire — the
        // analyzer's skip stands (legacy / parity mode, zero silent loss).
        let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
            .with_default_oss_setup()
            .build(); // no `.with_default_ccr_store()`
        assert!(c.ccr_store.is_none(), "this crusher must have no store");
        let items: Vec<Value> = (0..30)
            .map(|i| json!({"id": i, "name": format!("user_{}", i)}))
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        // Without recovery backing, the no-signal skip is preserved.
        assert_eq!(result.items.len(), 30);
        assert!(
            result.strategy_info.starts_with("skip:"),
            "expected skip:... without a store, got {}",
            result.strategy_info
        );
    }

    #[test]
    fn crush_array_low_uniqueness_compresses() {
        // 30 items with status=ok across all → low_uniqueness path
        // (crushable, smart_sample strategy).
        let c = crusher();
        let items: Vec<Value> = (0..30).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert!(result.items.len() <= 30, "should not exceed original count");
    }

    #[test]
    fn crush_array_keeps_error_items() {
        let c = crusher();
        let mut items: Vec<Value> = (0..30).map(|i| json!({"id": i, "status": "ok"})).collect();
        items.push(json!({"id": 30, "status": "error", "msg": "FATAL"}));
        let result = c.crush_array(&items, "", 1.0);
        // Whatever path is taken, the error item should survive.
        assert!(
            result
                .items
                .iter()
                .any(|item| { item.get("status").and_then(|v| v.as_str()) == Some("error") }),
            "error item must survive crush_array"
        );
    }

    // ---------- crush_mixed_array ----------

    #[test]
    fn crush_mixed_passthrough_at_threshold() {
        let c = crusher();
        let items: Vec<Value> = vec![
            json!(1),
            json!("two"),
            json!({"k": "v"}),
            json!([1, 2]),
            json!(null),
            json!(true),
            json!(3),
            json!("four"),
        ];
        let (result, strat) = c.crush_mixed_array(&items, "", 1.0);
        assert_eq!(result.len(), 8);
        assert_eq!(strat, "mixed:passthrough");
    }

    #[test]
    fn crush_mixed_groups_and_compresses_dicts() {
        let c = crusher();
        // 25 dicts (large group → gets crushed) + 5 strings (small group → all kept).
        let mut items: Vec<Value> = (0..25).map(|i| json!({"id": i, "status": "ok"})).collect();
        for i in 0..5 {
            items.push(json!(format!("string_{}", i)));
        }
        let (result, strat) = c.crush_mixed_array(&items, "", 1.0);
        assert!(strat.starts_with("mixed:adaptive("));
        // The 5 strings (small group) all survive.
        let str_count = result.iter().filter(|v| v.is_string()).count();
        assert_eq!(str_count, 5);
    }

    #[test]
    fn crush_mixed_keeps_lists_and_nulls_unchanged() {
        let c = crusher();
        let mut items: Vec<Value> = vec![json!([1, 2]); 6];
        items.extend(vec![json!(null); 6]);
        items.extend(vec![json!({"k": 1}); 10]);
        let (result, _strat) = c.crush_mixed_array(&items, "", 1.0);
        // Lists and nulls (not dict/str/number) → fall through to "keep all".
        let list_count = result.iter().filter(|v| v.is_array()).count();
        let null_count = result.iter().filter(|v| v.is_null()).count();
        assert_eq!(list_count, 6);
        assert_eq!(null_count, 6);
    }

    #[test]
    fn crusher_construction_default() {
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        assert_eq!(c.config.max_items_after_crush, 15);
    }

    // ---------- top-level crush ----------

    #[test]
    fn crush_non_json_passes_through_unchanged() {
        let c = crusher();
        let result = c.crush("not json at all", "", 1.0);
        assert!(!result.was_modified);
        assert_eq!(result.compressed, "not json at all");
        assert_eq!(result.strategy, "passthrough");
    }

    #[test]
    fn crush_scalar_json_passes_through() {
        let c = crusher();
        let result = c.crush("42", "", 1.0);
        // A scalar is not crushable; should round-trip unchanged.
        assert_eq!(result.compressed, "42");
        assert!(!result.was_modified);
    }

    #[test]
    fn crush_small_array_passes_through() {
        let c = crusher();
        // Compact-form input matches the compact serializer output, so
        // the array is not "modified" even though it round-trips
        // through parse → serialize. (The spaced form `[1, 2, 3]`
        // would mark `was_modified=true` because the compact
        // serializer rewrites it to `[1,2,3]`.)
        let result = c.crush(r#"[1,2,3]"#, "", 1.0);
        // Below min_items_to_analyze=5 → no crushing of the structure.
        assert!(!result.was_modified);
        assert_eq!(result.compressed, "[1,2,3]");
    }

    #[test]
    fn crush_dict_array_crushes_when_low_uniqueness() {
        // The public `crush()` API serializes back to JSON; the
        // lossless-path output (a compacted string) is exposed via
        // `crush_array().compacted` rather than being substituted into
        // the JSON re-serialization. So we exercise the lossy path
        // here via `without_compaction()` to validate the original
        // intent: low-uniqueness dicts compress via row-dropping.
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        let mut input = String::from("[");
        for i in 0..30 {
            if i > 0 {
                input.push(',');
            }
            input.push_str(r#"{"status":"ok"}"#);
        }
        input.push(']');
        let result = c.crush(&input, "", 1.0);
        assert!(
            result.was_modified,
            "30 identical dicts should compress (low_uniqueness_safe_to_sample)"
        );
        assert_ne!(result.strategy, "passthrough");
    }

    #[test]
    fn crush_serializes_with_python_safe_format() {
        let c = crusher();
        // SmartCrusher uses Python's `safe_json_dumps`: compact
        // separators `(",", ":")` + `ensure_ascii=False`, preserving
        // object-key insertion order. A spaced input round-trips to
        // the compact form.
        let input = r#"{"a": 1, "b": 2, "c": 3}"#;
        let result = c.crush(input, "", 1.0);
        assert_eq!(
            result.compressed, r#"{"a":1,"b":2,"c":3}"#,
            "safe_json_dumps emits compact `,` / `:` separators"
        );
    }

    #[test]
    fn crush_recurses_into_nested_arrays() {
        let c = crusher();
        // Top-level dict with a nested array of 30 identical items.
        // The inner array should compress (low_uniqueness path).
        let mut inner = String::from("[");
        for i in 0..30 {
            if i > 0 {
                inner.push(',');
            }
            inner.push_str(r#"{"status":"ok"}"#);
        }
        inner.push(']');
        let input = format!(r#"{{"data": {}}}"#, inner);
        let result = c.crush(&input, "", 1.0);
        assert!(
            result.was_modified,
            "nested compressible array must be crushed even inside a wrapper object"
        );
    }

    #[test]
    fn crusher_with_custom_scorer() {
        use crate::relevance::BM25Scorer;
        let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
            .with_scorer(Box::new(BM25Scorer::default()))
            .add_default_oss_constraints()
            .build();
        // Sanity: crushing still works with a swapped scorer.
        let items: Vec<Value> = (0..30).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "anything", 1.0);
        assert!(result.items.len() <= 30);
    }

    // ---------- lossless-first default with threshold + CCR-Dropped ----------

    #[test]
    fn without_compaction_yields_none_compacted_field() {
        // The opt-out constructor preserves the lossy-only path.
        // No lossless attempt → compacted/compaction_kind always None.
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        let items: Vec<Value> = (0..30).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert!(result.compacted.is_none());
        assert!(result.compaction_kind.is_none());
    }

    #[test]
    fn lossless_wins_when_savings_above_threshold() {
        // 50 uniform tabular dicts → CSV+schema compaction shrinks the
        // input well above the 0.30 gate, so the LOSSLESS render is a
        // valid candidate. Under `LosslessFirst` it MUST ship (all rows
        // visible, nothing dropped) whenever it clears the gate — that is
        // exactly what this policy guarantees and what this test pins.
        //
        // (The DEFAULT `MinTokens` policy may instead ship the equally-
        // recoverable lossy survivor render when it is fewer tokens —
        // both views are 100% recoverable, so that is a pure size win, not
        // information loss. The MinTokens routing race is covered by the
        // dedicated routing tests below; here we assert the lossless
        // render itself is built and chosen by the policy that owns it.)
        let cfg = SmartCrusherConfig {
            routing_policy: RoutingPolicy::LosslessFirst,
            ..Default::default()
        };
        let c = SmartCrusher::new(cfg);
        let items: Vec<Value> = (0..50)
            .map(|i| json!({"id": i, "name": format!("u_{i}"), "status": "ok"}))
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        let compacted = result.compacted.expect("compacted should be set");
        assert!(compacted.starts_with("[50]{"), "got: {compacted}");
        assert_eq!(result.compaction_kind, Some("table"));
        assert!(
            result.strategy_info.starts_with("lossless:table"),
            "got: {}",
            result.strategy_info
        );
        // Lossless = nothing dropped → no CCR retrieval needed.
        assert!(result.ccr_hash.is_none());
        // items preserved (full original).
        assert_eq!(result.items.len(), 50);
    }

    #[test]
    fn lossy_falls_through_when_savings_below_threshold() {
        // Force the threshold high enough that even tabular savings
        // can't satisfy it → lossy path runs → CCR-Dropped fires.
        // Use low-uniqueness items so the analyzer is willing to
        // crush (unique id+name per row would trip the
        // "unique_entities_no_signal" skip gate instead).
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99,
            ..Default::default()
        };
        let c = SmartCrusher::new(cfg);
        let items: Vec<Value> = (0..50).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "", 1.0);
        // Lossless declined → no compacted output.
        assert!(result.compacted.is_none());
        // Lossy ran → rows dropped.
        assert!(
            result.items.len() < 50,
            "expected lossy drop, got {} items",
            result.items.len()
        );
        // CCR hash populated for retrieval.
        let h = result.ccr_hash.expect("ccr_hash populated on drop");
        assert_eq!(h.len(), 12);
        // Marker visible in dropped_summary.
        assert!(
            result.dropped_summary.contains(&format!("<<ccr:{h}")),
            "got: {}",
            result.dropped_summary
        );
        assert!(result.dropped_summary.contains("rows_offloaded"));
    }

    #[test]
    fn ccr_hash_is_deterministic() {
        // Same input → same hash, so the runtime cache key is stable.
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // force lossy path
            ..Default::default()
        };
        let c = SmartCrusher::new(cfg);
        let items: Vec<Value> = (0..30).map(|i| json!({"id": i, "tag": "ok"})).collect();
        let r1 = c.crush_array(&items, "", 1.0);
        let r2 = c.crush_array(&items, "", 1.0);
        assert_eq!(r1.ccr_hash, r2.ccr_hash);
        assert!(r1.ccr_hash.is_some());
    }

    #[test]
    fn ccr_hash_changes_with_input() {
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99,
            ..Default::default()
        };
        let c = SmartCrusher::new(cfg);
        let a: Vec<Value> = (0..30).map(|i| json!({"id": i})).collect();
        let b: Vec<Value> = (100..130).map(|i| json!({"id": i})).collect();
        let ra = c.crush_array(&a, "", 1.0);
        let rb = c.crush_array(&b, "", 1.0);
        assert_ne!(ra.ccr_hash, rb.ccr_hash);
    }

    #[test]
    fn lossy_without_compaction_still_emits_ccr_hash() {
        // The CCR-Dropped restoration applies regardless of whether
        // lossless was attempted — without_compaction also gets the
        // ccr_hash on row drops.
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        let items: Vec<Value> = (0..30).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "", 1.0);
        if result.items.len() < items.len() {
            assert!(result.ccr_hash.is_some());
            assert!(!result.dropped_summary.is_empty());
        }
    }

    #[test]
    fn passthrough_paths_do_not_emit_ccr_hash() {
        // Tier-1 boundary (items.len() <= adaptive_k): nothing
        // dropped, no CCR. Skip path: same.
        let c = crusher();
        let small: Vec<Value> = (0..3).map(|i| json!({"id": i})).collect();
        let r = c.crush_array(&small, "", 1.0);
        assert!(r.ccr_hash.is_none());
        assert_eq!(r.dropped_summary, "");
    }

    #[test]
    fn compaction_skips_non_object_array() {
        // Compactor returns Untouched for non-object arrays → no
        // compacted field populated, no kind tag.
        let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
            .with_default_oss_setup()
            .with_default_compaction()
            .build();
        let items: Vec<Value> = (0..30).map(|i| json!(i)).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert!(result.compacted.is_none());
        assert!(result.compaction_kind.is_none());
    }

    // ---------- walker-integration in process_value ----------

    #[test]
    fn process_string_short_string_passthrough() {
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        let (out, info) = c.process_value(&json!("hello world"), 0, "", 1.0);
        assert_eq!(out, json!("hello world"));
        assert!(info.is_empty());
    }

    #[test]
    fn process_string_stringified_json_array_recurses() {
        // A string-typed field whose value is a JSON-encoded array of
        // dicts. process_value should parse it, recurse, and return
        // the processed JSON re-rendered as a string.
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        let big_array_json = serde_json::to_string(
            &(0..50)
                .map(|i| json!({"id": i, "level": "info", "msg": "ok"}))
                .collect::<Vec<_>>(),
        )
        .unwrap();
        let doc = json!({"payload": big_array_json.clone()});
        let (out, info) = c.process_value(&doc, 0, "", 1.0);
        // payload still a string-typed field — we preserved the
        // wrapping shape — but its content was processed.
        let payload = out.pointer("/payload").and_then(|v| v.as_str()).unwrap();
        // Either compressed or unchanged; if compressed, info reflects.
        // For 50 items with low-uniqueness, compression should fire.
        // The strategy info should mention string_json processing.
        assert!(
            info.contains("string_json") || payload != big_array_json,
            "expected processing trace; info={info}, len before={}, after={}",
            big_array_json.len(),
            payload.len(),
        );
    }

    #[test]
    fn process_string_opaque_blob_becomes_ccr_marker() {
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        let big_b64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=".repeat(8);
        let doc = json!({"id": 1, "blob": big_b64});
        let (out, _info) = c.process_value(&doc, 0, "", 1.0);
        let blob = out.pointer("/blob").and_then(|v| v.as_str()).unwrap();
        assert!(blob.starts_with("<<ccr:"), "got: {blob}");
        assert!(blob.contains(",base64,"));
    }

    #[test]
    fn process_string_top_level_string_processed() {
        // crush() takes a string; if it doesn't parse as JSON, today's
        // behavior returns it unchanged. But if it's a stringified
        // JSON object/array, it should now get processed.
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        // Non-JSON top-level string — passthrough.
        let plain = "just some plain text";
        let result = c.crush(plain, "", 1.0);
        assert_eq!(result.compressed, plain);
    }

    #[test]
    fn process_string_does_not_alter_short_quoted_strings() {
        // Strings that look JSON-like but are short shouldn't be
        // CCR-substituted.
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        let doc = json!({"msg": "{this looks like json but isnt}"});
        let (out, _) = c.process_value(&doc, 0, "", 1.0);
        assert_eq!(out, doc);
    }

    #[test]
    fn process_string_helper_parses_only_containers() {
        assert!(try_parse_json_container("{\"a\":1}").is_some());
        assert!(try_parse_json_container("[1,2,3]").is_some());
        assert!(try_parse_json_container("123").is_none()); // bare scalar
        assert!(try_parse_json_container("\"hello\"").is_none()); // bare string
        assert!(try_parse_json_container("not json").is_none());
        assert!(try_parse_json_container("{malformed").is_none());
    }

    // ---------- recovery-pointer invariant (Defect 1) ----------

    #[test]
    fn enable_ccr_marker_false_still_surfaces_recovery_pointer() {
        // Defect 1 (kill silent loss, completed). With
        // `enable_ccr_marker=false` the engine STILL surfaces the
        // `<<ccr:HASH>>` recovery pointer in `dropped_summary` AND
        // writes the store. The recovery pointer is the retrieval key,
        // not a UX nicety: you cannot drop a distinct item without
        // leaving a pointer to it. Suppressing the pointer while still
        // dropping rows was the silent-loss bug this test now guards
        // against.
        //
        // (Previously this test asserted the OLD behavior —
        // `dropped_summary.is_empty()` — which encoded exactly the
        // silent loss the recovery invariant forbids on the public
        // path. Fixture flipped to assert the invariant.)
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;
        use std::sync::Arc;

        let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // force lossy path
            enable_ccr_marker: false,
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusherBuilder::new(cfg)
            .with_ccr_store(Arc::clone(&store))
            .build();
        let items: Vec<Value> = (0..50).map(|_| json!({"status": "ok"})).collect();

        let store_len_before = store.len();
        let result = c.crush_array(&items, "", 1.0);
        let store_len_after = store.len();

        // Rows were dropped (we built 50, kept fewer).
        assert!(result.items.len() < items.len(), "lossy path didn't fire");
        // The recovery pointer IS surfaced even with the marker flag off.
        assert!(
            result.dropped_summary.contains("<<ccr:"),
            "dropped_summary must carry the recovery pointer even with \
             enable_ccr_marker=false (Defect 1), got: {:?}",
            result.dropped_summary
        );
        assert!(result.dropped_summary.contains("rows_offloaded"));
        // The hash is returned so callers can mirror/retrieve.
        let h = result
            .ccr_hash
            .as_ref()
            .expect("ccr_hash should be returned on a drop");
        // The pointer text references the same hash.
        assert!(
            result.dropped_summary.contains(h.as_str()),
            "the pointer must reference the returned hash"
        );
        // ...and the store DID grow — persistence is unconditional.
        assert!(
            store_len_after > store_len_before,
            "ccr_store must grow on a drop (kill silent loss)"
        );
        // The dropped payload is recoverable: the canonical original
        // array round-trips out of the store under the returned hash.
        let recovered = store.get(h).expect("dropped payload must be retrievable");
        let canonical = canonical_array_json(&items);
        assert_eq!(
            recovered, canonical,
            "recovered payload must equal the canonical original array"
        );
    }

    #[test]
    fn ccr_backed_store_tightens_lossy_budget_vs_storeless() {
        // With a CCR store every dropped row is recoverable, so the
        // lossy keep budget halves; without a store the legacy full
        // `adaptive_k` budget applies (a tighter budget there would
        // drop unrecoverable rows for nothing).
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;
        use std::sync::Arc;

        let mk_cfg = || SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // force lossy path
            ..SmartCrusherConfig::default()
        };
        let items: Vec<Value> = (0..60)
            .map(|i| json!({"msg": format!("entirely distinct message number {}", i)}))
            .collect();

        let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
        let with_store = SmartCrusherBuilder::new(mk_cfg())
            .with_ccr_store(Arc::clone(&store))
            .build();
        let without_store = SmartCrusherBuilder::new(mk_cfg()).build();

        let r_store = with_store.crush_array(&items, "", 1.0);
        let r_legacy = without_store.crush_array(&items, "", 1.0);

        assert!(
            r_store.items.len() < r_legacy.items.len(),
            "store-backed budget must keep fewer rows ({} vs {})",
            r_store.items.len(),
            r_legacy.items.len()
        );
        // Everything dropped under the tightened budget is recoverable.
        let h = r_store.ccr_hash.as_ref().expect("hash on drop");
        let recovered = store.get(h).expect("dropped payload retrievable");
        assert_eq!(recovered, canonical_array_json(&items));
    }

    /// One realistic git-log-shaped row: identity columns (40-hex commit,
    /// ISO date), a low-cardinality author, and a genuinely varied unique
    /// subject built from rotating conventional-commit vocabulary.
    fn log_shaped_row(i: usize) -> Value {
        const PREFIXES: [&str; 8] = [
            "feat", "fix", "docs", "chore", "refactor", "test", "perf", "ci",
        ];
        const AREAS: [&str; 10] = [
            "crusher", "proxy", "ccr", "router", "bench", "tokenizer", "store", "pipeline",
            "compaction", "relevance",
        ];
        const VERBS: [&str; 10] = [
            "add", "remove", "rework", "guard", "pin", "extend", "isolate", "deflake",
            "speed up", "harden",
        ];
        const THINGS: [&str; 15] = [
            "the lossy budget",
            "novelty fill",
            "sentinel emission",
            "marker parsing",
            "store mirroring",
            "field-role gates",
            "ditto marks",
            "schema folding",
            "query anchors",
            "drop accounting",
            "TTL handling",
            "thread-local state",
            "import guards",
            "error surfaces",
            "byte parity",
        ];
        json!({
            "commit": format!("{:040x}", (i as u128 * 2_654_435_761 + 12_345)),
            "author": format!("Author {}", i % 7),
            "date": format!(
                "2026-{:02}-{:02}T{:02}:{:02}:00+02:00",
                (i % 12) + 1,
                (i % 28) + 1,
                i % 24,
                (i * 13) % 60
            ),
            "subject": format!(
                "{}({}): {} {} #{}",
                PREFIXES[i % 8],
                AREAS[i % 10],
                VERBS[i % 10],
                THINGS[i % 15],
                i + 100
            ),
        })
    }

    #[test]
    fn lossy_survivor_compaction_ships_table_with_sentinel_line() {
        // When the lossy path drops rows AND the survivors render as a
        // smaller CSV-schema table, the output is the rendering with the
        // `{"_ccr_dropped": ...}` sentinel appended as the final line.
        // Every survivor value stays verbatim; the dropped rows stay
        // recoverable under the surfaced hash.
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;
        use std::sync::Arc;

        let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // force the lossy path
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusherBuilder::new(cfg)
            .with_default_compaction()
            .with_ccr_store(Arc::clone(&store))
            .build();
        // High-entropy distinct rows (git-log shaped): hex/ISO identity
        // columns, repeating author, genuinely varied unique subjects
        // (uniformly-templated subjects trip the
        // `skip:unique_entities_no_signal` crushability gate and never
        // reach the lossy path — mirroring how real logs behave).
        let items: Vec<Value> = (0..60).map(log_shaped_row).collect();

        let result = c.crush_array(&items, "", 1.0);
        assert!(result.items.len() < items.len(), "lossy path didn't fire");

        let rendered = result
            .compacted
            .as_ref()
            .expect("survivor compaction should win on key-heavy log rows");
        // Sentinel is the final line and carries the recovery pointer.
        let last_line = rendered.lines().last().expect("non-empty rendering");
        assert!(
            last_line.starts_with("{\"_ccr_dropped\":"),
            "sentinel must be the final line, got: {last_line:?}"
        );
        assert!(last_line.contains("<<ccr:"), "sentinel carries the pointer");
        // Every survivor's subject is verbatim in the rendering.
        for row in &result.items {
            let subject = row["subject"].as_str().unwrap();
            assert!(
                rendered.contains(subject),
                "survivor value must stay verbatim: {subject}"
            );
        }
        // Dropped rows recoverable under the surfaced hash.
        let h = result.ccr_hash.as_ref().expect("hash on drop");
        assert!(last_line.contains(h.as_str()), "pointer names the hash");
        let recovered = store.get(h).expect("dropped payload retrievable");
        assert_eq!(recovered, canonical_array_json(&items));
    }

    #[test]
    fn enable_ccr_marker_true_is_default_behavior() {
        // Default config still emits markers + stores when rows drop.
        // Sanity: the gate is opt-out, not opt-in.
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;
        use std::sync::Arc;

        let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // force lossy path
            ..SmartCrusherConfig::default()
        };
        // Default: enable_ccr_marker = true.
        assert!(cfg.enable_ccr_marker);
        let c = SmartCrusherBuilder::new(cfg)
            .with_ccr_store(Arc::clone(&store))
            .build();
        let items: Vec<Value> = (0..50).map(|_| json!({"status": "ok"})).collect();

        let store_len_before = store.len();
        let result = c.crush_array(&items, "", 1.0);
        let store_len_after = store.len();

        assert!(result.items.len() < items.len(), "lossy path didn't fire");
        assert!(result.ccr_hash.is_some(), "default should produce a hash");
        assert!(
            result.dropped_summary.contains("<<ccr:"),
            "default should produce a marker: {:?}",
            result.dropped_summary
        );
        assert!(
            store_len_after > store_len_before,
            "default should write to ccr_store"
        );
    }

    // ---------- 1A non-dict silent-loss regression (adversarial) ----------
    //
    // The defect these pin: the NON-dict crush paths
    // (`crush_string_array`, `crush_number_array`, `crush_mixed_array`)
    // dropped distinct items with NO store write and NO sentinel — a
    // dropped needle was *silently* lost (markers=[], store empty,
    // `ccr_get` returns nothing). Now `process_value`'s String/Number/
    // Mixed branches route the full original through `persist_dropped`
    // and append a `_ccr_dropped` sentinel, so every distinct dropped
    // item is recoverable via `ccr_get(hash)` — same guarantee the dict
    // path already had.

    use std::collections::HashSet;

    /// Build a default-config crusher with an in-memory CCR store
    /// attached, returning both so tests can inspect the store.
    fn crusher_with_store() -> (SmartCrusher, std::sync::Arc<crate::ccr::InMemoryCcrStore>) {
        use crate::ccr::InMemoryCcrStore;
        use std::sync::Arc;
        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
            .with_default_oss_setup()
            .with_default_compaction()
            .with_ccr_store(store_dyn)
            .build();
        (c, store)
    }

    /// Recursively collect every `<<ccr:HASH N_rows_offloaded>>` hash
    /// from a crushed JSON tree (string-leaf markers AND `_ccr_dropped`
    /// object sentinels) plus every kept scalar's canonical repr.
    fn collect_scalars_and_hashes(
        node: &Value,
        scalars: &mut HashSet<String>,
        hashes: &mut Vec<String>,
    ) {
        match node {
            Value::Array(a) => {
                for x in a {
                    collect_scalars_and_hashes(x, scalars, hashes);
                }
            }
            Value::Object(map) => {
                if let Some(Value::String(s)) = map.get("_ccr_dropped") {
                    if let Some(h) = extract_ccr_hash(s) {
                        hashes.push(h);
                    }
                    // GRANULAR retrieval: the `_ccr_rows` marker names the
                    // per-blob row index (`{hash}#rows`). The index key is
                    // collected so the caller can resolve it to per-row
                    // hashes and prove each row is individually
                    // addressable + recoverable.
                    if let Some(Value::String(idx)) = map.get("_ccr_rows") {
                        if let Some(h) = extract_ccr_hash(idx) {
                            hashes.push(h);
                        }
                    }
                    return;
                }
                for v in map.values() {
                    collect_scalars_and_hashes(v, scalars, hashes);
                }
            }
            Value::String(s) => {
                if let Some(h) = extract_ccr_hash(s) {
                    hashes.push(h);
                } else {
                    scalars.insert(canonical_json_for_match(node));
                }
            }
            _ => {
                scalars.insert(canonical_json_for_match(node));
            }
        }
    }

    /// Pull the 12-char hash out of a `<<ccr:HASH N_rows_offloaded>>`
    /// marker string, if present.
    fn extract_ccr_hash(s: &str) -> Option<String> {
        let start = s.find("<<ccr:")? + "<<ccr:".len();
        let rest = &s[start..];
        let end = rest.find(' ')?;
        Some(rest[..end].to_string())
    }

    /// Run the full public `crush()` path over `items`, then assert that
    /// EVERY distinct input is recoverable: present in the kept output OR
    /// restorable from the CCR store under an emitted hash. Returns
    /// `(total, recovered, n_markers, store_len)`.
    fn assert_no_silent_loss(
        c: &SmartCrusher,
        store: &crate::ccr::InMemoryCcrStore,
        items: &[Value],
    ) {
        let content = serde_json::to_string(items).unwrap();
        let result = c.crush(&content, "", 1.0);
        let out: Value = serde_json::from_str(&result.compressed).unwrap();

        let mut kept_scalars: HashSet<String> = HashSet::new();
        let mut hashes: Vec<String> = Vec::new();
        collect_scalars_and_hashes(&out, &mut kept_scalars, &mut hashes);

        // A drop must emit at least one marker + populate the store.
        assert!(
            !hashes.is_empty(),
            "expected a <<ccr:..>> sentinel after dropping rows; got none. compressed={}",
            &result.compressed[..result.compressed.len().min(200)]
        );
        assert!(store.len() > 0, "ccr_store must be populated on drop");

        // Resolve every surfaced hash. With the granular model a drop
        // surfaces BOTH the whole-blob pointer (`_ccr_dropped`) and one
        // per-row pointer per original row (`_ccr_rows`). When the array
        // is large enough that the per-row chunks fill the bounded LRU,
        // the (now-redundant) whole-blob entry MAY be evicted — that is
        // acceptable precisely because the granular chunks recover every
        // row on their own. So a hash that fails to resolve is tolerated
        // here; the real invariant — every distinct input recovered — is
        // asserted on the final `recovered` set below.
        let mut recovered: HashSet<String> = kept_scalars;
        let mut n_resolved = 0usize;
        for h in &hashes {
            let Some(payload) = store.get(h) else {
                continue;
            };
            n_resolved += 1;
            let restored: Vec<Value> = serde_json::from_str(&payload).unwrap();
            // A `{hash}#rows` ROW INDEX resolves to a JSON array of per-row
            // hash STRINGS, not rows. Follow each one — the proportional
            // retrieval path: one `ccr_get(row_hash)` per needed row,
            // each returning exactly `[row]`. This is what makes a single
            // needed row cost ONE row, not the whole blob.
            if h.ends_with("#rows") {
                for hv in &restored {
                    if let Value::String(row_hash) = hv {
                        if let Some(row_payload) = store.get(row_hash) {
                            let row_arr: Vec<Value> =
                                serde_json::from_str(&row_payload).unwrap();
                            for x in row_arr {
                                recovered.insert(canonical_json_for_match(&x));
                            }
                        }
                    }
                }
                continue;
            }
            for x in restored {
                recovered.insert(canonical_json_for_match(&x));
            }
        }
        assert!(
            n_resolved > 0,
            "at least one surfaced <<ccr:..>> hash must resolve in the store"
        );

        let distinct_inputs: HashSet<String> =
            items.iter().map(canonical_json_for_match).collect();
        let lost: Vec<&String> = distinct_inputs.difference(&recovered).collect();
        assert!(
            lost.is_empty(),
            "{} distinct items silently lost (recovered {}/{}); first lost: {:?}",
            lost.len(),
            distinct_inputs.len() - lost.len(),
            distinct_inputs.len(),
            lost.iter().take(3).collect::<Vec<_>>()
        );
    }

    #[test]
    fn string_array_drops_are_ccr_recoverable() {
        // 1000 distinct strings → adversarial counterexample (was 964
        // silently lost).
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..1000)
            .map(|i| json!(format!("log-line-entry-number-{i}-payload")))
            .collect();
        assert_no_silent_loss(&c, &store, &items);
    }

    #[test]
    fn number_array_drops_are_ccr_recoverable() {
        // 1000 distinct numbers (was 985 silently lost).
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..1000).map(|i| json!(i)).collect();
        assert_no_silent_loss(&c, &store, &items);
    }

    #[test]
    fn mixed_array_drops_are_ccr_recoverable() {
        // 700 mixed str+number items (was 679 silently lost).
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..700)
            .map(|i| {
                if i % 2 == 0 {
                    json!(format!("event-{i}"))
                } else {
                    json!(i)
                }
            })
            .collect();
        assert_no_silent_loss(&c, &store, &items);
    }

    #[test]
    fn unicode_string_array_drops_are_ccr_recoverable() {
        // 1000 distinct unicode strings → the canonical bytes + hash
        // round-trip non-ASCII content losslessly.
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..1000)
            .map(|i| {
                let cp = char::from_u32(0x4E00 + (i % 2000) as u32).unwrap_or('日');
                json!(format!("café-{i}-日本語-{cp}"))
            })
            .collect();
        assert_no_silent_loss(&c, &store, &items);
    }

    #[test]
    fn dict_array_recovery_still_green_after_refactor() {
        // Control: the dict path (already 1A-covered) must keep
        // recovering 100% after extracting the shared `persist_dropped`
        // helper. Pins that the refactor didn't regress the dict path.
        let (c, store) = crusher_with_store();
        // Low-uniqueness dicts so the analyzer is willing to crush, and
        // a high lossless threshold so the lossy/CCR path fires rather
        // than lossless compaction.
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99,
            ..SmartCrusherConfig::default()
        };
        let store_dyn: std::sync::Arc<dyn CcrStore> =
            std::sync::Arc::clone(&store) as std::sync::Arc<dyn CcrStore>;
        let c2 = SmartCrusherBuilder::new(cfg)
            .with_default_oss_setup()
            .with_default_compaction()
            .with_ccr_store(store_dyn)
            .build();
        let _ = &c; // silence: reuse store handle from helper
        let items: Vec<Value> = (0..200).map(|i| json!({"status": "ok", "seq": i})).collect();
        assert_no_silent_loss(&c2, &store, &items);
    }

    #[test]
    fn persist_dropped_hash_is_byte_identical_to_inline_dict_scheme() {
        // The shared helper must produce the SAME hash the dict path
        // produced inline before the refactor: SHA-256(canonical) → 12
        // hex chars over `canonical_array_json(items)`. Pin it so the
        // CCR retrieve contract is provably unchanged.
        let (c, _store) = crusher_with_store();
        let items: Vec<Value> = (0..30).map(|i| json!({"id": i})).collect();
        let persisted = c
            .persist_dropped(&items, 5)
            .expect("dropped_count>0 → Some");
        let expected = hash_canonical(&canonical_array_json(&items));
        assert_eq!(persisted.hash, expected, "hash scheme must be unchanged");
        assert_eq!(persisted.hash.len(), 12);
        assert!(persisted.marker.contains(&format!("<<ccr:{expected} 5_rows_offloaded>>")));
        // Zero dropped → None (no hash, no marker, no store write).
        assert!(c.persist_dropped(&items, 0).is_none());
    }

    #[test]
    fn non_dict_drop_surfaces_pointer_and_persists_even_with_marker_off() {
        // Defect 1: parity with the dict path. With
        // `enable_ccr_marker=false`, the non-dict string path STILL
        // surfaces the `<<ccr:HASH>>` recovery pointer in the output AND
        // writes the store. The pointer is the recovery key; suppressing
        // it while still dropping rows is the silent loss the invariant
        // forbids. The hash carried by the pointer keys the canonical
        // original in the store → fully recoverable from the output.
        use crate::ccr::InMemoryCcrStore;
        use std::sync::Arc;

        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        let cfg = SmartCrusherConfig {
            enable_ccr_marker: false,
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusherBuilder::new(cfg)
            .with_default_oss_setup()
            .with_default_compaction()
            .with_ccr_store(store_dyn)
            .build();

        let items: Vec<Value> = (0..200)
            .map(|i| json!(format!("distinct-string-{i}")))
            .collect();
        let content = serde_json::to_string(&items).unwrap();

        let store_before = store.len();
        let result = c.crush(&content, "", 1.0);
        let store_after = store.len();

        // The recovery pointer IS in the output even with the flag off.
        assert!(
            result.compressed.contains("<<ccr:"),
            "recovery pointer must be surfaced even with enable_ccr_marker=false \
             (Defect 1), got: {}",
            &result.compressed[..result.compressed.len().min(200)]
        );
        // The store grew — persistence is unconditional.
        assert!(
            store_after > store_before,
            "ccr_store must grow on a drop (kill silent loss)"
        );
        // The pointer carries the hash that keys the canonical original.
        let expected_hash = hash_canonical(&canonical_array_json(&items));
        assert!(
            result.compressed.contains(&expected_hash),
            "the surfaced pointer must reference the canonical hash"
        );
        let recovered = store
            .get(&expected_hash)
            .expect("non-dict drop must be retrievable by hash");
        assert_eq!(recovered, canonical_array_json(&items));
    }

    // ---------- Phase 7: route-by-min-tokens ----------

    /// Build a default-config crusher (MinTokens) plus a LosslessFirst
    /// twin, both sharing one in-memory CCR store, so a routing test can
    /// compare the two policies and still recover any dropped rows.
    fn min_tokens_and_lossless_first(
    ) -> (SmartCrusher, SmartCrusher, std::sync::Arc<crate::ccr::InMemoryCcrStore>) {
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;
        use std::sync::Arc;
        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        let mk = |policy: RoutingPolicy| {
            SmartCrusherBuilder::new(SmartCrusherConfig {
                routing_policy: policy,
                ..SmartCrusherConfig::default()
            })
            .with_default_oss_setup()
            .with_default_compaction()
            .with_ccr_store(Arc::clone(&store_dyn))
            .build()
        };
        (mk(RoutingPolicy::MinTokens), mk(RoutingPolicy::LosslessFirst), store)
    }

    #[test]
    fn min_tokens_ships_lossy_for_logs_shaped_data() {
        // Logs-shaped: per-row entropy (40-hex commit + distinct subject)
        // shipped 90× makes the lossless render token-expensive; dropping
        // to a small visible sample + a `<<ccr:HASH>>` sentinel is far
        // fewer tokens. MinTokens must pick the lossy DROP render — and
        // the dropped rows must remain recoverable from the store.
        let (min_tokens, lossless_first, store) = min_tokens_and_lossless_first();
        let items: Vec<Value> = (0..90).map(log_shaped_row).collect();

        let r_min = min_tokens.crush_array(&items, "", 1.0);
        let r_loss = lossless_first.crush_array(&items, "", 1.0);

        // MinTokens drops (lossy chosen): a hash is surfaced.
        assert!(
            r_min.ccr_hash.is_some(),
            "MinTokens must ship the lossy DROP render for logs-shaped data; got strategy {:?}",
            r_min.strategy_info
        );
        assert!(r_min.items.len() < items.len(), "lossy must actually drop rows");

        // The chosen lossy render is fewer tokens than the lossless one.
        let lossy_tokens = min_tokens.render_token_count(&r_min);
        let lossless_tokens = lossless_first.render_token_count(&r_loss);
        assert!(
            lossy_tokens < lossless_tokens,
            "lossy must be strictly fewer tokens (lossy={lossy_tokens}, lossless={lossless_tokens})"
        );

        // LosslessFirst ships the lossless render for the same data.
        assert!(
            r_loss.ccr_hash.is_none() && r_loss.compacted.is_some(),
            "LosslessFirst must ship the lossless render; got strategy {:?}",
            r_loss.strategy_info
        );

        // Recovery proof: every dropped row is retrievable from the store
        // under the surfaced hash (the chosen lossy render loses nothing).
        let h = r_min.ccr_hash.as_ref().unwrap();
        let recovered = store.get(h).expect("dropped payload retrievable");
        assert_eq!(recovered, canonical_array_json(&items));
    }

    #[test]
    fn min_tokens_ships_lossless_when_it_is_fewer_tokens() {
        // A low-cardinality tabular array whose every row collapses under
        // dedup: the lossy path keeps the same content the lossless table
        // shows, so the lossless render is ≤ tokens. Under MinTokens the
        // tie-or-fewer goes to lossless (more rows visible). Nothing is
        // dropped → the output is recoverable inline, no CCR needed.
        let (min_tokens, lossless_first, _store) = min_tokens_and_lossless_first();
        let items: Vec<Value> = (0..12).map(|_| json!({"a": 1, "b": 2})).collect();

        let r_min = min_tokens.crush_array(&items, "", 1.0);
        let r_loss = lossless_first.crush_array(&items, "", 1.0);

        // MinTokens ships lossless: nothing dropped, compacted populated.
        assert!(
            r_min.ccr_hash.is_none() && r_min.compacted.is_some(),
            "MinTokens must ship the lossless render when it is ≤ tokens; got strategy {:?}",
            r_min.strategy_info
        );
        assert_eq!(r_min.items.len(), items.len(), "lossless drops nothing");

        // LosslessFirst ships lossless too (same render for this shape).
        assert!(
            r_loss.ccr_hash.is_none() && r_loss.compacted.is_some(),
            "LosslessFirst must ship lossless here; got strategy {:?}",
            r_loss.strategy_info
        );
        // The chosen render is identical across policies in this case.
        assert_eq!(r_min.compacted, r_loss.compacted);
    }

    #[test]
    fn min_tokens_never_ships_more_tokens_than_lossless() {
        // The core invariant: under MinTokens the shipped render is never
        // MORE tokens than the lossless render would have been — for any
        // droppable array where both candidates exist. (Lossy wins only
        // when STRICTLY fewer; ties go to lossless.) Pin it on the
        // logs-shaped family where lossy genuinely wins.
        let (min_tokens, lossless_first, _store) = min_tokens_and_lossless_first();
        let items: Vec<Value> = (0..90).map(log_shaped_row).collect();

        let r_min = min_tokens.crush_array(&items, "", 1.0);
        let r_loss = lossless_first.crush_array(&items, "", 1.0);

        let min_tokens_count = min_tokens.render_token_count(&r_min);
        let lossless_tokens = lossless_first.render_token_count(&r_loss);
        assert!(
            min_tokens_count <= lossless_tokens,
            "MinTokens must never ship more tokens than lossless \
             (chosen={min_tokens_count}, lossless={lossless_tokens})"
        );
    }

    // ---------- U8: single compaction pass on large-array hot path ----------

    /// A [`Formatter`] spy that counts how many times `format` is called.
    /// Each call to `CompactionStage::run` calls `format` exactly once, so
    /// this is a direct proxy for the number of `stage.run(items)` calls.
    struct CountingFormatter {
        inner: Box<dyn super::super::compaction::Formatter>,
        count: Arc<std::sync::atomic::AtomicUsize>,
    }

    impl super::super::compaction::Formatter for CountingFormatter {
        fn name(&self) -> &str {
            self.inner.name()
        }
        fn format(&self, c: &super::super::compaction::Compaction) -> String {
            self.count
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            self.inner.format(c)
        }
    }

    /// Build a [`SmartCrusher`] wired with a [`CountingFormatter`] and
    /// return the call-count handle alongside the crusher.
    fn crusher_with_counting_compaction(
        cfg: SmartCrusherConfig,
    ) -> (SmartCrusher, Arc<std::sync::atomic::AtomicUsize>) {
        use super::super::compaction::{CompactConfig, CompactionStage, CsvSchemaFormatter};
        let counter = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let stage = CompactionStage {
            config: CompactConfig::default(),
            formatter: Box::new(CountingFormatter {
                inner: Box::new(CsvSchemaFormatter::new()),
                count: Arc::clone(&counter),
            }),
        };
        let crusher = SmartCrusherBuilder::new(cfg)
            .with_default_oss_setup()
            .with_compaction(stage)
            .build();
        (crusher, counter)
    }

    /// RED test (TDD step 1): before the fix, crush_array calls stage.run
    /// TWICE unnecessarily on a large compactable array — once for
    /// lossless_candidate (line 796) and a second redundant time for
    /// lossless_uses_opaque (line 843).  After the fix the second call is
    /// eliminated and the compaction result is reused.
    ///
    /// Shape: unique-entity rows (no CCR store) → lossy path returns Skip
    /// (no rows dropped → `dropped_summary` is empty → the survivor-
    /// compaction branch inside crush_array_lossy does NOT fire).  Only the
    /// lossless_candidate call and the now-redundant lossless_uses_opaque call
    /// are in-scope.
    ///
    /// Before fix: 2 calls (lossless_candidate + lossless_uses_opaque).
    /// After fix:  1 call  (lossless_candidate only; result reused for opaque).
    #[test]
    fn crush_array_large_compactable_invokes_compaction_stage_exactly_once() {
        // 30 unique-entity rows (no CCR store → lossy skips, no drops →
        // survivor compaction doesn't fire).  Uniform tabular shape →
        // compacts well so lossless_candidate is not None.
        let items: Vec<Value> = (0..30)
            .map(|i| json!({"id": i, "user": format!("u_{i}"), "status": "ok"}))
            .collect();
        let cfg = SmartCrusherConfig {
            routing_policy: RoutingPolicy::LosslessFirst,
            lossless_min_savings_ratio: 0.0, // always accept lossless render
            ..Default::default()
        };
        let (crusher, counter) = crusher_with_counting_compaction(cfg);

        // Sanity: no CCR store on this crusher (survivor compaction guard).
        assert!(crusher.ccr_store.is_none());

        let result = crusher.crush_array(&items, "", 1.0);

        // Lossless wins → nothing dropped → survivor compaction (line 1058)
        // never runs.  Only the lossless_candidate + optional lossless_uses_opaque
        // calls count.
        assert!(
            result.compacted.is_some(),
            "lossless render must win in this test setup (strategy: {})",
            result.strategy_info
        );
        assert_eq!(result.items.len(), 30, "lossless drops nothing");

        let calls = counter.load(std::sync::atomic::Ordering::Relaxed);
        assert_eq!(
            calls, 1,
            "crush_array must invoke the compaction stage EXACTLY once on the \
             large-array hot path when lossless wins (got {calls} calls — the \
             redundant lossless_uses_opaque call must be eliminated)"
        );
    }

    /// Behavioral parity: lossless-wins case — the chosen render must be
    /// byte-identical before and after the fix. We capture output from the
    /// reference (standard) crusher and the counting crusher (same stage
    /// logic, just with the spy) to confirm the refactor does not alter the
    /// lossless output.
    #[test]
    fn crush_array_lossless_output_unchanged_after_dedup() {
        let items: Vec<Value> = (0..50)
            .map(|i| json!({"id": i, "status": "ok", "region": "us-east-1"}))
            .collect();
        let cfg = SmartCrusherConfig {
            routing_policy: RoutingPolicy::LosslessFirst,
            ..Default::default()
        };
        // Reference: normal crusher.
        let ref_crusher = SmartCrusher::new(cfg.clone());
        let ref_result = ref_crusher.crush_array(&items, "", 1.0);

        // Under test: counting spy (same compaction logic).
        let (spy_crusher, _counter) = crusher_with_counting_compaction(cfg);
        let spy_result = spy_crusher.crush_array(&items, "", 1.0);

        assert_eq!(
            ref_result.strategy_info, spy_result.strategy_info,
            "strategy_info must match"
        );
        assert_eq!(
            ref_result.compacted, spy_result.compacted,
            "compacted output must be byte-identical"
        );
        assert_eq!(
            ref_result.ccr_hash, spy_result.ccr_hash,
            "ccr_hash must match"
        );
        assert_eq!(
            ref_result.items.len(),
            spy_result.items.len(),
            "item count must match"
        );
    }

    /// Behavioral parity: lossy-wins case — when compaction savings are
    /// below threshold the lossy path fires; output must be unaffected.
    #[test]
    fn crush_array_lossy_output_unchanged_after_dedup() {
        let items: Vec<Value> = (0..50).map(|_| json!({"status": "ok"})).collect();
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // force lossy path
            ..Default::default()
        };
        let ref_crusher = SmartCrusher::new(cfg.clone());
        let ref_result = ref_crusher.crush_array(&items, "", 1.0);

        let (spy_crusher, _counter) = crusher_with_counting_compaction(cfg);
        let spy_result = spy_crusher.crush_array(&items, "", 1.0);

        assert_eq!(
            ref_result.strategy_info, spy_result.strategy_info,
            "strategy_info must match on lossy path"
        );
        assert_eq!(
            ref_result.ccr_hash, spy_result.ccr_hash,
            "ccr_hash must match on lossy path"
        );
        assert_eq!(
            ref_result.items.len(),
            spy_result.items.len(),
            "item count must match on lossy path"
        );
    }
}
