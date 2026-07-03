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
use super::classifier::{classify_array, ArrayType};
use super::compaction::{
    classify_cell, emit_opaque_ccr_marker, has_serde_private_marker, try_parse_json_container,
    CellClass, ClassifyConfig, Compaction, CompactionStage,
};
use super::config::{RoutingPolicy, SmartCrusherConfig};
use super::crushers::{compute_k_split, crush_number_array, crush_object, crush_string_array};
use super::field_role::compute_exclude_set;
use super::planning::SmartCrusherPlanner;
use super::traits::{Constraint, CrushEvent, Observer};
use super::types::{ArrayAnalysis, CompressionPlan, CompressionStrategy, CrushResult, DroppedRef};
use crate::ccr::persist::row_index_key;
use crate::ccr::{marker_for_row_index, marker_for_rows_offloaded, CcrStore};
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
    /// Top-level [`Compaction`] variant tag. Mirrors `compacted` —
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
        persisted.row_index_marker().as_deref(),
    ))
}

/// One deferred CCR store write (`key` → `payload`). Captured by
/// [`SmartCrusher::persist_dropped`] under [`PersistMode::Collect`] in
/// commit order (granular chunks → row index → whole-blob, the same
/// eviction-friendly order the direct writes use) and replayed by
/// [`SmartCrusher::commit_ccr_writes`] iff the lossy render ships.
struct CcrWrite {
    key: String,
    payload: String,
}

/// How [`SmartCrusher::persist_dropped`] treats the CCR store writes for
/// a drop. The hash, whole-blob marker and granular row-index marker are
/// computed IDENTICALLY in every mode (COR-28 byte-parity: routing built
/// on them cannot shift) — the modes differ ONLY in what happens to the
/// store writes.
#[derive(Clone, Copy)]
enum PersistMode {
    /// Write through to the configured store immediately. For paths
    /// whose render ALWAYS ships (the non-dict string/number/mixed
    /// sentinel path) — there is no later routing decision to wait for.
    Commit,
    /// Compute the writes but DEFER them: they are returned on
    /// [`DroppedPersist::pending_writes`] and committed by the routing
    /// layer only if the lossy render is actually chosen to ship. This
    /// is the P0-4 fix: committing at build time left orphan
    /// blob + chunks + index entries behind whenever the LOSSLESS render
    /// won the MinTokens/LosslessFirst arbitration — wasted
    /// COR-4-bounded capacity under hashes no surfaced marker names.
    Collect,
    /// Never write (COR-28, mixed dict arm): the caller surfaces no
    /// marker naming this hash, so any write would be an orphan.
    Skip,
}

/// Result of [`SmartCrusher::persist_dropped`] — the CCR hash that keys
/// the stored full-original array plus the prompt-visible recovery
/// pointer text. `Some(_)` only when rows were actually dropped. Under
/// [`PersistMode::Commit`] the store write (when a store is configured)
/// has already happened by the time this is returned; under
/// [`PersistMode::Collect`] the writes ride in `pending_writes` for the
/// caller to commit iff the render ships.
struct DroppedPersist {
    /// 12-char SHA-256 hex prefix of the canonical full-original array.
    /// Always returned when something was dropped — callers may mirror
    /// or retrieve it.
    hash: String,
    /// `<<ccr:HASH N_rows_offloaded>>` recovery pointer. ALWAYS
    /// non-empty when rows were dropped (Defect 1): the pointer is the
    /// recovery key, not a UX flag, so it is surfaced unconditionally on
    /// every drop. The store write backing a SHIPPED render is likewise
    /// unconditional (immediate under `Commit`, at ship-time under
    /// `Collect`).
    marker: String,
    /// Number of granular per-row chunks the store-side row index holds
    /// (exactly the in-range dropped rows — COR-20). `None` when no
    /// store is configured to chunk into or the drop exceeded the COR-4
    /// granular budget (the array hash + whole-blob `marker` still cover
    /// recovery in that case). The single datum behind BOTH the rendered
    /// `<<ccr:{hash}#rows {n}_chunks>>` marker (derived via
    /// [`DroppedPersist::row_index_marker`]) and the typed
    /// [`DroppedRef::RowDrop`] the FFI carries — one source, no drift.
    row_index_chunks: Option<usize>,
    /// Deferred store writes, populated ONLY under
    /// [`PersistMode::Collect`] (empty under `Commit` — already written —
    /// and `Skip` — never written). Commit order is preserved: granular
    /// chunks first, then the row index, the whole-blob LAST, so the
    /// bounded-store eviction rationale documented in `persist_dropped`
    /// holds identically for deferred replay.
    pending_writes: Vec<CcrWrite>,
}

impl DroppedPersist {
    /// Compact GRANULAR-retrieval marker, e.g.
    /// `<<ccr:9f3a2b#rows 50_chunks>>`. ONE short string (not a list), so
    /// surfacing it costs ~no prompt tokens and never flips the
    /// lossy-vs-lossless routing decision. It names the per-blob ROW
    /// INDEX entry (`{hash}#rows`) the store holds: a JSON array of the
    /// per-row hashes. The retrieval layer resolves it to fetch only the
    /// needed row(s) — `ccr_get(row_hash)` returns a 1-element array
    /// holding exactly that row — instead of paying for the whole blob.
    /// Derived from `row_index_chunks` through the pinned grammar owner
    /// (`ccr::markers`), byte-identical to the pre-derivation field.
    fn row_index_marker(&self) -> Option<String> {
        self.row_index_chunks
            .map(|n| marker_for_row_index(&self.hash, n))
    }

    /// The typed carrier for this drop (§4.2) — the exact values the
    /// rendered sentinel advertises, surfaced for direct mirroring.
    fn dropped_ref(&self) -> DroppedRef {
        DroppedRef::RowDrop {
            hash: self.hash.clone(),
            row_index_chunks: self.row_index_chunks,
        }
    }
}

/// Result of the lossy-recoverable render attempt in
/// [`SmartCrusher::crush_array_lossy`].
///
/// The routing layer needs to tell two cases apart:
/// - **Crushed** — a real DROP render exists (a surfaced `<<ccr:HASH>>`
///   pointer naming the offloadable rows). This is the candidate the
///   `MinTokens` policy sizes against the lossless render.
///   `pending_ccr_writes` carries the deferred store writes backing its
///   markers ([`PersistMode::Collect`]); the routing layer commits them
///   IFF this render ships — a discarded candidate's writes are dropped
///   with it, so the store never holds entries no surfaced marker names
///   (P0-4). Empty under [`PersistMode::Skip`] (mixed dict arm).
/// - **Skip** — the analyzer refused to crush the array (e.g. all-unique
///   entities with no signal). There is NO drop alternative; the carried
///   `CrushArrayResult` is the `skip:<reason>` passthrough, shipped only
///   when there's also no lossless render.
enum LossyOutcome {
    Crushed {
        result: CrushArrayResult,
        pending_ccr_writes: Vec<CcrWrite>,
    },
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
        // Collect the typed recovery refs alongside the rendered output.
        // `smart_crush_content_collecting` threads a per-call sink through
        // the recursive walk so EVERY reduction — row-drops and opaque
        // substitutions — surfaces here as a [`DroppedRef`], carrying the
        // values the emission sites already computed. The Python shim
        // mirrors these DIRECTLY into the compression_store, so `crush()`
        // recovery no longer depends on scraping `<<ccr:...>>` out of
        // `compressed`. The sink is pure side-output: it does not change
        // which sentinels/markers get embedded, so `compressed` is
        // byte-identical to the pre-typed-field behavior
        // (grammar-characterization + compression-floor untouched by
        // construction).
        let (compressed, was_modified, info, dropped) =
            self.smart_crush_content_collecting(content, query, bias);
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
            dropped,
        }
    }

    /// `SmartCrusher._smart_crush_content` (Python line 2243-2301).
    /// JSON-parse, recursively process, re-serialize. CCR marker
    /// injection is stubbed (CCR is disabled in this stage).
    ///
    /// Returns `(crushed_content, was_modified, info)`.
    ///
    /// Public wrapper that discards the typed-ref sink. Deprecated in
    /// favor of [`smart_crush_content_typed`](Self::smart_crush_content_typed)
    /// (§4.2 R3/R4) — callers that mirror recovery need the refs; parity
    /// callers that only want the tuple keep this shape.
    pub fn smart_crush_content(
        &self,
        content: &str,
        query_context: &str,
        bias: f64,
    ) -> (String, bool, String) {
        let (result, was_modified, info, _dropped) =
            self.smart_crush_content_collecting(content, query_context, bias);
        (result, was_modified, info)
    }

    /// Typed sibling of [`smart_crush_content`](Self::smart_crush_content)
    /// (§4.2 R3/R4): identical first three tuple elements — byte-identical
    /// rendering — plus every [`DroppedRef`] the walk produced (row-drops
    /// AND opaque substitutions, in emission order) so the FFI can hand
    /// recovery to Python typed instead of via the text scrape.
    pub fn smart_crush_content_typed(
        &self,
        content: &str,
        query_context: &str,
        bias: f64,
    ) -> (String, bool, String, Vec<DroppedRef>) {
        self.smart_crush_content_collecting(content, query_context, bias)
    }

    /// Collecting variant of [`smart_crush_content`](Self::smart_crush_content):
    /// identical rendering, but also returns every recovery ref the
    /// recursive walk produced as a `Vec<DroppedRef>` (row-drops with
    /// their optional per-blob row-index data, plus opaque
    /// substitutions). Drives `crush()`'s typed-field recovery. The
    /// render path is unchanged — the sink is side-output — so `result`
    /// bytes match `smart_crush_content` exactly.
    fn smart_crush_content_collecting(
        &self,
        content: &str,
        query_context: &str,
        bias: f64,
    ) -> (String, bool, String, Vec<DroppedRef>) {
        // COR-44: decline magic-key payloads before calling from_str so
        // serde_json's arbitrary_precision / raw_value promotions never fire.
        // Passthrough identical to the non-JSON branch: original bytes,
        // was_modified=false, no info, no dropped refs.
        if has_serde_private_marker(content) {
            return (content.to_string(), false, String::new(), Vec::new());
        }
        // Parse — non-JSON content passes through unchanged.
        let Ok(parsed) = serde_json::from_str::<Value>(content) else {
            return (content.to_string(), false, String::new(), Vec::new());
        };

        let mut dropped: Vec<DroppedRef> = Vec::new();
        let (crushed, info) =
            self.process_value_collecting(&parsed, 0, query_context, bias, &mut dropped);

        // Re-serialize with Python `safe_json_dumps` formatting:
        // compact `(",", ":")` separators + `ensure_ascii=False`,
        // preserving object-key insertion order. Matches the Python
        // SmartCrusher output bytes exactly.
        let result = crate::transforms::anchor_selector::python_safe_json_dumps(&crushed);
        let was_modified = result != content.trim();
        (result, was_modified, info, dropped)
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
    ///
    /// Public wrapper: allocates a throwaway sink and delegates to
    /// [`process_value_collecting`](Self::process_value_collecting). Keeps
    /// the established `(Value, String)` signature for every existing
    /// caller (tests, `process_string`); `crush()` uses the collecting
    /// variant to surface typed row-drop hashes.
    pub fn process_value(
        &self,
        value: &Value,
        depth: usize,
        query_context: &str,
        bias: f64,
    ) -> (Value, String) {
        let mut sink: Vec<DroppedRef> = Vec::new();
        self.process_value_collecting(value, depth, query_context, bias, &mut sink)
    }

    /// Collecting variant of [`process_value`](Self::process_value):
    /// identical processing and identical `(Value, String)` result, but
    /// every row-drop produced at any depth is appended to `dropped` as a
    /// [`DroppedRef`]. The sink is pure side-output — it never influences
    /// which sentinels are embedded into the returned `Value`, so the
    /// rendered bytes are byte-identical to `process_value`'s.
    fn process_value_collecting(
        &self,
        value: &Value,
        depth: usize,
        query_context: &str,
        bias: f64,
        dropped: &mut Vec<DroppedRef>,
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
                    // Strict lossless-or-passthrough: the non-dict crushers
                    // (string / number / mixed) are sampling drops with a
                    // `<<ccr:HASH>>` recovery sentinel — lossy-recoverable,
                    // so they never run under `lossless_only`. Their match
                    // guards below fail in that mode and the array falls
                    // through to plain recursive descent (nested content is
                    // still processed under the same strict rules). The
                    // DictArray arm stays unguarded: `crush_array` itself
                    // routes lossless-or-passthrough in this mode.
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
                                // The compacted render covers TWO cases: a
                                // PURE lossless win (nothing dropped —
                                // `dropped_refs` carries only whatever
                                // opaque substitutions the render bakes in)
                                // AND a LOSSY survivor-compacted drop
                                // (`smart_sample+compact:table` — rows
                                // dropped, the `<<ccr:HASH ...>>` sentinel
                                // baked into `rendered` as its last line,
                                // the row-drop ref in `dropped_refs`). Both
                                // are genuine reductions whose recovery the
                                // Python scrape would otherwise own —
                                // surface them typed, exactly like the
                                // JSON-array arm below.
                                dropped.extend(result.dropped_refs);
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
                            // Surface the SAME hash + row-index data the
                            // sentinel advertises, typed for direct
                            // mirroring. `dropped_refs` carries the
                            // row-drop ref exactly when rows were dropped
                            // (`persist_dropped` returned `Some`), so this
                            // matches the sentinel condition above.
                            dropped.extend(result.dropped_refs);
                            return (Value::Array(items), info_parts.join(","));
                        }
                        ArrayType::StringArray if !self.config.lossless_only => {
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
                            if let Some(sentinel) =
                                self.ccr_dropped_sentinel_collecting(arr, &crushed_values, dropped)
                            {
                                crushed_values.push(sentinel);
                            }
                            return (Value::Array(crushed_values), info_parts.join(","));
                        }
                        ArrayType::NumberArray if !self.config.lossless_only => {
                            let (crushed, strategy) = crush_number_array(arr, &self.config, bias);
                            info_parts.push(format!("{}({}->{})", strategy, n, crushed.len()));
                            let mut crushed = crushed;
                            // 1A (non-dict path): same guarantee as the
                            // string branch — persist + sentinel on drop.
                            if let Some(sentinel) =
                                self.ccr_dropped_sentinel_collecting(arr, &crushed, dropped)
                            {
                                crushed.push(sentinel);
                            }
                            return (Value::Array(crushed), info_parts.join(","));
                        }
                        ArrayType::MixedArray if !self.config.lossless_only => {
                            // Collecting variant: a dict subgroup's
                            // substituted lossless table can bake in
                            // opaque-cell markers — surface those typed
                            // through the same sink (§4.2 R2).
                            let (crushed, strategy) = self.crush_mixed_array_collecting(
                                arr,
                                query_context,
                                bias,
                                dropped,
                            );
                            info_parts.push(format!("{}({}->{})", strategy, n, crushed.len()));
                            let mut crushed = crushed;
                            // 1A (non-dict path): the mixed crusher drops
                            // str/number subgroup items (and its own
                            // dropped_summary was previously discarded).
                            // Persist the full original + sentinel here so
                            // every dropped item across all subgroups is
                            // recoverable.
                            if let Some(sentinel) =
                                self.ccr_dropped_sentinel_collecting(arr, &crushed, dropped)
                            {
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
                    let (p_item, p_info) = self.process_value_collecting(
                        item,
                        depth + 1,
                        query_context,
                        bias,
                        dropped,
                    );
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
                    let (p_val, p_info) =
                        self.process_value_collecting(v, depth + 1, query_context, bias, dropped);
                    processed.insert(k.clone(), p_val);
                    if !p_info.is_empty() {
                        info_parts.push(p_info);
                    }
                }

                // Second pass: if the object itself has many keys,
                // compress at the key level. Key-crush DROPS keys with no
                // recovery pointer at all, so it is doubly out under strict
                // lossless-or-passthrough.
                if processed.len() >= self.config.min_items_to_analyze && !self.config.lossless_only
                {
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
            // opaque blobs (with store-write so retrieval works). The
            // collecting variant threads the sink so BOTH a row-drop
            // INSIDE a stringified-JSON sub-array AND the opaque-blob
            // substitution itself surface typed (§4.2 R2 — this
            // deliberately overturns the earlier scrape-by-design
            // decision, per the owner mandate).
            Value::String(s) => {
                self.process_string_collecting(s, depth, query_context, bias, dropped)
            }
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
    ///
    /// Row-drops produced while recursing into a stringified-JSON
    /// container are appended to `dropped`, and so is the opaque blob
    /// substitution itself (case 2) as a typed [`DroppedRef::Opaque`] —
    /// §4.2 R2. (Opaque recovery previously stayed on the Python scrape
    /// "by design"; that decision is deliberately overturned per the
    /// owner mandate — the typed ref carries the same hash/kind the
    /// marker renders, plus the exact byte size.)
    fn process_string_collecting(
        &self,
        s: &str,
        depth: usize,
        query_context: &str,
        bias: f64,
        dropped: &mut Vec<DroppedRef>,
    ) -> (Value, String) {
        // 1. Stringified-JSON: parse, recurse, re-render.
        if let Some(parsed) = try_parse_json_container(s) {
            let (processed, sub_info) =
                self.process_value_collecting(&parsed, depth + 1, query_context, bias, dropped);
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
        // Substitution replaces visible bytes with a `<<ccr:` pointer —
        // recoverable, but a visible information reduction — so it is
        // disabled under strict `lossless_only` (the blob passes through
        // verbatim; no store write happens: nothing was hidden).
        if !self.config.lossless_only {
            let cfg = ClassifyConfig::default();
            if let CellClass::Opaque(kind) = classify_cell(&Value::String(s.to_string()), &cfg) {
                let (marker, dropped_ref) =
                    emit_opaque_ccr_marker(s, &kind, self.ccr_store.as_ref());
                // The substitution always ships from here — surface the
                // typed ref alongside the marker text (§4.2 R2).
                dropped.push(dropped_ref);
                let kind_label = opaque_kind_label(&kind);
                return (Value::String(marker), format!("string_ccr:{kind_label}"));
            }
        }

        // 3. Plain string — passthrough.
        (Value::String(s.to_string()), String::new())
    }

    /// Compress an array of dict items.
    ///
    /// Direct port of `_crush_array` (Python line 2400-2687) with the
    /// retired optional subsystems (learning / feedback / telemetry)
    /// in their disabled behavior and CCR wired live. See module-level
    /// docs for the rationale.
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
        self.crush_array_inner(items, query_context, bias, true)
    }

    /// [`SmartCrusher::crush_array`] with the CCR store writes
    /// switchable (COR-28).
    ///
    /// `persist = false` — used by the mixed-array dict arm — runs the
    /// exact same pipeline and returns a byte-identical result: the
    /// hash, marker text and therefore the `MinTokens` routing decision
    /// are all computed as usual; ONLY the store writes are skipped.
    /// That caller consumes just the kept-items set (plus a PURE
    /// lossless `compacted` render) and surfaces no marker naming the
    /// inner hash — its caller appends a whole-mixed-array sentinel of
    /// its own — so a persisted blob + chunks + index would be orphan
    /// entries nothing can ever retrieve, burning the COR-4-bounded
    /// store capacity.
    ///
    /// Contract for `persist = false` callers: NEVER surface
    /// `dropped_summary` / `ccr_hash` / a sentinel-bearing survivor
    /// `compacted` render from the result — a surfaced pointer to an
    /// unpersisted hash would dangle (and trip the Python mirror's
    /// COR-5 fail-open). The pure lossless `compacted` render
    /// (`dropped_summary` empty) carries no pointer and is safe to ship.
    fn crush_array_inner(
        &self,
        items: &[Value],
        query_context: &str,
        bias: f64,
        persist: bool,
    ) -> CrushArrayResult {
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
        // and small arrays are the COMMON case for tool output. Four
        // gates protect the passthrough default beyond the big-array
        // ratio check:
        // - decoder-verifiable shape only (COR-13, fail-closed): the
        //   lossless claim is "exact reconstruction through the
        //   reference decoder", which today covers flat `Table`s only —
        //   `Buckets` renders and `Nested` cells DECLINE to passthrough
        //   until the decoder covers them;
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
                    if c.is_decoder_verifiable() && !c.contains_opaque_ref() {
                        let input_bytes = estimate_array_bytes(&item_strings);
                        let saved = input_bytes.saturating_sub(rendered.len());
                        let savings_ratio = if input_bytes > 0 {
                            saved as f64 / input_bytes as f64
                        } else {
                            0.0
                        };
                        if clears_small_array_lossless_floor(saved)
                            && savings_ratio >= self.config.lossless_min_savings_ratio
                        {
                            let kind = compaction_kind_str(&c);
                            // The `!contains_opaque_ref` gate above means
                            // this collects nothing today; collecting
                            // anyway keeps the typed carrier correct by
                            // construction if the gate ever changes.
                            let mut dropped_refs: Vec<DroppedRef> = Vec::new();
                            c.collect_opaque_refs(&mut dropped_refs);
                            return CrushArrayResult {
                                items: items.to_vec(), // nothing dropped
                                strategy_info: format!("lossless:{kind}"),
                                ccr_hash: None,
                                dropped_summary: String::new(),
                                compacted: Some(rendered),
                                compaction_kind: Some(kind),
                                row_index_marker: None,
                                dropped_refs,
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
                dropped_refs: Vec::new(),
            };
        }

        // ── Lossless candidate ──
        //
        // Run the compaction stage ONCE if present. The lossless render keeps
        // every row (nothing dropped); it is a valid candidate only when
        // it actually compacted into a decoder-verifiable shape (COR-13:
        // a flat `Table` — `Buckets`/`Nested` renders are unverifiable by
        // the reference decoder and DECLINE, fail-closed) and clears the
        // byte-savings gate — below that gate the rendering is not worth
        // shipping over either the raw array or the lossy view, so it is
        // not a real alternative.
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
                // Strict mode tightens the lossless claim to "reconstructible
                // from the visible output ALONE": an opaque-substituted render
                // hides blob bytes behind a `<<ccr:` pointer (recoverable, but
                // a visible information reduction), so it is NOT a candidate
                // under `lossless_only` — same rule the small-array zone
                // already applies unconditionally.
                let opaque_ok = !(self.config.lossless_only && uses_opaque);
                let candidate = if c.is_decoder_verifiable() && opaque_ok {
                    let input_bytes = estimate_array_bytes(&item_strings);
                    let savings_ratio = if input_bytes > 0 {
                        1.0 - (rendered.len() as f64 / input_bytes as f64)
                    } else {
                        0.0
                    };
                    if savings_ratio >= self.config.lossless_min_savings_ratio {
                        let kind = compaction_kind_str(&c);
                        // This render CAN carry opaque substitutions
                        // (decoder-verifiability excludes only Nested
                        // cells) — collect them typed (§4.2 R2). The refs
                        // ride the candidate: they ship iff it ships, and
                        // a discarded candidate's refs drop with it —
                        // exactly like its pending store writes.
                        let mut dropped_refs: Vec<DroppedRef> = Vec::new();
                        c.collect_opaque_refs(&mut dropped_refs);
                        Some(CrushArrayResult {
                            items: items.to_vec(), // nothing dropped
                            strategy_info: format!("lossless:{kind}"),
                            ccr_hash: None,
                            dropped_summary: String::new(),
                            compacted: Some(rendered),
                            compaction_kind: Some(kind),
                            row_index_marker: None,
                            dropped_refs,
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

        // ── Strict lossless-or-passthrough (`lossless_only`) ──
        //
        // The lossy-recoverable candidate is NEVER BUILT in this mode: no
        // rows are dropped, no `<<ccr:HASH>>` sentinel is minted, and no
        // CCR store write happens (`crush_array_lossy` is not invoked, so
        // there are no deferred writes to leak either). Ship the proven-
        // lossless render when it cleared its gates; otherwise pass every
        // row through untouched.
        if self.config.lossless_only {
            return match lossless_candidate {
                Some(lossless) => lossless,
                None => CrushArrayResult {
                    items: items.to_vec(),
                    strategy_info: "skip:lossless_only".to_string(),
                    ccr_hash: None,
                    dropped_summary: String::new(),
                    compacted: None,
                    compaction_kind: None,
                    row_index_marker: None,
                    dropped_refs: Vec::new(),
                },
            };
        }

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

        // P0-4: build the lossy candidate with its CCR store writes
        // DEFERRED (collect-only). Committing at build time orphaned the
        // blob + chunks + index in the store whenever the routing below
        // chose the lossless render — wasted COR-4-bounded capacity and
        // misleading store stats under hashes no surfaced marker names.
        // The deferred writes are committed exactly when the lossy render
        // ships (the two ship-lossy arms below), so persistence for
        // SHIPPED lossy output remains UNCONDITIONAL — the recovery
        // invariant is timing-shifted, never weakened. Hash/marker
        // computation is mode-independent, so routing cannot shift.
        // `persist = false` (COR-28, mixed dict arm) still means NO
        // writes ever: Skip mode.
        let persist_mode = if persist {
            PersistMode::Collect
        } else {
            PersistMode::Skip
        };
        let lossy = self.crush_array_lossy(
            items,
            query_context,
            &item_strings,
            adaptive_k,
            !lossless_uses_opaque,
            persist_mode,
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
            (
                Some(lossless),
                LossyOutcome::Crushed {
                    result: lossy,
                    pending_ccr_writes,
                },
            ) => match self.config.routing_policy {
                // Lossless ships → the lossy candidate is discarded and
                // its deferred writes drop with it (no orphans, P0-4).
                RoutingPolicy::LosslessFirst => lossless,
                RoutingPolicy::MinTokens => {
                    let lossless_tokens = self.render_token_count(&lossless);
                    let lossy_tokens = self.render_token_count(&lossy);
                    // Lossy wins only when STRICTLY fewer tokens; ties (and
                    // lossless-fewer) → lossless: more rows visible at no
                    // extra token cost.
                    if lossy_tokens < lossless_tokens {
                        // Lossy SHIPS → commit its recovery entries now
                        // (unconditional persist for shipped output).
                        self.commit_ccr_writes(pending_ccr_writes);
                        lossy
                    } else {
                        // Lossless ships → discarded candidate's deferred
                        // writes are dropped (no orphans, P0-4).
                        lossless
                    }
                }
            },
            // Lossless render valid but the array isn't droppable (Skip):
            // ship lossless — it shows every row losslessly. (A non-
            // droppable array should never drop, and lossless never drops.)
            (Some(lossless), LossyOutcome::Skip(_)) => lossless,
            // Only the lossy DROP render is valid → ship it. Its recovery
            // entries are committed on the way out (same unconditional
            // guarantee as before the deferral; only the timing moved).
            (
                None,
                LossyOutcome::Crushed {
                    result: lossy,
                    pending_ccr_writes,
                },
            ) => {
                self.commit_ccr_writes(pending_ccr_writes);
                lossy
            }
            // No lossless render and the array isn't droppable → the
            // `skip:<reason>` passthrough (preserves pre-routing behavior).
            (None, LossyOutcome::Skip(passthrough)) => passthrough,
        }
    }

    /// Replay deferred CCR writes captured under [`PersistMode::Collect`],
    /// in capture order (granular chunks → row index → whole-blob — the
    /// same eviction-friendly order `persist_dropped` writes directly).
    /// Called by the routing layer EXACTLY when the lossy render ships;
    /// a discarded candidate's writes are simply dropped, so the store
    /// never carries entries no surfaced marker names (P0-4).
    fn commit_ccr_writes(&self, pending: Vec<CcrWrite>) {
        if pending.is_empty() {
            return;
        }
        if let Some(store) = &self.ccr_store {
            for write in &pending {
                store.put(&write.key, &write.payload);
            }
        }
    }

    /// Build the lossy-recoverable render of `items` (row-drop + CCR
    /// sentinel). Returns [`LossyOutcome::Skip`] (carrying the
    /// `skip:<reason>` passthrough) when the array is not safe to crush
    /// (the analyzer's `Skip` gate) — there is no DROP render in that
    /// case. Otherwise returns [`LossyOutcome::Crushed`] with the
    /// row-dropped render plus its deferred store writes: a chosen lossy
    /// render is ALWAYS recoverable — the routing layer commits the
    /// writes iff this render ships (P0-4).
    ///
    /// Factored out of `crush_array` so the routing layer can size this
    /// candidate against the lossless one before deciding which to ship.
    /// The rendered bytes are byte-identical to the pre-routing lossy
    /// path — only the place it is *called from* (and, since P0-4, the
    /// store-write timing) changed.
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
        // How the drop's store writes are handled (hash + markers are
        // computed identically in every mode — routing stays
        // byte-identical). `Collect` defers them into the returned
        // outcome for commit-on-ship (P0-4); `Skip` (COR-28, mixed dict
        // arm) never writes. See `crush_array_inner` for the contract.
        persist_mode: PersistMode,
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
                dropped_refs: Vec::new(),
            });
        }

        let plan = self.planner().create_plan(
            &analysis,
            items,
            query_context,
            None, // preserve_fields — no production caller supplies these
            Some(effective_max_items),
            Some(item_strings),
        );
        let mut result = self.execute_plan(&plan, items);
        // Computed BEFORE annotation (which only adds keys to kept rows,
        // never changes the row count) so it can gate the stamping below.
        let dropped_count = items.len().saturating_sub(result.len());

        // Field-aware multiplicity (DESIGN.md Imp2). When rows that are
        // identical-except-identity collapse under the stable-projection
        // hash, the kept representative carries a `_dup_count` so the
        // model knows N rows existed. This fires ONLY when the plan
        // actually dropped rows (COR-33: on a no-drop plan every original
        // row is already visible, so stamping is token inflation with
        // zero compression) AND real duplication is present (group size
        // > 1); for all-distinct data (e.g. unique-subject git logs,
        // search results) every group is size 1, no key is added, and
        // the output bytes are unchanged.
        if dropped_count > 0 {
            let exclude = compute_exclude_set(&analysis.field_stats, items);
            if !exclude.is_empty() {
                annotate_dup_counts(&mut result, items, &exclude);
            }
        }

        // CCR persistence + marker emission. **The store write is the
        // cornerstone of CCR's no-data-loss guarantee:** whenever rows
        // are dropped we hash the full original and stash it in the
        // configured store so a dropped needle is *always* recoverable
        // — never silently lost. The plan's `keep_indices` name the
        // surviving rows, so their complement is EXACTLY the dropped
        // set — threaded through so only dropped rows get granular
        // chunks (COR-4: kept rows must never flood the bounded store).
        let dropped_indices = dropped_indices_from_kept(&plan.keep_indices, items.len());
        let (ccr_hash, dropped_summary, row_index_marker, row_drop_refs, pending_ccr_writes) =
            match self.persist_dropped(items, dropped_count, &dropped_indices, persist_mode) {
                Some(persisted) => {
                    let row_index_marker = persisted.row_index_marker();
                    // The typed carrier for this drop — same hash + chunk
                    // count the sentinel advertises (§4.2).
                    let refs = vec![persisted.dropped_ref()];
                    (
                        Some(persisted.hash),
                        persisted.marker,
                        row_index_marker,
                        refs,
                        persisted.pending_writes,
                    )
                }
                None => (None, String::new(), None, Vec::new(), Vec::new()),
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
        // - decoder-verifiable shape only (COR-13, fail-closed): the
        //   survivor render must be provable by the same reference
        //   decoder as the lossless tier — flat `Table` only,
        //   `Buckets`/`Nested` renders decline to the plain JSON form;
        // - no `OpaqueRef` substitution (survivor values must stay
        //   verbatim — same rule as the small-array lossless zone);
        // - absolute saving ≥ `LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES`
        //   vs the exact bytes the JSON form would ship.
        if !dropped_summary.is_empty() {
            if let Some(stage) = &self.compaction {
                let (c, rendered) = stage.run(&result);
                if c.is_decoder_verifiable() && !c.contains_opaque_ref() {
                    let sentinel = ccr_sentinel_map(&dropped_summary, row_index_marker.as_deref());
                    let sentinel_line = crate::transforms::anchor_selector::python_safe_json_dumps(
                        &Value::Object(sentinel.clone()),
                    );
                    let mut json_form_items = result.clone();
                    json_form_items.push(Value::Object(sentinel));
                    let json_form = crate::transforms::anchor_selector::python_safe_json_dumps(
                        &Value::Array(json_form_items),
                    );
                    let compact_len = rendered.len() + 1 + sentinel_line.len();
                    if clears_lossy_survivor_floor(json_form.len().saturating_sub(compact_len)) {
                        let kind = compaction_kind_str(&c);
                        let rendered_with_sentinel =
                            format!("{}\n{sentinel_line}", rendered.trim_end_matches('\n'));
                        // Survivor renders are gated opaque-free
                        // (`!contains_opaque_ref` above) so this collects
                        // nothing today — kept for correctness under gate
                        // changes. Render order: opaque cells first, the
                        // sentinel (row-drop) is the render's last line.
                        let mut dropped_refs: Vec<DroppedRef> = Vec::new();
                        c.collect_opaque_refs(&mut dropped_refs);
                        dropped_refs.extend(row_drop_refs);
                        return LossyOutcome::Crushed {
                            result: CrushArrayResult {
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
                                dropped_refs,
                            },
                            pending_ccr_writes,
                        };
                    }
                }
            }
        }

        LossyOutcome::Crushed {
            result: CrushArrayResult {
                items: result,
                strategy_info: analysis.recommended_strategy.as_str().to_string(),
                ccr_hash,
                dropped_summary,
                compacted: None,
                compaction_kind: None,
                row_index_marker,
                dropped_refs: row_drop_refs,
            },
            pending_ccr_writes,
        }
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
            let sentinel =
                ccr_sentinel_map(&result.dropped_summary, result.row_index_marker.as_deref());
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
    ///
    /// `dropped_count` drives the whole-blob marker text (the byte-stable
    /// `{n}_rows_offloaded` arithmetic every caller already computed as
    /// `original - kept`); `dropped_indices` names exactly WHICH original
    /// rows left the visible output and therefore get granular per-row
    /// chunks. The two agree on every path; they can diverge only when a
    /// crusher synthesizes non-original output items (mixed-path
    /// summaries), in which case the index simply covers every original
    /// row not visible verbatim — a safe superset for recovery.
    ///
    /// `mode` selects what happens to the store writes ([`PersistMode`]):
    /// `Commit` writes through immediately (always-ships paths);
    /// `Collect` (P0-4, the dict arbitration path) defers them into
    /// [`DroppedPersist::pending_writes`] for commit-on-ship; `Skip`
    /// (COR-28, via `crush_array_inner`) performs NO store write — the
    /// caller surfaces no marker naming this hash, so any write would be
    /// an orphan entry burning COR-4-bounded capacity. In EVERY mode the
    /// hash, marker and granular-index decision are computed EXACTLY the
    /// same — the returned markers are byte-identical, so token routing
    /// built on them cannot shift. The COR-4 store-flood gate still
    /// decides `row_index_marker` the same way in all modes.
    fn persist_dropped(
        &self,
        original_items: &[Value],
        dropped_count: usize,
        dropped_indices: &[usize],
        mode: PersistMode,
    ) -> Option<DroppedPersist> {
        if dropped_count == 0 {
            return None;
        }

        // Deferred-write sink (populated only under `Collect`). Routing a
        // write: through to the store (Commit), into the sink (Collect),
        // or nowhere (Skip). Total over `PersistMode` so a new mode is a
        // compile error here, not a silent write-nothing.
        let mut pending_writes: Vec<CcrWrite> = Vec::new();
        fn route_write(
            mode: PersistMode,
            store: &Arc<dyn CcrStore>,
            pending: &mut Vec<CcrWrite>,
            key: &str,
            payload: String,
        ) {
            match mode {
                PersistMode::Commit => store.put(key, &payload),
                PersistMode::Collect => pending.push(CcrWrite {
                    key: key.to_string(),
                    payload,
                }),
                PersistMode::Skip => {}
            }
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
        // → −10.3% worst-case). Fix: ALSO stash each DROPPED row under
        // its own canonical 1-element hash so retrieving ONE row fetches
        // exactly one row, not the whole blob.
        //
        // Only the DROPPED rows are chunked (COR-4). Kept rows are
        // already visible in the output — a per-row chunk for them buys
        // nothing, and writing one store entry per ORIGINAL row let a
        // single large array (or two arrays in one document) flood the
        // bounded FIFO store and evict whole-blob entries the SAME
        // document's markers still referenced — a dangling `<<ccr:HASH>>`
        // is silent loss. Each per-row entry is keyed by `hash_canonical`
        // over the 1-element canonical array, so `ccr_get(row_hash)`
        // returns exactly `[row]`.
        //
        // ── Store-flood gate (COR-4, oversized drops) ──
        // Even dropped-only chunking can flood the store: a drop bigger
        // than the store's capacity would self-evict its own earliest
        // chunks. When the chunk count exceeds `capacity /
        // GRANULAR_CHUNK_CAPACITY_DIVISOR`, skip granular chunking
        // entirely and persist the whole-blob only (`row_index_marker:
        // None` — an already-supported sentinel shape). Proportional
        // retrieval degrades to whole-blob retrieval for oversized
        // arrays; recovery never degrades.
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
        let mut row_index_chunks: Option<usize> = None;
        if let Some(store) = &self.ccr_store {
            let granular_budget = store.capacity() / GRANULAR_CHUNK_CAPACITY_DIVISOR;
            if !dropped_indices.is_empty() && dropped_indices.len() <= granular_budget {
                // In-range dropped rows are the chunkable set (out-of-
                // range indices: nothing to chunk). Resolved up front so
                // the marker's chunk count stays identical whether or
                // not the writes below happen (COR-28 persist-skip).
                let chunk_rows: Vec<&Value> = dropped_indices
                    .iter()
                    .filter_map(|&idx| original_items.get(idx))
                    .collect();
                if !matches!(mode, PersistMode::Skip) {
                    let mut row_hashes: Vec<String> = Vec::with_capacity(chunk_rows.len());
                    for item in &chunk_rows {
                        let row_canonical = canonical_array_json(std::slice::from_ref(*item));
                        let row_hash = hash_canonical(&row_canonical);
                        route_write(mode, store, &mut pending_writes, &row_hash, row_canonical);
                        row_hashes.push(row_hash);
                    }
                    // Row index: `{hash}#rows` → ["rowhash0", "rowhash1", ...].
                    // Stored as a JSON array of strings so the retrieval layer can
                    // parse it and address each dropped row independently. The
                    // marker advertises `chunk_rows.len()` — EXACTLY the number
                    // of chunks the index holds (COR-20: it previously claimed
                    // `dropped_count` chunks over an every-original-row index).
                    // Key construction shared with the typed carrier's
                    // `DroppedRef::row_index_key` accessor (one owner).
                    let index_key = row_index_key(&hash);
                    let index_payload = serde_json::to_string(&row_hashes).unwrap_or_default();
                    route_write(mode, store, &mut pending_writes, &index_key, index_payload);
                }
                row_index_chunks = Some(chunk_rows.len());
            }
        }

        // ── Whole-blob persist (1A) — written LAST ──
        // The byte-stable recovery key the invariant + parity depend on.
        // Unconditional on every persisting path (immediate under Commit,
        // deferred-to-ship under Collect); skipped ONLY for the
        // surfaced-nowhere mixed-arm inner call (COR-28, Skip).
        if let Some(store) = &self.ccr_store {
            route_write(mode, store, &mut pending_writes, &hash, canonical);
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
        // *retrieval-tool injection* (advertising `furl_retrieve`
        // into the request), which is owned by the router layer
        // (`CCRConfig.inject_tool` / `inject_retrieval_marker`), NOT by
        // the crusher. The crusher's job is to never drop a distinct
        // item without leaving a pointer to it; that pointer is now
        // emitted unconditionally on every drop.
        let marker = marker_for_rows_offloaded(&hash, dropped_count);

        Some(DroppedPersist {
            hash,
            marker,
            row_index_chunks,
            pending_writes,
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
    ///
    /// Pushes the typed [`DroppedRef::RowDrop`] onto `dropped` so the
    /// non-dict (string/number/mixed) row-drop is surfaced for direct
    /// mirroring — parity with the dict path. Side effect only: the
    /// returned `Value` is byte-identical to the pre-collection behavior.
    ///
    /// `kept_items` is the crushed output BEFORE the sentinel is pushed.
    /// The whole-blob marker keeps the callers' historical arithmetic
    /// (`original - kept`); the dropped-row set for granular chunking is
    /// derived by multiset diff, since the non-dict crushers return kept
    /// VALUES, not kept indices (COR-4: kept rows are never chunked).
    fn ccr_dropped_sentinel_collecting(
        &self,
        original_items: &[Value],
        kept_items: &[Value],
        dropped: &mut Vec<DroppedRef>,
    ) -> Option<Value> {
        let dropped_count = original_items.len().saturating_sub(kept_items.len());
        let dropped_indices = dropped_indices_by_multiset_diff(original_items, kept_items);
        // Commit mode: the non-dict paths never arbitrate against a
        // lossless candidate — this render always ships, so the write
        // happens now (no deferral needed).
        let persisted = self.persist_dropped(
            original_items,
            dropped_count,
            &dropped_indices,
            PersistMode::Commit,
        )?;
        dropped.push(persisted.dropped_ref());
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
    ///
    /// Public wrapper: allocates a throwaway sink and delegates to
    /// [`crush_mixed_array_collecting`](Self::crush_mixed_array_collecting)
    /// — same pattern as `process_value` / `smart_crush_content`.
    pub fn crush_mixed_array(
        &self,
        items: &[Value],
        query_context: &str,
        bias: f64,
    ) -> (Vec<Value>, String) {
        let mut sink: Vec<DroppedRef> = Vec::new();
        self.crush_mixed_array_collecting(items, query_context, bias, &mut sink)
    }

    /// Collecting variant of [`crush_mixed_array`](Self::crush_mixed_array):
    /// identical output, but the typed refs of any SHIPPED substituted
    /// render (the dict subgroup's pure-lossless table, COR-28b — which
    /// can bake in opaque-cell markers) are appended to `dropped`
    /// (§4.2 R2). Row-drop refs from the inner dict pipeline are NEVER
    /// surfaced here: that call runs `persist = false` (PersistMode::Skip
    /// — nothing persisted, COR-28), and only its pure-lossless render
    /// (no drops, no sentinel) is ever substituted; opaque-cell store
    /// writes happen eagerly inside `compact()` regardless of the
    /// persist mode, so the opaque refs it carries are backed.
    fn crush_mixed_array_collecting(
        &self,
        items: &[Value],
        query_context: &str,
        bias: f64,
        dropped: &mut Vec<DroppedRef>,
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
        // Kept positions whose ORIGINAL item is replaced by a rendered
        // string (the dict subgroup's lossless table — COR-28b).
        let mut substitutions: std::collections::BTreeMap<usize, Value> =
            std::collections::BTreeMap::new();
        let mut strategy_parts: Vec<String> = Vec::new();

        for (type_key, indices, values) in groups.into_iter() {
            // Small groups: keep all items.
            if values.len() < self.config.min_items_to_analyze {
                keep_indices.extend(&indices);
                continue;
            }

            match type_key {
                "dict" => {
                    // COR-28: run the shared dict pipeline WITHOUT store
                    // persistence. This arm consumes only the kept-items
                    // set — the caller appends its own whole-mixed-array
                    // sentinel (the recovery pointer rides the OUTER
                    // hash), so an inner persist would write blob +
                    // chunks + index under a hash no surfaced marker
                    // ever names: orphan entries burning COR-4-bounded
                    // capacity.
                    let CrushArrayResult {
                        items: crushed,
                        strategy_info,
                        compacted,
                        dropped_summary,
                        dropped_refs,
                        ..
                    } = self.crush_array_inner(&values, query_context, bias, false);
                    // COR-28b (EFF-9): ship a PURE lossless win (nothing
                    // dropped, no sentinel) as ONE rendered table string
                    // at the subgroup's first position — it was
                    // previously discarded, shipping the subgroup
                    // uncompressed while reporting `dict:N->N`. Survivor-
                    // compacted renders (`dropped_summary` non-empty) are
                    // NOT substituted: their baked-in sentinel names the
                    // unpersisted inner hash — a dangling pointer — so
                    // they keep the kept-items path below.
                    if dropped_summary.is_empty() {
                        if let (Some(rendered), Some(&first_idx)) = (compacted, indices.first()) {
                            keep_indices.insert(first_idx);
                            substitutions.insert(first_idx, Value::String(rendered));
                            // The substituted render SHIPS — surface its
                            // typed refs (opaque cells only: a pure
                            // lossless render has no row-drop, and the
                            // opaque originals were written eagerly by
                            // `compact()` regardless of persist mode).
                            dropped.extend(dropped_refs);
                            strategy_parts.push(format!(
                                "dict:{}->{}",
                                values.len(),
                                strategy_info
                            ));
                            continue;
                        }
                    }
                    // Kept-items path: the inner result's renders (and
                    // any refs they carried) are DISCARDED — no inner
                    // marker ships, so no ref may surface (COR-28).
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

        // Reassemble in original order; a substituted position ships its
        // rendered string (COR-28b) instead of the original item.
        let result: Vec<Value> = keep_indices
            .iter()
            .map(|&i| match substitutions.remove(&i) {
                Some(rendered) => rendered,
                None => items[i].clone(),
            })
            .collect();
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

/// Stamp `_dup_count` on the kept REPRESENTATIVE of each
/// stable-projection-hash family (over ALL original `items`, with
/// `exclude` identity columns filtered) that has more than one member
/// (DESIGN.md Imp2).
///
/// `_dup_count = N` records that N original rows shared this row's
/// value-bearing content (differing only in excluded identity columns).
/// Only the FIRST kept member of a family — its representative — is
/// stamped (COR-33): when several members of the same family stay
/// visible, stamping each of them made N rows each claim N duplicates,
/// reading like N² originals — token inflation, not information. Rows
/// in a singleton family are left untouched, so all-distinct input is
/// byte-for-byte unchanged. The representative keeps its own real
/// varying values; the dropped duplicates remain CCR-recoverable from
/// the full-original store entry.
fn annotate_dup_counts(
    kept: &mut [Value],
    all_items: &[Value],
    exclude: &std::collections::BTreeSet<String>,
) {
    use crate::transforms::anchor_selector::stable_item_hash;

    // Family sizes over the WHOLE original array.
    let mut family_size: std::collections::HashMap<String, usize> =
        std::collections::HashMap::new();
    for item in all_items {
        if item.is_object() || item.is_array() {
            *family_size
                .entry(stable_item_hash(item, exclude))
                .or_insert(0) += 1;
        }
    }

    // Families whose representative has already been stamped — later
    // kept members of the same family stay untouched (COR-33).
    let mut stamped: std::collections::HashSet<String> = std::collections::HashSet::new();
    for row in kept.iter_mut() {
        if !row.is_object() {
            continue;
        }
        let h = stable_item_hash(row, exclude);
        let count = family_size.get(&h).copied().unwrap_or(1);
        if count > 1 && stamped.insert(h) {
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

/// Whether an absolute byte saving clears the small-array lossless floor
/// (`>= SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES`, inclusive). Extracted from the
/// inline gate so the boundary is unit-testable directly (255/256/257) without
/// crafting a renderer-byte-exact fixture through the whole crush pipeline — a
/// directional fixture (well-above / well-below) would survive a `>=`→`>`
/// operator mutation; the boundary test kills it.
#[inline]
fn clears_small_array_lossless_floor(saved: usize) -> bool {
    saved >= SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES
}

/// Divisor applied to `adaptive_k` for the lossy keep budget when a CCR
/// store guarantees recovery of every dropped row. 2 (halving) keeps a
/// meaningful visible sample while the critical signals (errors /
/// outliers / anomalies / query pins) remain exempt from the budget.
const CCR_BACKED_KEEP_DIVISOR: usize = 2;

/// Floor for the CCR-backed keep budget. `min_items_to_analyze` (5) is
/// the engine's own notion of "too small to even analyze" — the visible
/// sample never shrinks below it.
const CCR_BACKED_KEEP_FLOOR: usize = 5;

/// Store-flood gate for granular per-row chunking (COR-4). One drop
/// writes `chunks + index + whole-blob` entries into a bounded FIFO
/// store; capping the chunk count at `capacity / 4` leaves room for
/// several drops in one document (each ≤ ~capacity/4 + 2 entries)
/// before anything a live marker references can be evicted. Drops
/// bigger than the budget persist the whole-blob only.
const GRANULAR_CHUNK_CAPACITY_DIVISOR: usize = 4;

/// Minimum ABSOLUTE byte saving required before the lossy path ships
/// its survivors as a CSV-schema rendering instead of a JSON array.
/// Same churn-protection rationale as the small-array gate: re-encoding
/// a handful of rows to save a few bytes is noise, not compression.
const LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES: usize = 64;

/// Whether an absolute byte saving clears the lossy-survivor render floor
/// (`>= LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES`, inclusive). Extracted for the
/// same reason as the small-array floor helper: the inclusive boundary
/// (63/64/65) is unit-testable here without a pipeline-byte-exact fixture.
#[inline]
fn clears_lossy_survivor_floor(saved: usize) -> bool {
    saved >= LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES
}

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

/// Complement of `keep_indices` over `0..len`: the original-array
/// indices of the rows a plan DROPS, ascending. Out-of-range keep
/// indices are ignored (mirroring `execute_plan`'s bounds filter) and
/// duplicates collapse via the mask, so the result length always equals
/// `len - |kept ∩ 0..len|`. Feeds `persist_dropped`'s dropped-rows-only
/// granular chunking on the dict path (COR-4).
fn dropped_indices_from_kept(keep_indices: &[usize], len: usize) -> Vec<usize> {
    let mut kept = vec![false; len];
    for &idx in keep_indices {
        if idx < len {
            kept[idx] = true;
        }
    }
    kept.iter()
        .enumerate()
        .filter_map(|(i, &k)| (!k).then_some(i))
        .collect()
}

/// Original-array indices whose rows do NOT appear in `kept`, ascending
/// (multiset semantics: each kept occurrence consumes ONE matching
/// original). Matching is on the same per-row canonical bytes the
/// granular chunks are keyed by (`canonical_array_json` of the
/// 1-element slice), so "kept" means byte-identical to a visible output
/// row. A synthesized kept item (e.g. a mixed-path summary) matches no
/// original and consumes nothing — the result can only ever
/// OVER-approximate the dropped set, never miss a genuinely dropped
/// row. Feeds `persist_dropped` on the string/number/mixed paths, whose
/// crushers return kept VALUES rather than kept indices (COR-4).
fn dropped_indices_by_multiset_diff(original: &[Value], kept: &[Value]) -> Vec<usize> {
    use std::collections::HashMap;
    let mut kept_counts: HashMap<String, usize> = HashMap::with_capacity(kept.len());
    for item in kept {
        *kept_counts
            .entry(canonical_array_json(std::slice::from_ref(item)))
            .or_insert(0) += 1;
    }
    original
        .iter()
        .enumerate()
        .filter_map(|(i, item)| {
            let key = canonical_array_json(std::slice::from_ref(item));
            match kept_counts.get_mut(&key) {
                Some(count) if *count > 0 => {
                    *count -= 1;
                    None
                }
                _ => Some(i),
            }
        })
        .collect()
}

/// 12-char SHA-256 hex prefix of an already-serialized canonical JSON
/// string. Caller is responsible for producing the canonical form via
/// [`canonical_array_json`] (or another byte-equal serializer) — the
/// hash is over the bytes, so a stable serializer is the contract.
/// Algorithm consolidated in `ccr::persist` (ARCH-5); this domain alias
/// stays so the row-drop call sites and the parity pins keep their
/// vocabulary.
fn hash_canonical(canonical: &str) -> String {
    crate::ccr::persist::sha6_hex12(canonical.as_bytes())
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

    // The two tests above pin the SMALL_ARRAY_LOSSLESS floor DIRECTIONALLY
    // (well-above ships lossless, well-below stays passthrough). The two below
    // pin the EXACT inclusive boundary of each absolute-saved gate. Because
    // `saved = estimate_array_bytes - rendered.len()` is a coupled function of
    // the compaction renderer's output, a byte-exact pipeline fixture would be
    // brittle; testing the extracted predicate isolates the `>=` boundary
    // cleanly and kills a `>=`→`>` operator mutation a directional fixture
    // would survive.
    #[test]
    fn small_array_lossless_floor_boundary_is_inclusive_256() {
        assert!(!clears_small_array_lossless_floor(255));
        assert!(clears_small_array_lossless_floor(256));
        assert!(clears_small_array_lossless_floor(257));
    }

    #[test]
    fn lossy_survivor_floor_boundary_is_inclusive_64() {
        assert!(!clears_lossy_survivor_floor(63));
        assert!(clears_lossy_survivor_floor(64));
        assert!(clears_lossy_survivor_floor(65));
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
    fn small_array_with_nested_cells_stays_passthrough() {
        // COR-13 fail-closed: an array-of-objects cell becomes
        // `CellValue::Nested`, whose CSV-quoted IR-JSON rendering the
        // reference decoder cannot invert — the small-array lossless
        // zone must DECLINE it (verbatim passthrough), never ship it as
        // "lossless"-verified. The long constant columns make the
        // render clear both byte-savings gates, so only the
        // decoder-coverage gate keeps this shape out.
        let c = crusher();
        let items: Vec<Value> = (0..6)
            .map(|i| {
                json!({
                    "id": i,
                    "service": "auth-service-primary-eu-central-1.internal.example.com",
                    "status": "ok-and-healthy-and-ready",
                    "region": "eu-central-1-availability-zone-a",
                    "deployment": "blue-green-rollout-2026-06-15T00:00:00Z-primary",
                    "children": [{"k": i}, {"k": i + 1}],
                })
            })
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(
            result.strategy_info, "none:adaptive_at_limit",
            "a Nested-cell table must not ship under the lossless claim"
        );
        assert!(result.compacted.is_none());
        assert_eq!(result.items.len(), 6, "nothing may be dropped");
    }

    #[test]
    fn heterogeneous_buckets_array_never_ships_lossless() {
        // COR-13 fail-closed: a heterogeneous array with a clean string
        // discriminator compacts to `Compaction::Buckets`, whose
        // `__buckets:` grammar the reference decoder cannot decode —
        // the lossless accept gates must DECLINE it. The corpus routes
        // through the big-array candidate gate (60 rows) so the lossy
        // path stays available; whatever wins, no `lossless:` strategy
        // and no `__buckets:` render may ship.
        let c = crusher();
        let items: Vec<Value> = (0..60_i64)
            .map(|i| {
                if i % 2 == 0 {
                    json!({
                        "kind": "user",
                        "name": format!("user-{i:03}"),
                        "email": format!("user{i}@example.com"),
                        "role": if i % 4 == 0 { "admin" } else { "member" },
                    })
                } else {
                    json!({
                        "kind": "metric",
                        "ts": 1_700_000_000_i64 + i,
                        "value": i * 3,
                        "unit": "ms",
                    })
                }
            })
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        assert!(
            !result.strategy_info.starts_with("lossless:"),
            "Buckets must be declined from the lossless tier (COR-13); got: {}",
            result.strategy_info
        );
        if let Some(compacted) = &result.compacted {
            assert!(
                !compacted.starts_with("__buckets:"),
                "an unverifiable __buckets: render shipped: {compacted}"
            );
        }
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

    // ---------- COR-33: `_dup_count` stamping ----------

    #[test]
    fn annotate_dup_counts_stamps_only_the_family_representative() {
        // COR-33 (representative half): when SEVERAL members of the same
        // stable-projection family stay visible, only the FIRST kept
        // member (the representative) may carry `_dup_count`. Stamping
        // every visible copy made N rows each claim N duplicates —
        // reading like N² rows — pure token inflation.
        let all: Vec<Value> = (0..4)
            .map(|i| json!({"req_id": format!("{i:040x}"), "msg": "dup"}))
            .collect();
        let mut kept = vec![all[0].clone(), all[1].clone()];
        let exclude: std::collections::BTreeSet<String> =
            std::iter::once("req_id".to_string()).collect();
        annotate_dup_counts(&mut kept, &all, &exclude);
        assert_eq!(
            kept[0].get("_dup_count"),
            Some(&json!(4)),
            "the representative records the family size"
        );
        assert_eq!(
            kept[1].get("_dup_count"),
            None,
            "non-representative visible copies must NOT be stamped (COR-33)"
        );
    }

    #[test]
    fn dup_count_not_stamped_when_plan_drops_nothing() {
        // COR-33 (no-drop half): `_dup_count` exists to record rows the
        // plan COLLAPSED. When the plan dropped nothing every original
        // row is already visible, so stamping is token inflation with
        // zero compression. Fixture: 30 all-error rows (the error
        // constraint pins every row through the over-budget path) in 3
        // identical-except-identity families, with whole-item dedup
        // disabled so the duplicates stay visible.
        let config = SmartCrusherConfig {
            dedup_identical_items: false,
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusher::without_compaction(config);
        let msgs = ["disk full", "auth expired", "cache miss"];
        let items: Vec<Value> = (0..30)
            .map(|i| {
                json!({
                    "req_id": format!("{i:040x}"),
                    "status": "error",
                    "msg": msgs[i % 3],
                })
            })
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(
            result.items.len(),
            30,
            "fixture precondition: all-error rows must produce a no-drop plan, got strategy {}",
            result.strategy_info
        );
        assert!(
            result.items.iter().all(|r| r.get("_dup_count").is_none()),
            "a no-drop plan must not stamp `_dup_count` (COR-33); got {:?}",
            result.items
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
        // The 5 strings (small group) all survive. (Counted by value:
        // since COR-28b the dict subgroup may ALSO ship as one rendered
        // table string, so a blanket is_string() count is ambiguous.)
        let str_count = result
            .iter()
            .filter(|v| v.as_str().is_some_and(|s| s.starts_with("string_")))
            .count();
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
    fn mixed_dict_arm_persists_nothing_to_the_store() {
        // COR-28(a): the dict subgroup's inner crush must not write
        // blob + chunks + index into the store — the mixed arm consumes
        // only the kept-items set and surfaces NO marker naming the
        // inner hash (the caller appends its own whole-mixed-array
        // sentinel), so every inner write is an orphan entry burning
        // COR-4-bounded capacity. Store must stay EMPTY.
        use crate::ccr::InMemoryCcrStore;
        use std::sync::Arc;
        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        // No compaction stage: the dict subgroup must take the LOSSY
        // path (the persist-writing one), not a lossless render.
        let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
            .with_default_oss_setup()
            .with_ccr_store(store_dyn)
            .build();
        let mut items: Vec<Value> = (0..25).map(|i| json!({"id": i, "status": "ok"})).collect();
        for i in 0..9 {
            items.push(json!(i));
        }
        let (crushed, strat) = c.crush_mixed_array(&items, "", 1.0);
        assert!(
            crushed.len() < items.len(),
            "fixture precondition: the dict subgroup must actually drop rows, strat={strat}"
        );
        assert_eq!(
            store.len(),
            0,
            "no surfaced marker names the inner dict-subgroup hash — the \
             mixed arm must not persist (COR-28), strat={strat}"
        );
    }

    #[test]
    fn mixed_dict_subgroup_ships_lossless_render_when_it_wins() {
        // COR-28(b) / EFF-9: a PURE lossless win on the dict subgroup
        // (nothing dropped, no sentinel) used to be thrown away — the
        // subgroup shipped uncompressed while `strategy_parts` reported
        // `dict:25->25`. It must ship as ONE rendered table string at
        // the subgroup's first position instead.
        let config = SmartCrusherConfig {
            // Deterministic: lossless wins whenever its gate clears,
            // independent of tokenizer sizing.
            routing_policy: RoutingPolicy::LosslessFirst,
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusher::new(config);
        // Wide, repetitive df-style rows — compacts far past both
        // lossless gates (same shape as the small-array lossless test).
        let mut items: Vec<Value> = (0..25)
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
        for i in 0..4 {
            items.push(json!(format!("trailing_note_{i}")));
        }
        let (crushed, strat) = c.crush_mixed_array(&items, "", 1.0);
        assert!(
            strat.contains("dict:25->lossless:table"),
            "strategy must report the lossless subgroup win, got: {strat}"
        );
        let table = crushed
            .iter()
            .find_map(|v| v.as_str().filter(|s| s.starts_with("[25]{")));
        assert!(
            table.is_some(),
            "dict subgroup must ship as one rendered table string, got: {crushed:?}"
        );
        assert_eq!(
            crushed.iter().filter(|v| v.is_object()).count(),
            0,
            "no raw dict row may remain once the lossless render shipped"
        );
        // The render sits at the subgroup's first original position.
        assert!(
            crushed
                .first()
                .and_then(|v| v.as_str())
                .is_some_and(|s| s.starts_with("[25]{")),
            "the rendered table replaces the subgroup at its first index"
        );
        // The 4 trailing strings (small group) still pass through.
        let trailing = crushed
            .iter()
            .filter(|v| v.as_str().is_some_and(|s| s.starts_with("trailing_note_")))
            .count();
        assert_eq!(trailing, 4);
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

    // ---------- strict lossless-or-passthrough (`lossless_only`) ----------

    /// Build a store-backed crusher with `lossless_only` plus any extra
    /// config the test needs, returning the concrete store handle so
    /// tests can assert it never grows.
    fn lossless_only_crusher(
        cfg: SmartCrusherConfig,
    ) -> (SmartCrusher, Arc<crate::ccr::InMemoryCcrStore>) {
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;

        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        let c = SmartCrusherBuilder::new(cfg)
            .with_default_oss_setup()
            .with_default_compaction()
            .with_ccr_store(store_dyn)
            .build();
        (c, store)
    }

    #[test]
    fn lossless_only_droppable_array_passes_through_no_markers_no_store_writes() {
        // The shape that DOES lossy-drop under defaults (see
        // `lossy_falls_through_when_savings_below_threshold`): low
        // uniqueness, lossless gate forced unreachable. Under
        // `lossless_only` the lossy candidate must never be BUILT —
        // every row passes through, no `<<ccr:` pointer of any shape is
        // minted, and the store stays empty.
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // lossless never clears
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let items: Vec<Value> = (0..50).map(|_| json!({"status": "ok"})).collect();

        let result = c.crush_array(&items, "", 1.0);

        assert_eq!(result.items.len(), 50, "no row may be dropped");
        assert_eq!(result.strategy_info, "skip:lossless_only");
        assert!(result.ccr_hash.is_none());
        assert!(result.dropped_summary.is_empty());
        assert!(result.compacted.is_none());
        assert_eq!(store.len(), 0, "strict mode must not write the CCR store");
    }

    #[test]
    fn lossless_only_ships_proven_lossless_render_without_markers() {
        // Cleanly tabular input still compacts LOSSLESSLY in strict mode
        // — the mode forbids lossy candidates, not the verified lossless
        // tier. The render must carry every row and no `<<ccr:` pointer.
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let items: Vec<Value> = (0..50)
            .map(|i| json!({"id": i, "name": format!("u_{i}"), "status": "ok"}))
            .collect();

        let result = c.crush_array(&items, "", 1.0);

        let compacted = result.compacted.expect("lossless render should ship");
        assert!(compacted.starts_with("[50]{"), "got: {compacted}");
        assert!(
            !compacted.contains("<<ccr:"),
            "strict-mode lossless render must be pointer-free, got: {compacted}"
        );
        assert!(result.strategy_info.starts_with("lossless:table"));
        assert_eq!(result.items.len(), 50);
        assert!(result.ccr_hash.is_none());
        assert!(result.dropped_summary.is_empty());
        assert_eq!(store.len(), 0, "a pure lossless win writes nothing");
    }

    #[test]
    fn lossless_only_rejects_opaque_bearing_lossless_render() {
        // Long base64 columns normally make the compactor substitute
        // cells with `<<ccr:HASH,base64,SIZE>>` (opaque refs) — a
        // recoverable render that still hides visible bytes. Strict mode
        // must neither ship such a render NOR write the store: the
        // builder disables the stage's `substitute_opaque` (the Defect-2
        // store write is EAGER inside `compact()`, so a routing-layer
        // rejection alone would come after the write), and the blobs-
        // verbatim render then fails the savings gate → passthrough.
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let blob = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/".repeat(64);
        let items: Vec<Value> = (0..30)
            .map(|i| json!({"path": format!("src/f{i}.py"), "content": blob.clone()}))
            .collect();

        // Counterfactual precondition: under the DEFAULT config this very
        // fixture ships an opaque-substituted render (pointers in the
        // output) — proving the strict-mode decline below is the work of
        // the new gates, not an accident of the fixture.
        let (default_c, _s) = lossless_only_crusher(SmartCrusherConfig::default());
        let default_result = default_c.crush_array(&items, "", 1.0);
        assert!(
            default_result
                .compacted
                .as_deref()
                .is_some_and(|r| r.contains("<<ccr:")),
            "fixture precondition: default config must ship an opaque render, got {}",
            default_result.strategy_info
        );

        let result = c.crush_array(&items, "", 1.0);

        assert_eq!(result.items.len(), 30, "nothing may be dropped");
        assert!(result.ccr_hash.is_none());
        assert!(result.dropped_summary.is_empty());
        assert_eq!(
            store.len(),
            0,
            "the eager Defect-2 opaque write must not fire in strict mode"
        );
        // With substitution off, the CONSTANT blob column folds into the
        // declaration (`content:string=<blob>` — verbatim, exactly once):
        // a legitimately PURE lossless render that may still win the
        // savings gate. Whichever way the gate lands, the strict-mode
        // output must carry the blob bytes verbatim and no pointer.
        match &result.compacted {
            Some(render) => {
                assert!(
                    !render.contains("<<ccr:"),
                    "strict-mode render must be pointer-free"
                );
                assert!(
                    render.contains(&blob),
                    "the blob must appear verbatim in the render"
                );
                assert!(result.strategy_info.starts_with("lossless:"));
            }
            None => {
                let rendered = serde_json::to_string(&result.items).unwrap();
                assert!(!rendered.contains("<<ccr:"), "no pointer may be minted");
                assert!(rendered.contains(&blob), "blobs must stay verbatim");
            }
        }
    }

    #[test]
    fn lossless_only_distinct_blob_rows_pass_through_untouched() {
        // DISTINCT per-row blobs (distinct at BOTH ends, so neither the
        // constant fold nor the affix/head-dict encoders apply): the
        // blobs-verbatim render cannot clear the savings gate, and the
        // opaque-substituted render is disabled in strict mode — so the
        // array must pass through untouched with an empty store.
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let base = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        let items: Vec<Value> = (0..30)
            .map(|i| {
                json!({
                    "path": format!("src/f{i}.py"),
                    "content": format!("{i:04}{}{i:04}", base.repeat(64)),
                })
            })
            .collect();

        let result = c.crush_array(&items, "", 1.0);

        assert_eq!(result.items.len(), 30, "nothing may be dropped");
        assert!(result.compacted.is_none(), "no render can clear the gate");
        assert!(result.ccr_hash.is_none());
        assert!(result.dropped_summary.is_empty());
        assert_eq!(store.len(), 0, "strict mode must not write the store");
        let rendered = serde_json::to_string(&result.items).unwrap();
        assert!(!rendered.contains("<<ccr:"), "no pointer may be minted");
    }

    #[test]
    fn lossless_only_routing_gate_declines_opaque_render_even_if_stage_substitutes() {
        // Belt-and-braces layer: the ROUTING gate (`opaque_ok` in
        // `crush_array_inner`) must decline an opaque-bearing render even
        // when a hand-composed crusher pairs `lossless_only` with a stage
        // that still substitutes (e.g. via `from_parts` — the production
        // builder always disables `substitute_opaque`, but the strict-mode
        // output invariant must not depend on wiring).
        let (mut c, _store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        // Adversarial wiring: re-enable substitution behind the mode's back.
        c.compaction
            .as_mut()
            .expect("builder installs a stage")
            .config
            .substitute_opaque = true;
        let blob = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/".repeat(64);
        let items: Vec<Value> = (0..30)
            .map(|i| json!({"path": format!("src/f{i}.py"), "content": blob.clone()}))
            .collect();

        let result = c.crush_array(&items, "", 1.0);

        assert!(
            result.compacted.is_none(),
            "routing gate must decline the opaque render in strict mode"
        );
        assert_eq!(result.items.len(), 30, "nothing may be dropped");
        assert!(result.ccr_hash.is_none());
        let rendered = serde_json::to_string(&result.items).unwrap();
        assert!(!rendered.contains("<<ccr:"), "no pointer may ship");
    }

    #[test]
    fn lossless_only_string_and_number_arrays_pass_through() {
        // The non-dict crushers are sampling drops (lossy-recoverable);
        // strict mode routes their arrays to plain recursive descent.
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });

        let strings: Vec<Value> = (0..200)
            .map(|i| Value::String(format!("log-line-{i}-payload")))
            .collect();
        let (out, _info) = c.process_value(&Value::Array(strings.clone()), 0, "", 1.0);
        assert_eq!(
            out.as_array().map(|a| a.len()),
            Some(200),
            "string array must pass through untouched"
        );

        let numbers: Vec<Value> = (0..200).map(|i| json!(i * 7)).collect();
        let (out, _info) = c.process_value(&Value::Array(numbers.clone()), 0, "", 1.0);
        assert_eq!(
            out.as_array().map(|a| a.len()),
            Some(200),
            "number array must pass through untouched"
        );

        let mixed: Vec<Value> = (0..100)
            .flat_map(|i| [Value::String(format!("s{i}")), json!(i)])
            .collect();
        let (out, _info) = c.process_value(&Value::Array(mixed.clone()), 0, "", 1.0);
        assert_eq!(
            out.as_array().map(|a| a.len()),
            Some(200),
            "mixed array must pass through untouched"
        );

        assert_eq!(store.len(), 0, "no drops → no store writes");
    }

    #[test]
    fn lossless_only_disables_opaque_string_substitution() {
        // The walker-equivalent string path normally substitutes long
        // base64 blobs with `<<ccr:HASH,base64,SIZE>>`. Strict mode keeps
        // the blob verbatim (visible bytes are never hidden).
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let big_b64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=".repeat(8);
        let doc = json!({"id": 1, "blob": big_b64});

        let (out, _info) = c.process_value(&doc, 0, "", 1.0);

        let blob = out.pointer("/blob").and_then(|v| v.as_str()).unwrap();
        assert_eq!(blob, big_b64, "blob must stay verbatim in strict mode");
        assert_eq!(store.len(), 0);
    }

    #[test]
    fn lossless_only_disables_object_key_crush() {
        // Object key-crush drops keys with no recovery pointer at all —
        // doubly forbidden in strict mode. Every key must survive.
        let (c, _store) = lossless_only_crusher(SmartCrusherConfig {
            min_tokens_to_crush: 1, // make key-crush eager if it were allowed
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let mut obj = serde_json::Map::new();
        for i in 0..40 {
            obj.insert(
                format!("key_{i}"),
                Value::String(format!("value-{i}-with-some-padding-to-cost-tokens")),
            );
        }

        let (out, _info) = c.process_value(&Value::Object(obj.clone()), 0, "", 1.0);

        assert_eq!(
            out.as_object().map(|o| o.len()),
            Some(40),
            "no key may be dropped in strict mode"
        );
    }

    #[test]
    fn lossless_only_end_to_end_crush_output_carries_no_ccr_pointer() {
        // Public `crush()` over a document holding every lossy-tempting
        // shape at once: a droppable dict sub-array, a big string array,
        // and an opaque blob. Strict mode output must contain no `<<ccr:`
        // pointer anywhere and surface no typed row-drop hashes.
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // lossless never clears → pure passthrough
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let blob = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/".repeat(16);
        let doc = json!({
            "rows": (0..50).map(|_| json!({"status": "ok"})).collect::<Vec<_>>(),
            "lines": (0..100).map(|i| format!("line-{i}")).collect::<Vec<_>>(),
            "attachment": blob,
        });
        let content = serde_json::to_string(&doc).unwrap();

        let result = c.crush(&content, "", 1.0);

        assert!(
            !result.compressed.contains("<<ccr:"),
            "strict-mode crush() output must be pointer-free, got: {}",
            &result.compressed[..result.compressed.len().min(300)]
        );
        assert!(result.dropped.is_empty(), "no typed recovery refs");
        assert!(result.ccr_hashes().is_empty(), "no typed row-drop hashes");
        assert!(result.row_index_markers().is_empty());
        assert_eq!(store.len(), 0, "no store writes in strict mode");
        // Every original row/line survives (parse and count).
        let parsed: Value = serde_json::from_str(&result.compressed).unwrap();
        assert_eq!(
            parsed.pointer("/rows").unwrap().as_array().unwrap().len(),
            50
        );
        assert_eq!(
            parsed.pointer("/lines").unwrap().as_array().unwrap().len(),
            100
        );
        assert_eq!(
            parsed.pointer("/attachment").unwrap().as_str().unwrap(),
            blob
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
            "crusher",
            "proxy",
            "ccr",
            "router",
            "bench",
            "tokenizer",
            "store",
            "pipeline",
            "compaction",
            "relevance",
        ];
        const VERBS: [&str; 10] = [
            "add", "remove", "rework", "guard", "pin", "extend", "isolate", "deflake", "speed up",
            "harden",
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
                            let row_arr: Vec<Value> = serde_json::from_str(&row_payload).unwrap();
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

        let distinct_inputs: HashSet<String> = items.iter().map(canonical_json_for_match).collect();
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
        let items: Vec<Value> = (0..200)
            .map(|i| json!({"status": "ok", "seq": i}))
            .collect();
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
            .persist_dropped(&items, 5, &[25, 26, 27, 28, 29], PersistMode::Commit)
            .expect("dropped_count>0 → Some");
        let expected = hash_canonical(&canonical_array_json(&items));
        assert_eq!(persisted.hash, expected, "hash scheme must be unchanged");
        assert_eq!(persisted.hash.len(), 12);
        assert!(persisted
            .marker
            .contains(&format!("<<ccr:{expected} 5_rows_offloaded>>")));
        // Zero dropped → None (no hash, no marker, no store write).
        assert!(c
            .persist_dropped(&items, 0, &[], PersistMode::Commit)
            .is_none());
    }

    // ---------- COR-4 / COR-20: dropped-rows-only granular persist ----------

    #[test]
    fn persist_dropped_chunks_only_dropped_rows_and_marker_counts_them() {
        // COR-4: kept rows must NEVER be written to the store — one
        // granular chunk per DROPPED row, nothing else. COR-20: the
        // `_ccr_rows` marker advertises EXACTLY the number of chunks the
        // index holds (it previously claimed `dropped_count` chunks over
        // an every-original-row index — "keep 7 of 60" rendered
        // `53_chunks` over a 60-entry index, a model-visible lie).
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..10).map(|i| json!({"id": i})).collect();
        // Keep rows 0..7, drop rows 7, 8, 9.
        let dropped_indices = [7usize, 8, 9];
        let persisted = c
            .persist_dropped(&items, 3, &dropped_indices, PersistMode::Commit)
            .expect("drop → Some");

        // Marker advertises exactly the chunks the index holds (COR-20).
        let idx_marker = persisted
            .row_index_marker()
            .expect("granular index fires for a small drop");
        assert!(
            idx_marker.contains("#rows 3_chunks>>"),
            "marker must advertise the 3 dropped-row chunks, got: {idx_marker}"
        );

        // The index holds one hash per DROPPED row, in original order,
        // each resolving to exactly `[row]`.
        let index_raw = store
            .get(&format!("{}#rows", persisted.hash))
            .expect("row index stored");
        let row_hashes: Vec<String> = serde_json::from_str(&index_raw).unwrap();
        assert_eq!(row_hashes.len(), 3, "index holds dropped rows only");
        for (rh, &orig_idx) in row_hashes.iter().zip(dropped_indices.iter()) {
            let payload = store.get(rh).expect("dropped-row chunk resolves");
            assert_eq!(
                payload,
                canonical_array_json(std::slice::from_ref(&items[orig_idx]))
            );
        }

        // Kept rows are NOT in the store under their per-row hashes.
        for kept in &items[..7] {
            let kept_canonical = canonical_array_json(std::slice::from_ref(kept));
            let kept_hash = hash_canonical(&kept_canonical);
            assert!(
                store.get(&kept_hash).is_none(),
                "kept row {kept} must never be written to the store (COR-4)"
            );
        }

        // Store contents: 3 chunks + 1 index + 1 whole-blob, nothing more.
        assert_eq!(store.len(), 5, "3 chunks + index + whole-blob exactly");
    }

    #[test]
    fn persist_dropped_skips_granular_chunking_for_oversized_drops() {
        // COR-4 store-flood gate: when the dropped-row count exceeds
        // `store.capacity() / 4`, granular chunking stands down entirely
        // — whole-blob only, `row_index_marker: None` — so a single big
        // array can never self-evict its own chunks (and two big arrays
        // in one document can never evict each other's whole-blobs).
        let (c, store) = crusher_with_store();
        let budget = store.capacity() / GRANULAR_CHUNK_CAPACITY_DIVISOR;

        // One past the budget → granular skipped, whole-blob only.
        let n_over = budget + 8;
        let items: Vec<Value> = (0..n_over).map(|i| json!({"id": i})).collect();
        let dropped_indices: Vec<usize> = (0..=budget).collect(); // budget+1 dropped
        let persisted = c
            .persist_dropped(
                &items,
                dropped_indices.len(),
                &dropped_indices,
                PersistMode::Commit,
            )
            .expect("drop → Some");
        assert!(
            persisted.row_index_marker().is_none(),
            "oversized drop must not advertise a granular index"
        );
        assert_eq!(
            store.len(),
            1,
            "oversized drop persists the whole-blob ONLY (no chunk flood)"
        );
        let payload = store.get(&persisted.hash).expect("whole-blob resolves");
        assert_eq!(payload, canonical_array_json(&items));

        // Exactly AT the budget → granular still fires (inclusive bound).
        let (c2, store2) = crusher_with_store();
        let at_budget: Vec<usize> = (0..budget).collect();
        let persisted2 = c2
            .persist_dropped(&items, at_budget.len(), &at_budget, PersistMode::Commit)
            .expect("drop → Some");
        assert!(
            persisted2.row_index_marker().is_some(),
            "a drop at the budget keeps proportional retrieval"
        );
        assert_eq!(
            store2.len(),
            budget + 2,
            "chunks + index + whole-blob at the budget"
        );
    }

    #[test]
    fn persist_dropped_skip_mode_is_marker_identical_and_writes_nothing() {
        // COR-28 persist-skip contract: `PersistMode::Skip` must return a
        // BYTE-IDENTICAL DroppedPersist (hash, whole-blob marker, and
        // granular row-index marker — the inputs to MinTokens routing)
        // while writing NOTHING to the store (and deferring nothing —
        // there is no later commit for the mixed arm). If the marker text
        // could shift, the mixed arm's inner routing would shift with it
        // and the skip would no longer be behavior-invisible.
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..10).map(|i| json!({"id": i})).collect();
        let dropped_indices = [7usize, 8, 9];

        let skipped = c
            .persist_dropped(&items, 3, &dropped_indices, PersistMode::Skip)
            .expect("drop → Some regardless of persist mode");
        assert_eq!(store.len(), 0, "Skip mode must write nothing");
        assert!(
            skipped.pending_writes.is_empty(),
            "Skip mode must defer nothing either (no later commit exists)"
        );

        let persisted = c
            .persist_dropped(&items, 3, &dropped_indices, PersistMode::Commit)
            .expect("drop → Some");
        assert_eq!(skipped.hash, persisted.hash);
        assert_eq!(skipped.marker, persisted.marker);
        assert_eq!(skipped.row_index_marker(), persisted.row_index_marker());
        assert!(
            persisted.pending_writes.is_empty(),
            "Commit mode writes through — nothing rides back deferred"
        );
        assert_eq!(
            store.len(),
            5,
            "Commit mode still writes 3 chunks + index + whole-blob"
        );
    }

    #[test]
    fn persist_dropped_collect_mode_defers_writes_and_replay_matches_commit() {
        // P0-4 Collect contract: hash + markers byte-identical to Commit
        // (routing built on them cannot shift), NOTHING written until the
        // caller commits — and replaying the deferred writes reproduces
        // Commit-mode store state exactly (same keys, same payloads, same
        // chunks → index → whole-blob order).
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..10).map(|i| json!({"id": i})).collect();
        let dropped_indices = [7usize, 8, 9];

        let collected = c
            .persist_dropped(&items, 3, &dropped_indices, PersistMode::Collect)
            .expect("drop → Some");
        assert_eq!(store.len(), 0, "Collect must write nothing before commit");
        assert_eq!(
            collected.pending_writes.len(),
            5,
            "3 chunks + index + whole-blob ride back deferred"
        );

        let (c2, store2) = crusher_with_store();
        let committed = c2
            .persist_dropped(&items, 3, &dropped_indices, PersistMode::Commit)
            .expect("drop → Some");
        assert_eq!(collected.hash, committed.hash, "hash is mode-independent");
        assert_eq!(collected.marker, committed.marker);
        assert_eq!(collected.row_index_marker(), committed.row_index_marker());

        // The whole-blob write is LAST (bounded-store eviction rationale
        // — redundant chunks shed before the recovery backstop).
        assert_eq!(
            collected.pending_writes.last().map(|w| w.key.as_str()),
            Some(committed.hash.as_str()),
            "whole-blob must be the final deferred write"
        );

        // Replaying the deferred writes reproduces Commit-mode state.
        c.commit_ccr_writes(collected.pending_writes);
        assert_eq!(store.len(), store2.len());
        assert_eq!(store.len(), 5, "3 chunks + index + whole-blob exactly");
        assert_eq!(
            store.get(&committed.hash),
            store2.get(&committed.hash),
            "whole-blob payload identical across modes"
        );
        let index_key = format!("{}#rows", committed.hash);
        assert_eq!(
            store.get(&index_key),
            store2.get(&index_key),
            "row index identical across modes"
        );
        let row_hashes: Vec<String> =
            serde_json::from_str(&store.get(&index_key).expect("index present")).unwrap();
        for rh in &row_hashes {
            assert_eq!(
                store.get(rh),
                store2.get(rh),
                "per-row chunk {rh} identical across modes"
            );
        }
    }

    #[test]
    fn dropped_indices_helpers_cover_complement_and_multiset_diff() {
        // Dict path: complement of the plan's keep set, out-of-range
        // keeps ignored, duplicates collapsed.
        assert_eq!(dropped_indices_from_kept(&[0, 2, 4], 5), vec![1, 3]);
        assert_eq!(dropped_indices_from_kept(&[], 3), vec![0, 1, 2]);
        assert_eq!(dropped_indices_from_kept(&[0, 0, 99], 3), vec![1, 2]);
        assert_eq!(
            dropped_indices_from_kept(&[0, 1, 2], 3),
            Vec::<usize>::new()
        );

        // Non-dict paths: multiset diff on values. Duplicates consume
        // one-for-one; synthesized kept items consume nothing.
        let original = vec![json!("a"), json!("a"), json!("b"), json!("c")];
        let kept = vec![json!("a"), json!("c"), json!("<summary>")];
        assert_eq!(
            dropped_indices_by_multiset_diff(&original, &kept),
            vec![1, 2],
            "one 'a' kept consumes index 0; 'b' and the second 'a' dropped"
        );
        assert_eq!(
            dropped_indices_by_multiset_diff(&original, &original),
            Vec::<usize>::new()
        );
    }

    #[test]
    fn hash_canonical_pinned_vectors() {
        // Rule-2 pin (anti parallel-mutation blindness): fixed SHA-256[:12]
        // literals over the EXACT canonical bytes `serde_json::to_string`
        // emits. A truncation (`take(6)`→`take(5)`), a hex-format change, or
        // a hasher swap FLIPS a literal here — unlike the sibling
        // `persist_dropped_hash_is_byte_identical_to_inline_dict_scheme`,
        // which RECOMPUTES `expected` and would survive every such mutation.
        // Literals produced once in Python and pinned identically on the
        // Python side (tests/test_ccr_hash_parity_vectors.py) — the two
        // pins together are the Py↔Rust parity lock for the CCR recovery key:
        //   python3 -c "import hashlib; print(hashlib.sha256(C.encode()).hexdigest()[:12])"
        assert_eq!(hash_canonical("[]"), "4f53cda18c2b");
        assert_eq!(
            hash_canonical(r#"["alpha","beta","gamma"]"#),
            "a3e185260009"
        );
        assert_eq!(hash_canonical("[1,2,3,4,5]"), "f5baf0e4336f");
        assert_eq!(
            hash_canonical(r#"[{"id":1},{"id":2},{"id":3}]"#),
            "d99179347cb1"
        );
        // The canonical serializer must emit EXACTLY those bytes, so each
        // literal above is the hash of what the array drop path actually
        // hashes (closes the loop: a serializer change would flip this).
        assert_eq!(canonical_array_json(&[]), "[]");
        assert_eq!(
            canonical_array_json(&[json!("alpha"), json!("beta"), json!("gamma")]),
            r#"["alpha","beta","gamma"]"#
        );
        assert_eq!(
            canonical_array_json(&[json!({"id": 1}), json!({"id": 2}), json!({"id": 3})]),
            r#"[{"id":1},{"id":2},{"id":3}]"#
        );
    }

    #[test]
    fn hash_canonical_wire_form_pinned_vectors() {
        // TEST-33: the WIRE-FORM half of the Py↔Rust parity lock. This
        // canonical is LITERAL-PRESERVING for decimal tokens (serde_json
        // `arbitrary_precision`: `1.50` stays `1.50`, digits beyond f64
        // precision survive) and normalizes ONLY the exponent spelling
        // (`1E5` → `1e+5`: lowercase `e`, explicit sign — serde's number
        // scanner, not a float round-trip). The documented Python
        // reference (`json.loads` → `json.dumps`) round-trips through
        // float and computes a DIFFERENT key for every numeric vector
        // here (`1.50`→`1.5`, `1E5`→`100000.0`, `1e400`→`Infinity`) — it
        // is valid for Python-normal-form inputs only. Pinned identically
        // (from the canonical TEXT, not parsed values) on the Python side
        // (tests/test_ccr_hash_parity_vectors.py::_WIRE_VECTORS):
        //   python3 -c "import hashlib; print(hashlib.sha256(C.encode()).hexdigest()[:12])"
        let wire_vectors: [(&str, &str, &str); 6] = [
            // (wire input, serde canonical, pinned SHA-256[:12])
            (
                r#"[{"price":1.50}]"#,
                r#"[{"price":1.50}]"#,
                "86cf954ca9f3", // trailing zero preserved verbatim
            ),
            ("[1E5]", "[1e+5]", "5c20cc153829"), // exponent spelling normalized
            ("[1e400]", "[1e+400]", "7e9854d86909"), // overflows f64; token kept
            (
                "[2.5000000000000000000000000001]",
                "[2.5000000000000000000000000001]",
                "44a8948fa037", // beyond f64 precision, preserved verbatim
            ),
            // Non-ASCII and control-char forms — these AGREE with the
            // Python reference (same hashes pinned in its `_VECTORS`);
            // included so the lock covers the full canonical grammar,
            // not just ASCII scalars.
            (
                r#"["café","日本語","naïve"]"#,
                r#"["café","日本語","naïve"]"#,
                "3a6991f2cdbf",
            ),
            (
                r#"["line1\nline2","tab\there","bell\u0007"]"#,
                r#"["line1\nline2","tab\there","bell\u0007"]"#,
                "333b058285a5",
            ),
        ];
        for (input, canonical, expected) in wire_vectors {
            // The production path hashes `canonical_array_json(parsed
            // rows)` — pin that parsing the wire text re-serializes to
            // EXACTLY the canonical bytes above (`preserve_order` +
            // `arbitrary_precision`). A serde feature-flag regression or
            // a serializer swap flips this before it can corrupt a key.
            let items: Vec<Value> =
                serde_json::from_str(input).expect("wire vector must parse as a JSON array");
            assert_eq!(
                canonical_array_json(&items),
                canonical,
                "canonical for wire input {input:?}"
            );
            assert_eq!(
                hash_canonical(canonical),
                expected,
                "hash for {canonical:?}"
            );
        }
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
    fn min_tokens_and_lossless_first() -> (
        SmartCrusher,
        SmartCrusher,
        std::sync::Arc<crate::ccr::InMemoryCcrStore>,
    ) {
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
        (
            mk(RoutingPolicy::MinTokens),
            mk(RoutingPolicy::LosslessFirst),
            store,
        )
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
        assert!(
            r_min.items.len() < items.len(),
            "lossy must actually drop rows"
        );

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

    // ---------- P0-4: no orphan store-writes for a non-shipped lossy candidate ----------
    //
    // The defect these pin: `crush_array_inner` builds the lossy candidate
    // for MinTokens/LosslessFirst arbitration, and `persist_dropped` used
    // to COMMIT its store writes (whole-blob + granular chunks + row
    // index) at build time — before the routing decision. When the
    // LOSSLESS render won and shipped, those writes stayed behind as
    // orphans: entries no surfaced marker names, burning COR-4-bounded
    // capacity and inflating store stats. The fix defers the candidate's
    // writes (collect-only) and commits them exactly when the lossy
    // render ships — persistence for SHIPPED lossy output stays
    // unconditional (the recovery invariant is timing-shifted, never
    // weakened), and hashes/markers are computed identically either way.

    #[test]
    fn lossless_first_win_writes_nothing_for_discarded_lossy_candidate() {
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;
        use std::sync::Arc;

        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        let cfg = SmartCrusherConfig {
            routing_policy: RoutingPolicy::LosslessFirst,
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusherBuilder::new(cfg)
            .with_default_oss_setup()
            .with_default_compaction()
            .with_ccr_store(store_dyn)
            .build();
        // Low-uniqueness (analyzer willing to crush → a real lossy DROP
        // candidate is built) AND cleanly tabular (lossless clears the
        // 0.30 gate) → both candidates exist; LosslessFirst ships lossless.
        let items: Vec<Value> = (0..50).map(|i| json!({"status": "ok", "seq": i})).collect();

        let result = c.crush_array(&items, "", 1.0);

        // Precondition (asserted, not if-guarded): lossless shipped.
        assert!(
            result.compacted.is_some() && result.ccr_hash.is_none(),
            "precondition: lossless render must ship under LosslessFirst, got {}",
            result.strategy_info
        );
        assert_eq!(result.items.len(), items.len(), "lossless drops nothing");
        // The discarded lossy candidate must leave NO entries behind.
        assert_eq!(
            store.len(),
            0,
            "discarded lossy candidate must not commit store writes (orphan entries)"
        );
    }

    #[test]
    fn min_tokens_lossless_win_writes_nothing_for_discarded_lossy_candidate() {
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;
        use std::sync::Arc;

        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        // Default policy IS MinTokens; spelled out because the test is
        // specifically about the MinTokens arbitration arm.
        let cfg = SmartCrusherConfig {
            routing_policy: RoutingPolicy::MinTokens,
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusherBuilder::new(cfg)
            .with_default_oss_setup()
            .with_default_compaction()
            .with_ccr_store(store_dyn)
            .build();
        // The pinned MinTokens lossless-win shape (see
        // `min_tokens_ships_lossless_when_it_is_fewer_tokens`): identical
        // low-cardinality rows dedup so hard that the lossless table is
        // ≤ tokens vs the drop render — ties go to lossless. The lossy
        // candidate is still BUILT for arbitration; it must not write.
        let items: Vec<Value> = (0..12).map(|_| json!({"a": 1, "b": 2})).collect();

        let result = c.crush_array(&items, "", 1.0);

        assert!(
            result.compacted.is_some() && result.ccr_hash.is_none(),
            "precondition: lossless render must win under MinTokens here, got {}",
            result.strategy_info
        );
        assert_eq!(
            store.len(),
            0,
            "MinTokens lossless win must leave no orphan lossy store writes"
        );
    }

    #[test]
    fn min_tokens_lossy_win_still_commits_store_writes_unconditionally() {
        // The P0-4 deferral must NOT weaken the recovery invariant: when
        // the lossy render SHIPS out of the arbitration arm (both
        // candidates existed, lossy strictly fewer tokens), its store
        // writes are committed exactly as before — whole-blob under the
        // surfaced hash, plus the granular row index when in budget. Only
        // the write TIMING moved (build → ship decision).
        let (min_tokens, _lossless_first, store) = min_tokens_and_lossless_first();
        let items: Vec<Value> = (0..90).map(log_shaped_row).collect();

        let r = min_tokens.crush_array(&items, "", 1.0);

        let h = r
            .ccr_hash
            .as_ref()
            .expect("lossy is the pinned winner for logs-shaped data");
        let dropped = items.len() - r.items.len();
        assert!(dropped > 0, "lossy must actually drop rows");
        // Whole-blob committed under the surfaced hash.
        assert_eq!(
            store.get(h).as_deref(),
            Some(canonical_array_json(&items).as_str()),
            "shipped lossy render must persist the whole-blob (unconditional)"
        );
        // Granular index + one chunk per dropped row committed too (this
        // drop is well inside the capacity/4 budget).
        let idx_marker = r
            .row_index_marker
            .as_ref()
            .expect("store-backed in-budget drop advertises the row index");
        let index_raw = store
            .get(&format!("{h}#rows"))
            .expect("row index must be committed when the lossy render ships");
        let row_hashes: Vec<String> = serde_json::from_str(&index_raw).unwrap();
        assert_eq!(row_hashes.len(), dropped, "one chunk per dropped row");
        assert!(
            idx_marker.contains(&format!("#rows {}_chunks>>", row_hashes.len())),
            "marker advertises exactly the committed chunk count, got: {idx_marker}"
        );
        assert_eq!(
            store.len(),
            dropped + 2,
            "chunks + index + whole-blob — nothing more, nothing less"
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

    // ── crush() typed row-drop fields (pass 1a parity) ──────────────────
    //
    // `crush()` now surfaces every row-drop hash + row-index marker TYPED
    // on `CrushResult` so the Python shim mirrors them DIRECTLY instead of
    // scraping `<<ccr:HASH>>` out of the rendered text. These tests pin the
    // contract at the Rust boundary; the Python parity test
    // (`tests/test_crush_typed_hash_parity.py`) pins the end-to-end mirror.

    /// Build a lossy-forced crusher WITH a store so row-drops persist and
    /// row-index markers populate. Mirrors the harness used by the
    /// no-silent-loss tests above.
    fn lossy_crusher_with_store() -> (SmartCrusher, Arc<dyn CcrStore>) {
        use crate::ccr::InMemoryCcrStore;
        let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // force lossy row-drop path
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusherBuilder::new(cfg)
            .with_ccr_store(Arc::clone(&store))
            .build();
        (c, store)
    }

    #[test]
    fn crush_surfaces_typed_row_drop_hash_matching_embedded_marker() {
        let (c, _store) = lossy_crusher_with_store();
        // Single droppable dict array. Sized so the DROPPED count stays
        // within the granular chunk budget (`capacity / 4` = 250 for the
        // default store) — an oversized drop persists the whole-blob
        // only (COR-4) and would surface no row-index marker to pin.
        let items: Vec<Value> = (0..200)
            .map(|i| json!({"id": i, "status": "ok", "svc": "api"}))
            .collect();
        let content = serde_json::to_string(&items).unwrap();
        let r = c.crush(&content, "", 1.0);

        // A drop happened → at least one typed hash, and the SAME hash is
        // embedded in the rendered `<<ccr:HASH N_rows_offloaded>>` marker.
        // Uses the DERIVED back-compat getters — the corpus lock for R1's
        // field→getter promotion (values asserted against the RENDERED
        // text, not against each other).
        assert!(
            !r.ccr_hashes().is_empty(),
            "row drop must surface a typed ccr_hash; strategy={:?}",
            r.strategy
        );
        for h in &r.ccr_hashes() {
            assert!(
                r.compressed.contains(&format!("<<ccr:{h} ")),
                "typed hash {h} must match the embedded row-drop marker"
            );
        }
        // Store-backed → a row-index marker is surfaced for proportional
        // retrieval, and it is embedded too (as the `_ccr_rows` field).
        assert!(
            !r.row_index_markers().is_empty(),
            "store-backed drop must surface a typed row_index_marker"
        );
        for m in &r.row_index_markers() {
            assert!(
                r.compressed.contains(m.as_str()),
                "typed row_index_marker {m} must be embedded in the output"
            );
        }
        // The typed refs' BARE index keys resolve in the store — the
        // datum R5's Python mirror consumes instead of marker text.
        for d in &r.dropped {
            let key = d
                .row_index_key()
                .expect("store-backed row drop carries a row-index key");
            assert!(
                key.ends_with("#rows"),
                "bare key form is HASH#rows, got: {key}"
            );
        }
    }

    #[test]
    fn crush_surfaces_one_typed_hash_per_dropped_subarray() {
        // ★ The multiplicity the singular-spec model would silently lose:
        // an object with TWO independent droppable sub-arrays must yield
        // TWO distinct typed hashes — one per drop — NOT a single hash.
        let (c, _store) = lossy_crusher_with_store();
        let arr_a: Vec<Value> = (0..300)
            .map(|i| json!({"id": i, "kind": "a", "status": "ok"}))
            .collect();
        let arr_b: Vec<Value> = (0..300)
            .map(|i| json!({"ref": i, "kind": "b", "level": "INFO"}))
            .collect();
        let doc = json!({"alpha": arr_a, "beta": arr_b});
        let content = serde_json::to_string(&doc).unwrap();
        let r = c.crush(&content, "", 1.0);

        // Distinct hashes (the two arrays differ) and both ≥ 2.
        let hashes = r.ccr_hashes();
        let distinct: std::collections::HashSet<&String> = hashes.iter().collect();
        assert!(
            distinct.len() >= 2,
            "two droppable sub-arrays must surface ≥2 distinct typed hashes, \
             got {:?} (strategy={:?})",
            hashes,
            r.strategy
        );
        // Every typed hash is embedded in the output as a row-drop marker
        // (parity with the scrape the Python shim used to depend on).
        let out: Value = serde_json::from_str(&r.compressed).unwrap();
        let mut embedded_scalars: HashSet<String> = HashSet::new();
        let mut embedded_hashes: Vec<String> = Vec::new();
        collect_scalars_and_hashes(&out, &mut embedded_scalars, &mut embedded_hashes);
        let embedded: std::collections::HashSet<&String> = embedded_hashes.iter().collect();
        for h in &hashes {
            assert!(
                embedded.contains(h),
                "typed hash {h} must appear in the embedded row-drop markers \
                 (parity with the scrape)"
            );
        }
    }

    // ── COR-44: magic-key guard in smart_crush_content_collecting ──

    #[test]
    fn smart_crush_content_passthrough_on_serde_private_marker() {
        // COR-44: with arbitrary_precision + raw_value enabled, feeding
        // {"$serde_json::private::Number":"123"} to serde_json::from_str
        // would silently return the number literal 123 — mutating the input.
        // The guard must decline parsing and return the original bytes
        // unchanged (was_modified=false, no info, no dropped refs).
        let c = crusher();
        let magic = r#"{"$serde_json::private::Number":"123"}"#;
        let (result, was_modified, info, dropped) =
            c.smart_crush_content_collecting(magic, "", 1.0);
        assert_eq!(
            result, magic,
            "magic-key input must be returned byte-identical"
        );
        assert!(!was_modified, "magic-key input must not be marked modified");
        assert!(info.is_empty(), "no strategy info for declined input");
        assert!(dropped.is_empty(), "no dropped refs for declined input");
    }

    #[test]
    fn smart_crush_content_wrapper_delegates_to_collecting_and_drops_sink() {
        // HONEST SCOPE: `smart_crush_content` IS a wrapper over
        // `smart_crush_content_collecting` (it calls it and discards the
        // 4th element), so this only proves the wrapper forwards render +
        // flags faithfully and that the collecting variant actually
        // collects on a multi-array doc — NOT byte-identity vs the
        // pre-typed-field behavior. The byte-identity-vs-before guarantee
        // is empirical: the floor bench (retain=1.000 on every dataset +
        // needle 100%) and grammar-characterization (marker text frozen).
        let (c, _store) = lossy_crusher_with_store();
        let arr_a: Vec<Value> = (0..300).map(|i| json!({"id": i, "status": "ok"})).collect();
        let arr_b: Vec<Value> = (0..300).map(|i| json!(format!("log-line-{i}"))).collect();
        let doc = json!({"alpha": arr_a, "beta": arr_b, "n": 7});
        let content = serde_json::to_string(&doc).unwrap();

        let (render_wrapper, mod_w, info_w) = c.smart_crush_content(&content, "", 1.0);
        let (render_collect, mod_c, info_c, dropped) =
            c.smart_crush_content_collecting(&content, "", 1.0);

        // Wrapper forwards the render + flags unchanged.
        assert_eq!(render_wrapper, render_collect);
        assert_eq!(mod_w, mod_c);
        assert_eq!(info_w, info_c);
        // And the collecting variant actually collected the drops.
        assert!(
            !dropped.is_empty(),
            "expected the multi-array doc to drop rows and collect hashes"
        );
    }
}
