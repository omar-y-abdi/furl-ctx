//! PyO3 bindings for furl-core. Exposed to Python as `furl_ctx._core`.
//!
//! # Stage 3b — diff_compressor bridge
//!
//! The `DiffCompressor` family is exported here so the Python
//! `ContentRouter` can route to the Rust implementation in-process via
//! PyO3 instead of running the Python port. Backend selection happens in
//! `furl_ctx.transforms._rust_diff_compressor.RustBackedDiffCompressor`,
//! which mirrors the Python `DiffCompressor` API one-for-one (so callers
//! don't notice the swap).
//!
//! Why in-process: ContentRouter compresses on the engine's hot path. Any
//! IPC / subprocess / RPC bridge would dominate the cost we're trying to
//! save. PyO3 calls cost ~microseconds; staying in-process is ~free.

use std::any::Any;
use std::collections::BTreeMap;

use furl_core::signals::{
    ImportanceCategory, ImportanceContext, KeywordDetector, KeywordRegistry, LineImportanceDetector,
};
use furl_core::transforms::smart_crusher::compaction::{
    has_serde_private_marker, DocumentCompactor,
};
use furl_core::transforms::smart_crusher::{
    CrushResult as RustCrushResult, DroppedRef as RustDroppedRef,
    RoutingPolicy as RustRoutingPolicy, SmartCrusher as RustSmartCrusher,
    SmartCrusherConfig as RustSmartCrusherConfig,
};
use furl_core::transforms::tag_protector::{
    is_known_html_tag as rust_is_known_html_tag, known_html_tag_names as rust_known_html_tag_names,
    protect_tags as rust_protect_tags, restore_tags as rust_restore_tags,
};
use furl_core::transforms::{
    detect as rust_detect_chain, DetectionResult as RustDetectionResult, DiffCompressionResult,
    DiffCompressor, DiffCompressorConfig, DiffCompressorStats,
    LogCompressionResult as RustLogResult, LogCompressor as RustLogCompressor,
    LogCompressorConfig as RustLogConfig, LogCompressorStats as RustLogStats,
    SearchCompressionResult as RustSearchResult, SearchCompressor as RustSearchCompressor,
    SearchCompressorConfig as RustSearchConfig, SearchCompressorStats as RustSearchStats,
    TextCrushResult as RustTextCrushResult, TextCrusher as RustTextCrusher,
    TextCrusherConfig as RustTextCrusherConfig, TextCrusherStats as RustTextCrusherStats,
};
use pyo3::prelude::*;
use pyo3::types::PyDict;

/// Identity stub used by the Python smoke test to verify linkage.
#[pyfunction]
fn hello() -> &'static str {
    furl_core::hello()
}

/// Build the `ValueError` raised for invalid caller input at the FFI
/// boundary. Centralized so every binding reports bad input the same way
/// (and none of them panic — see `crush_array_json`).
fn invalid_input(msg: String) -> PyErr {
    pyo3::exceptions::PyValueError::new_err(msg)
}

/// Convert a caught `catch_unwind` panic payload into a `PyRuntimeError`.
///
/// A bare Rust panic crossing the PyO3 boundary surfaces as
/// `pyo3_runtime.PanicException`, a `BaseException` that escapes the caller's
/// `except Exception` (and Python `compress()`'s fail-open). Wrapping the hot
/// bridge methods in `std::panic::catch_unwind` and routing the payload through
/// here turns an engine-bug panic into an ordinary `Exception`, so the
/// fail-open path reverts to the original messages instead of crashing the host
/// request. The workspace keeps `panic = "unwind"` (see Cargo.toml) precisely so
/// this `catch_unwind` is not a no-op.
///
/// The payload is `Box<dyn Any + Send>`; the message string set by
/// `panic!(...)` is either a `&str` (literal) or a `String` (formatted), so we
/// try both before falling back.
fn panic_to_pyerr(payload: Box<dyn Any + Send>) -> PyErr {
    let msg = payload
        .downcast_ref::<&str>()
        .map(|s| (*s).to_string())
        .or_else(|| payload.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "unknown panic".to_string());
    pyo3::exceptions::PyRuntimeError::new_err(format!("Rust panic in furl-core: {msg}"))
}

fn type_name(v: &serde_json::Value) -> &'static str {
    match v {
        serde_json::Value::Null => "null",
        serde_json::Value::Bool(_) => "bool",
        serde_json::Value::Number(_) => "number",
        serde_json::Value::String(_) => "string",
        serde_json::Value::Array(_) => "array",
        serde_json::Value::Object(_) => "object",
    }
}

/// Build the dict returned by `SmartCrusher.crush_array_json`. Kept
/// outside `#[pymethods]` so we can `unwrap()` `set_item` (it cannot
/// fail when keys are static str literals and values are owned String /
/// Option<String> / Option<&'static str>) without tripping the
/// `clippy::useless_conversion` false positive that fired inside the
/// pyo3 method-attribute macro (first seen under pyo3 0.22; kept
/// defensively under the pinned 0.29 — re-check on pyo3 bumps).
///
/// `dropped_refs` (typed recovery refs, §4.2 R3/R4) is converted to a
/// `list[DroppedRef]`; `row_index_key` is the bare `"HASH#rows"` store
/// key of the top-level row-drop (NOT marker text), `None` when no
/// granular index was written. `set_item` on the pyclass list allocates
/// Python objects and can in principle fail — propagated as PyErr.
#[allow(clippy::too_many_arguments)]
fn build_crush_array_dict<'py>(
    py: Python<'py>,
    kept_json: String,
    ccr_hash: Option<String>,
    dropped_summary: String,
    strategy_info: String,
    compacted: Option<String>,
    compaction_kind: Option<&'static str>,
    row_index_key: Option<String>,
    dropped_refs: Vec<PyDroppedRef>,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("items", kept_json).unwrap();
    dict.set_item("ccr_hash", ccr_hash).unwrap();
    dict.set_item("dropped_summary", dropped_summary).unwrap();
    dict.set_item("strategy_info", strategy_info).unwrap();
    dict.set_item("compacted", compacted).unwrap();
    dict.set_item("compaction_kind", compaction_kind).unwrap();
    dict.set_item("row_index_key", row_index_key).unwrap();
    dict.set_item("dropped_refs", dropped_refs)?;
    Ok(dict)
}

// ─── DiffCompressorConfig ──────────────────────────────────────────────────

/// Mirror of `furl_ctx.transforms.diff_compressor.DiffCompressorConfig`.
/// Defaults match Python; constructor accepts every field as a kwarg with
/// the same name and type as the Python dataclass for drop-in
/// compatibility.
#[pyclass(
    name = "DiffCompressorConfig",
    module = "furl_ctx._core",
    from_py_object
)]
#[derive(Clone)]
struct PyDiffCompressorConfig {
    inner: DiffCompressorConfig,
}

#[pymethods]
impl PyDiffCompressorConfig {
    #[new]
    #[pyo3(signature = (
        max_context_lines = 2,
        max_hunks_per_file = 10,
        max_files = 20,
        always_keep_additions = true,
        always_keep_deletions = true,
        enable_ccr = true,
        min_lines_for_ccr = 50,
        min_compression_ratio_for_ccr = 0.8,
        drop_noise_hunks = false,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        max_context_lines: usize,
        max_hunks_per_file: usize,
        max_files: usize,
        always_keep_additions: bool,
        always_keep_deletions: bool,
        enable_ccr: bool,
        min_lines_for_ccr: usize,
        min_compression_ratio_for_ccr: f64,
        drop_noise_hunks: bool,
    ) -> Self {
        Self {
            inner: DiffCompressorConfig {
                max_context_lines,
                max_hunks_per_file,
                max_files,
                always_keep_additions,
                always_keep_deletions,
                enable_ccr,
                min_lines_for_ccr,
                min_compression_ratio_for_ccr,
                drop_noise_hunks,
            },
        }
    }

    // Read-only field accessors mirroring the Python dataclass surface.
    #[getter]
    fn max_context_lines(&self) -> usize {
        self.inner.max_context_lines
    }
    #[getter]
    fn max_hunks_per_file(&self) -> usize {
        self.inner.max_hunks_per_file
    }
    #[getter]
    fn max_files(&self) -> usize {
        self.inner.max_files
    }
    #[getter]
    fn always_keep_additions(&self) -> bool {
        self.inner.always_keep_additions
    }
    #[getter]
    fn always_keep_deletions(&self) -> bool {
        self.inner.always_keep_deletions
    }
    #[getter]
    fn enable_ccr(&self) -> bool {
        self.inner.enable_ccr
    }
    #[getter]
    fn min_lines_for_ccr(&self) -> usize {
        self.inner.min_lines_for_ccr
    }
    #[getter]
    fn min_compression_ratio_for_ccr(&self) -> f64 {
        self.inner.min_compression_ratio_for_ccr
    }
    #[getter]
    fn drop_noise_hunks(&self) -> bool {
        self.inner.drop_noise_hunks
    }

    fn __repr__(&self) -> String {
        format!(
            "DiffCompressorConfig(max_context_lines={}, max_hunks_per_file={}, max_files={}, \
             always_keep_additions={}, always_keep_deletions={}, enable_ccr={}, \
             min_lines_for_ccr={}, min_compression_ratio_for_ccr={}, drop_noise_hunks={})",
            self.inner.max_context_lines,
            self.inner.max_hunks_per_file,
            self.inner.max_files,
            self.inner.always_keep_additions,
            self.inner.always_keep_deletions,
            self.inner.enable_ccr,
            self.inner.min_lines_for_ccr,
            self.inner.min_compression_ratio_for_ccr,
            self.inner.drop_noise_hunks,
        )
    }
}

// ─── DiffCompressionResult ─────────────────────────────────────────────────

/// Mirror of `furl_ctx.transforms.diff_compressor.DiffCompressionResult`.
/// Read-only on the Python side: ContentRouter consumes fields, doesn't
/// mutate. `compression_ratio` and `tokens_saved_estimate` are exposed as
/// methods (not `@property`) — Python callers reach them via `.method()`.
/// The Python adapter wraps and re-exposes them as properties for full
/// dataclass compatibility.
#[pyclass(name = "DiffCompressionResult", module = "furl_ctx._core")]
struct PyDiffCompressionResult {
    inner: DiffCompressionResult,
}

#[pymethods]
impl PyDiffCompressionResult {
    #[getter]
    fn compressed(&self) -> &str {
        &self.inner.compressed
    }
    #[getter]
    fn original_line_count(&self) -> usize {
        self.inner.original_line_count
    }
    #[getter]
    fn compressed_line_count(&self) -> usize {
        self.inner.compressed_line_count
    }
    #[getter]
    fn files_affected(&self) -> usize {
        self.inner.files_affected
    }
    #[getter]
    fn additions(&self) -> usize {
        self.inner.additions
    }
    #[getter]
    fn deletions(&self) -> usize {
        self.inner.deletions
    }
    #[getter]
    fn hunks_kept(&self) -> usize {
        self.inner.hunks_kept
    }
    #[getter]
    fn hunks_removed(&self) -> usize {
        self.inner.hunks_removed
    }
    #[getter]
    fn cache_key(&self) -> Option<String> {
        self.inner.cache_key.clone()
    }

    /// Mirror of Python `@property compression_ratio`. Returns
    /// `compressed_line_count / original_line_count` (1.0 if input was
    /// empty).
    fn compression_ratio(&self) -> f64 {
        if self.inner.original_line_count == 0 {
            1.0
        } else {
            self.inner.compressed_line_count as f64 / self.inner.original_line_count as f64
        }
    }

    /// Mirror of Python `@property tokens_saved_estimate`. Same `chars *
    /// 40 / 4` heuristic; bytes-equivalent numeric result.
    fn tokens_saved_estimate(&self) -> usize {
        let saved = self
            .inner
            .original_line_count
            .saturating_sub(self.inner.compressed_line_count);
        (saved * 40) / 4
    }

    fn __repr__(&self) -> String {
        format!(
            "DiffCompressionResult(compressed=<{} chars>, original_line_count={}, \
             compressed_line_count={}, files_affected={}, additions={}, deletions={}, \
             hunks_kept={}, hunks_removed={}, cache_key={:?})",
            self.inner.compressed.len(),
            self.inner.original_line_count,
            self.inner.compressed_line_count,
            self.inner.files_affected,
            self.inner.additions,
            self.inner.deletions,
            self.inner.hunks_kept,
            self.inner.hunks_removed,
            self.inner.cache_key,
        )
    }
}

// ─── DiffCompressorStats ───────────────────────────────────────────────────

/// Mirror of Rust `DiffCompressorStats` — sidecar observability not
/// present in the Python dataclass. Returned only from `compress_with_stats`,
/// which the Python adapter exposes as a method on the wrapper. `Vec`s are
/// returned as Python lists; the `BTreeMap` becomes a `dict`.
#[pyclass(name = "DiffCompressorStats", module = "furl_ctx._core")]
struct PyDiffCompressorStats {
    inner: DiffCompressorStats,
}

#[pymethods]
impl PyDiffCompressorStats {
    #[getter]
    fn input_lines(&self) -> usize {
        self.inner.input_lines
    }
    #[getter]
    fn output_lines(&self) -> usize {
        self.inner.output_lines
    }
    #[getter]
    fn compression_ratio(&self) -> f64 {
        self.inner.compression_ratio
    }
    #[getter]
    fn files_total(&self) -> usize {
        self.inner.files_total
    }
    #[getter]
    fn files_kept(&self) -> usize {
        self.inner.files_kept
    }
    #[getter]
    fn files_dropped(&self) -> Vec<String> {
        self.inner.files_dropped.clone()
    }
    #[getter]
    fn hunks_total(&self) -> usize {
        self.inner.hunks_total
    }
    #[getter]
    fn hunks_kept(&self) -> usize {
        self.inner.hunks_kept
    }
    #[getter]
    fn hunks_dropped(&self) -> usize {
        self.inner.hunks_dropped
    }
    #[getter]
    fn hunks_dropped_per_file(&self) -> BTreeMap<String, usize> {
        self.inner.hunks_dropped_per_file.clone()
    }
    #[getter]
    fn noise_hunks_elided(&self) -> usize {
        self.inner.noise_hunks_elided
    }
    #[getter]
    fn context_lines_input(&self) -> usize {
        self.inner.context_lines_input
    }
    #[getter]
    fn context_lines_kept(&self) -> usize {
        self.inner.context_lines_kept
    }
    #[getter]
    fn context_lines_trimmed(&self) -> usize {
        self.inner.context_lines_trimmed
    }
    #[getter]
    fn largest_hunk_kept_lines(&self) -> usize {
        self.inner.largest_hunk_kept_lines
    }
    #[getter]
    fn largest_hunk_dropped_lines(&self) -> usize {
        self.inner.largest_hunk_dropped_lines
    }
    #[getter]
    fn parse_warnings(&self) -> Vec<String> {
        self.inner.parse_warnings.clone()
    }
    #[getter]
    fn processing_duration_us(&self) -> u64 {
        self.inner.processing_duration_us
    }
    #[getter]
    fn cache_key_emitted(&self) -> bool {
        self.inner.cache_key_emitted
    }
    #[getter]
    fn ccr_skipped_reason(&self) -> Option<String> {
        self.inner.ccr_skipped_reason.clone()
    }
    #[getter]
    fn file_mode_normalizations(&self) -> Vec<(String, String)> {
        self.inner.file_mode_normalizations.clone()
    }
    #[getter]
    fn binary_files_simplified(&self) -> Vec<String> {
        self.inner.binary_files_simplified.clone()
    }
}

// ─── DiffCompressor ────────────────────────────────────────────────────────

/// Mirror of `furl_ctx.transforms.diff_compressor.DiffCompressor`. The
/// Python adapter wraps this in `RustBackedDiffCompressor` so
/// `ContentRouter` can swap backends transparently.
#[pyclass(name = "DiffCompressor", module = "furl_ctx._core")]
struct PyDiffCompressor {
    inner: DiffCompressor,
}

#[pymethods]
impl PyDiffCompressor {
    /// `__init__(config: DiffCompressorConfig | None = None)` — matches the
    /// Python constructor signature one-for-one.
    #[new]
    #[pyo3(signature = (config = None))]
    fn new(config: Option<&PyDiffCompressorConfig>) -> Self {
        let cfg = config.map(|c| c.inner.clone()).unwrap_or_default();
        Self {
            inner: DiffCompressor::new(cfg),
        }
    }

    /// `compress(content: str, context: str = "") -> DiffCompressionResult`.
    /// Argument order and keyword names match the Python implementation.
    ///
    /// Releases the GIL across the Rust compress call so concurrent
    /// Python threads (uvicorn workers, asyncio tasks) can keep
    /// running while we hash + parse + filter the diff. The
    /// `&str` inputs are copied to owned `String`s first because
    /// PyO3 ties their lifetime to the GIL hold.
    #[pyo3(signature = (content, context = ""))]
    fn compress(
        &self,
        py: Python<'_>,
        content: &str,
        context: &str,
    ) -> PyResult<PyDiffCompressionResult> {
        let content = content.to_string();
        let context = context.to_string();
        // catch_unwind inside detach: keep the GIL released during the
        // Rust compute, catch any panic there, convert after re-acquiring so an
        // engine bug becomes a catchable PyRuntimeError instead of a
        // BaseException that crashes the host (COR-7).
        let inner = py
            .detach(|| {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    self.inner.compress(&content, &context)
                }))
            })
            .map_err(panic_to_pyerr)?;
        Ok(PyDiffCompressionResult { inner })
    }

    /// `compress_with_stats(content, context="") -> (result, stats)`.
    /// Sidecar API not present in Python — exposes the Rust observability
    /// struct alongside the parity-equal result. Returned as a 2-tuple to
    /// keep the call site Pythonic.
    #[pyo3(signature = (content, context = ""))]
    fn compress_with_stats(
        &self,
        py: Python<'_>,
        content: &str,
        context: &str,
    ) -> PyResult<(PyDiffCompressionResult, PyDiffCompressorStats)> {
        let content = content.to_string();
        let context = context.to_string();
        // catch_unwind inside detach (see `panic_to_pyerr`): the
        // sidecar path parses the same diff as `compress` and can hit the
        // same unidiff panics (e.g. orphaned `+++` headers) — same COR-7
        // containment (P0-1).
        let (result, stats) = py
            .detach(|| {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    self.inner.compress_with_stats(&content, &context)
                }))
            })
            .map_err(panic_to_pyerr)?;
        Ok((
            PyDiffCompressionResult { inner: result },
            PyDiffCompressorStats { inner: stats },
        ))
    }
}

// ─── SmartCrusherConfig ────────────────────────────────────────────────────

/// Mirror of `furl_ctx.transforms.smart_crusher.SmartCrusherConfig`.
/// Defaults match Python's dataclass byte-for-byte. The constructor
/// accepts every dataclass field as a kwarg with the same name and type,
/// so the Python shim passes `SmartCrusherConfig(**asdict(py_cfg), ...)` —
/// plus two non-dataclass kwargs it injects explicitly:
/// `relevance_threshold` (the reconciled 0.3 scoring threshold — the
/// retired `RelevanceScorerConfig`'s 0.25 was never forwarded) and
/// `enable_ccr_marker` (derived from the CCR config).
///
/// An unknown kwarg (including the four knobs deleted by SIMP-7:
/// `enabled`, `uniqueness_threshold`, `similarity_threshold`,
/// `include_summaries`) raises `TypeError` — fail-loud wire contract.
#[pyclass(name = "SmartCrusherConfig", module = "furl_ctx._core", from_py_object)]
#[derive(Clone)]
struct PySmartCrusherConfig {
    inner: RustSmartCrusherConfig,
}

#[pymethods]
impl PySmartCrusherConfig {
    #[new]
    #[pyo3(signature = (
        min_items_to_analyze = 5,
        min_tokens_to_crush = 200,
        variance_threshold = 2.0,
        max_items_after_crush = 15,
        preserve_change_points = true,
        dedup_identical_items = true,
        first_fraction = 0.3,
        last_fraction = 0.15,
        relevance_threshold = 0.3,
        lossless_min_savings_ratio = 0.30,
        enable_ccr_marker = true,
        routing_policy = "min-tokens",
        lossless_only = false,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        min_items_to_analyze: usize,
        min_tokens_to_crush: usize,
        variance_threshold: f64,
        max_items_after_crush: usize,
        preserve_change_points: bool,
        dedup_identical_items: bool,
        first_fraction: f64,
        last_fraction: f64,
        relevance_threshold: f64,
        lossless_min_savings_ratio: f64,
        enable_ccr_marker: bool,
        routing_policy: &str,
        lossless_only: bool,
    ) -> PyResult<Self> {
        // Parse the kebab-case routing policy at the boundary so a typo
        // is a clear ValueError, not a silent default.
        let routing_policy = RustRoutingPolicy::from_str(routing_policy).ok_or_else(|| {
            invalid_input(format!(
                "unknown routing_policy {routing_policy:?}; expected one of: \
                 \"min-tokens\", \"lossless-first\""
            ))
        })?;
        Ok(Self {
            inner: RustSmartCrusherConfig {
                min_items_to_analyze,
                min_tokens_to_crush,
                variance_threshold,
                max_items_after_crush,
                preserve_change_points,
                dedup_identical_items,
                first_fraction,
                last_fraction,
                relevance_threshold,
                lossless_min_savings_ratio,
                enable_ccr_marker,
                routing_policy,
                lossless_only,
                // Entropy-floor crushability override: FORCED true here
                // so the Python `compress()` pipeline crushes near-unique
                // no-signal data recoverably (deterministic, aggressive).
                // DELIBERATELY not exposed as a constructor kwarg (owner
                // decision Q12: keep the comment-guarded divergence) —
                // the Python dataclass has no such field, the parity
                // fixtures assume it, and the one Rust-side caller that
                // needs it off (the byte-faithful live-zone dispatcher)
                // constructs its own core config directly. If a Python
                // knob is ever wanted, add the dataclass field + this
                // kwarg in ONE commit (wire-contract rule).
                crush_unique_entities_when_recoverable: true,
            },
        })
    }

    #[getter]
    fn min_items_to_analyze(&self) -> usize {
        self.inner.min_items_to_analyze
    }
    #[getter]
    fn min_tokens_to_crush(&self) -> usize {
        self.inner.min_tokens_to_crush
    }
    #[getter]
    fn variance_threshold(&self) -> f64 {
        self.inner.variance_threshold
    }
    #[getter]
    fn max_items_after_crush(&self) -> usize {
        self.inner.max_items_after_crush
    }
    #[getter]
    fn preserve_change_points(&self) -> bool {
        self.inner.preserve_change_points
    }
    #[getter]
    fn dedup_identical_items(&self) -> bool {
        self.inner.dedup_identical_items
    }
    #[getter]
    fn first_fraction(&self) -> f64 {
        self.inner.first_fraction
    }
    #[getter]
    fn last_fraction(&self) -> f64 {
        self.inner.last_fraction
    }
    #[getter]
    fn relevance_threshold(&self) -> f64 {
        self.inner.relevance_threshold
    }
    #[getter]
    fn lossless_min_savings_ratio(&self) -> f64 {
        self.inner.lossless_min_savings_ratio
    }
    #[getter]
    fn enable_ccr_marker(&self) -> bool {
        self.inner.enable_ccr_marker
    }
    #[getter]
    fn routing_policy(&self) -> &'static str {
        self.inner.routing_policy.as_str()
    }
    #[getter]
    fn lossless_only(&self) -> bool {
        self.inner.lossless_only
    }

    fn __repr__(&self) -> String {
        format!(
            "SmartCrusherConfig(min_items_to_analyze={}, \
             min_tokens_to_crush={}, max_items_after_crush={}, \
             relevance_threshold={})",
            self.inner.min_items_to_analyze,
            self.inner.min_tokens_to_crush,
            self.inner.max_items_after_crush,
            self.inner.relevance_threshold,
        )
    }
}

// ─── DroppedRef ────────────────────────────────────────────────────────────

/// Typed CCR recovery ref carried across the FFI (§4.2) — one per
/// reduction the engine shipped (row-drop or opaque substitution).
///
/// A small pyclass instead of a flat tuple so the row-drop variant never
/// needs a documented `byte_size=0` filler: fields that don't apply to a
/// variant are `None`.
///
/// * `kind_tag` — `"row_drop"` | `"opaque"` (the variant discriminator).
/// * `hash` — the CCR store key (also embedded in the rendered marker).
/// * `row_index_key` — bare granular-index store key (`"HASH#rows"`,
///   NOT marker text); row-drop only, `None` when no index was written.
/// * `opaque_kind` — wire kind token (`"base64"` / `"string"` / `"html"`
///   / custom); opaque only.
/// * `byte_size` — EXACT original payload length in bytes (the rendered
///   marker only carries the humanized form); opaque only.
#[pyclass(name = "DroppedRef", module = "furl_ctx._core", from_py_object)]
#[derive(Clone)]
struct PyDroppedRef {
    kind_tag: &'static str,
    hash: String,
    row_index_key: Option<String>,
    opaque_kind: Option<String>,
    byte_size: Option<usize>,
}

impl From<&RustDroppedRef> for PyDroppedRef {
    fn from(r: &RustDroppedRef) -> Self {
        match r {
            RustDroppedRef::RowDrop { hash, .. } => PyDroppedRef {
                kind_tag: "row_drop",
                hash: hash.clone(),
                row_index_key: r.row_index_key(),
                opaque_kind: None,
                byte_size: None,
            },
            RustDroppedRef::Opaque {
                hash,
                kind,
                byte_size,
            } => PyDroppedRef {
                kind_tag: "opaque",
                hash: hash.clone(),
                row_index_key: None,
                opaque_kind: Some(kind.clone()),
                byte_size: Some(*byte_size),
            },
        }
    }
}

fn py_dropped_refs(refs: &[RustDroppedRef]) -> Vec<PyDroppedRef> {
    refs.iter().map(PyDroppedRef::from).collect()
}

#[pymethods]
impl PyDroppedRef {
    #[getter]
    fn kind_tag(&self) -> &'static str {
        self.kind_tag
    }
    #[getter]
    fn hash(&self) -> &str {
        &self.hash
    }
    #[getter]
    fn row_index_key(&self) -> Option<String> {
        self.row_index_key.clone()
    }
    #[getter]
    fn opaque_kind(&self) -> Option<String> {
        self.opaque_kind.clone()
    }
    #[getter]
    fn byte_size(&self) -> Option<usize> {
        self.byte_size
    }

    fn __repr__(&self) -> String {
        // Python-style repr: options render as 'value' / None, not as
        // Rust's Some(..) Debug form.
        fn opt(v: &Option<String>) -> String {
            match v {
                Some(s) => format!("'{s}'"),
                None => "None".to_string(),
            }
        }
        match self.kind_tag {
            "row_drop" => format!(
                "DroppedRef(kind_tag='row_drop', hash='{}', row_index_key={})",
                self.hash,
                opt(&self.row_index_key)
            ),
            _ => format!(
                "DroppedRef(kind_tag='opaque', hash='{}', opaque_kind={}, byte_size={})",
                self.hash,
                opt(&self.opaque_kind),
                self.byte_size
                    .map(|n| n.to_string())
                    .unwrap_or_else(|| "None".to_string()),
            ),
        }
    }
}

// ─── CrushResult ───────────────────────────────────────────────────────────

/// Mirror of `furl_ctx.transforms.smart_crusher.CrushResult`. Read-only;
/// the Python shim builds its own dataclass instance from these
/// attributes so callers that destructure with `asdict()` keep working.
#[pyclass(name = "CrushResult", module = "furl_ctx._core")]
struct PyCrushResult {
    inner: RustCrushResult,
}

#[pymethods]
impl PyCrushResult {
    #[getter]
    fn compressed(&self) -> &str {
        &self.inner.compressed
    }
    #[getter]
    fn original(&self) -> &str {
        &self.inner.original
    }
    #[getter]
    fn was_modified(&self) -> bool {
        self.inner.was_modified
    }
    #[getter]
    fn strategy(&self) -> &str {
        &self.inner.strategy
    }

    /// Row-drop CCR hashes produced anywhere in this crush. The Python
    /// shim mirrors EACH directly into the compression_store (typed
    /// recovery) instead of scraping `<<ccr:HASH>>` out of `compressed`.
    /// Plural because `crush()` recurses and can drop rows from many
    /// sub-arrays — see `RustCrushResult::ccr_hashes()`. Empty when
    /// nothing was dropped. Returned as a fresh `list[str]` per call.
    /// Back-compat derivation over the typed `dropped` carrier —
    /// byte-identical to the retired field.
    #[getter]
    fn ccr_hashes(&self) -> Vec<String> {
        self.inner.ccr_hashes()
    }

    /// Granular per-blob row-index markers (`<<ccr:HASH#rows N_chunks>>`)
    /// paired with `ccr_hashes`, for proportional retrieval. May be
    /// shorter than `ccr_hashes` (a drop with no store configured has no
    /// row index); never longer. Returned as a fresh `list[str]` per
    /// call. Back-compat derivation over the typed `dropped` carrier —
    /// byte-identical to the retired field. Deprecated: prefer
    /// `dropped_refs` (bare `row_index_key`, no marker re-parsing).
    #[getter]
    fn row_index_markers(&self) -> Vec<String> {
        self.inner.row_index_markers()
    }

    /// Every CCR-recoverable reduction this crush shipped, typed
    /// (§4.2): row-drops AND opaque substitutions, in emission order.
    /// The Python shim mirrors each directly — no marker re-parsing.
    /// Returned as a fresh `list[DroppedRef]` per call.
    #[getter]
    fn dropped_refs(&self) -> Vec<PyDroppedRef> {
        py_dropped_refs(&self.inner.dropped)
    }

    fn __repr__(&self) -> String {
        format!(
            "CrushResult(compressed=<{} chars>, was_modified={}, strategy={:?}, \
             dropped_refs={})",
            self.inner.compressed.len(),
            self.inner.was_modified,
            self.inner.strategy,
            self.inner.dropped.len(),
        )
    }
}

// ─── SmartCrusher ──────────────────────────────────────────────────────────

/// Mirror of `furl_ctx.transforms.smart_crusher.SmartCrusher`.
///
/// Constructor accepts only `config` — Python's `relevance_config`,
/// `scorer`, and `ccr_config` parameters are handled in the Python
/// shim (the optional subsystems are disabled in Rust;
/// the shim drops those args to preserve call-site compatibility).
#[pyclass(name = "SmartCrusher", module = "furl_ctx._core")]
struct PySmartCrusher {
    inner: RustSmartCrusher,
}

#[pymethods]
impl PySmartCrusher {
    #[new]
    #[pyo3(signature = (config = None))]
    fn new(config: Option<&PySmartCrusherConfig>) -> Self {
        let cfg = config.map(|c| c.inner.clone()).unwrap_or_default();
        Self {
            inner: RustSmartCrusher::new(cfg),
        }
    }

    /// Construct WITHOUT the lossless-first compaction stage. The
    /// public `crush()` API runs the lossy path directly (still with
    /// CCR-Dropped retrieval markers populated when rows are dropped).
    /// Used by the parity fixture harness — those fixtures
    /// were recorded against the lossy-only behavior.
    #[staticmethod]
    #[pyo3(signature = (config = None))]
    fn without_compaction(config: Option<&PySmartCrusherConfig>) -> Self {
        let cfg = config.map(|c| c.inner.clone()).unwrap_or_default();
        Self {
            inner: RustSmartCrusher::without_compaction(cfg),
        }
    }

    /// Construct with the lossless-first compaction stage's formatter
    /// chosen by name: `"csv-schema"` (the `new()` default), `"json"`,
    /// or `"markdown-kv"`. Raises `ValueError` on unknown names so a
    /// misconfigured knob is visible instead of silently falling back.
    #[staticmethod]
    #[pyo3(signature = (config = None, format_name = "csv-schema"))]
    fn with_compaction_format(
        config: Option<&PySmartCrusherConfig>,
        format_name: &str,
    ) -> PyResult<Self> {
        let cfg = config.map(|c| c.inner.clone()).unwrap_or_default();
        match RustSmartCrusher::with_compaction_format(cfg, format_name) {
            Some(inner) => Ok(Self { inner }),
            None => Err(pyo3::exceptions::PyValueError::new_err(format!(
                "unknown compaction format {format_name:?}; expected one of: {}",
                furl_core::transforms::smart_crusher::compaction::CompactionStage::SUPPORTED_FORMAT_NAMES.join(", ")
            ))),
        }
    }

    /// `crush(content, query="", bias=1.0) -> CrushResult`. Argument
    /// order and keyword names mirror the Python implementation.
    ///
    /// Releases the GIL across the Rust crush call. Concurrent Python
    /// threads in the engine keep running during the JSON parse +
    /// recursive process_value + per-array compression work. `&str`
    /// inputs are copied to owned `String`s up-front since PyO3 ties
    /// their lifetime to the GIL hold.
    #[pyo3(signature = (content, query = "", bias = 1.0))]
    fn crush(
        &self,
        py: Python<'_>,
        content: &str,
        query: &str,
        bias: f64,
    ) -> PyResult<PyCrushResult> {
        let content = content.to_string();
        let query = query.to_string();
        // catch_unwind inside detach (see `panic_to_pyerr`): a panic in
        // the recursive crush becomes a catchable PyRuntimeError (COR-7).
        let inner = py
            .detach(|| {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    self.inner.crush(&content, &query, bias)
                }))
            })
            .map_err(panic_to_pyerr)?;
        Ok(PyCrushResult { inner })
    }

    /// `smart_crush_content(content, query="", bias=1.0) -> (str, bool, str)`.
    /// Mirrors Python's `_smart_crush_content` — used by
    /// `smart_crush_tool_output` convenience function and direct
    /// callers that want the tuple form. Releases the GIL across the
    /// compute (same rationale as `crush`).
    ///
    /// Deprecated (§4.2 R3/R4): prefer `smart_crush_content_typed`,
    /// which additionally returns the typed recovery refs — this shape
    /// forces the caller back onto the text scrape for recovery.
    /// Delegates to the same engine walk; rendered bytes are identical.
    #[pyo3(signature = (content, query = "", bias = 1.0))]
    fn smart_crush_content(
        &self,
        py: Python<'_>,
        content: &str,
        query: &str,
        bias: f64,
    ) -> PyResult<(String, bool, String)> {
        let content = content.to_string();
        let query = query.to_string();
        // catch_unwind inside detach (see `panic_to_pyerr`): COR-7.
        py.detach(|| {
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                self.inner.smart_crush_content(&content, &query, bias)
            }))
        })
        .map_err(panic_to_pyerr)
    }

    /// `smart_crush_content_typed(content, query="", bias=1.0) ->
    /// (str, bool, str, list[DroppedRef])` — the typed sibling of
    /// `smart_crush_content` (§4.2 R3/R4): identical first three
    /// elements (byte-identical rendering), plus every typed recovery
    /// ref the walk shipped (row-drops AND opaque substitutions, in
    /// emission order) so the Python mirror consumes refs directly
    /// instead of re-parsing rendered markers.
    #[pyo3(signature = (content, query = "", bias = 1.0))]
    fn smart_crush_content_typed(
        &self,
        py: Python<'_>,
        content: &str,
        query: &str,
        bias: f64,
    ) -> PyResult<(String, bool, String, Vec<PyDroppedRef>)> {
        let content = content.to_string();
        let query = query.to_string();
        // catch_unwind inside detach (see `panic_to_pyerr`): COR-7.
        let (crushed, was_modified, info, dropped) = py
            .detach(|| {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    self.inner.smart_crush_content_typed(&content, &query, bias)
                }))
            })
            .map_err(panic_to_pyerr)?;
        Ok((crushed, was_modified, info, py_dropped_refs(&dropped)))
    }

    /// Crush a JSON array directly and return the structured result.
    ///
    /// Input is a JSON string holding an array (`[item, item, ...]`).
    /// Returns a dict with:
    /// - `items`: JSON array string of the kept rows after compression
    /// - `ccr_hash`: 12-char hash if rows were dropped, else `None`
    /// - `dropped_summary`: `<<ccr:HASH N_rows_offloaded>>` marker
    ///   text, empty if nothing dropped
    /// - `strategy_info`: debug string describing what ran (e.g.
    ///   `"smart_sample"`, `"lossless:table"`, `"none:adaptive_at_limit"`)
    /// - `compacted`: rendered bytes when the lossless path won, else `None`
    /// - `compaction_kind`: `"table" | "buckets" | "ccr" | None`
    /// - `row_index_key`: bare `"HASH#rows"` granular-index store key of
    ///   the drop (NOT marker text), `None` when no index was written
    /// - `dropped_refs`: `list[DroppedRef]` — every typed recovery ref
    ///   the shipped render carries (the row-drop plus any opaque
    ///   substitutions baked into `compacted`), §4.2 R3/R4
    ///
    /// This surfaces `CrushArrayResult` to Python so tests and the
    /// runtime can reach the CCR hash directly (rather than parsing it
    /// out of the prompt marker).
    /// Raises `ValueError` when `items_json` is not valid JSON or not a
    /// JSON array — explicit boundary validation with a specific, clean error.
    /// A panic anywhere in the compute is caught by `catch_unwind` and
    /// converted to `PyRuntimeError` (see `panic_to_pyerr`) so it does not
    /// surface as `pyo3_runtime.PanicException` — a `BaseException` that would
    /// escape the caller's `except Exception` handlers.
    #[pyo3(signature = (items_json, query = "", bias = 1.0))]
    fn crush_array_json<'py>(
        &self,
        py: Python<'py>,
        items_json: &str,
        query: &str,
        bias: f64,
    ) -> PyResult<Bound<'py, PyDict>> {
        // GIL-release pattern: own the inputs, do all heavy compute
        // (JSON parse, crush, re-serialize) without the GIL, then
        // re-acquire to build the PyDict from the owned outputs.
        let items_json = items_json.to_string();
        let query = query.to_string();
        // catch_unwind wraps the whole GIL-free compute so a panic in the JSON
        // parse / crush / re-serialize becomes a catchable PyRuntimeError
        // (COR-7). Two Result layers to flatten: the outer is the panic
        // (`map_err(panic_to_pyerr)?`), the inner is the existing input-validation
        // `PyErr` (`?`).
        let (
            kept_json,
            ccr_hash,
            dropped_summary,
            strategy_info,
            compacted,
            compaction_kind,
            dropped_refs,
        ) = py
            .detach(|| {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    // COR-44: decline magic-key payloads before from_str so
                    // serde_json's arbitrary_precision / raw_value promotions
                    // never fire.  Raises ValueError — same class as the
                    // invalid-JSON path — so callers get a clean, catchable
                    // signal rather than silently mutated output.
                    if has_serde_private_marker(&items_json) {
                        return Err(invalid_input(
                            "items_json contains a serde_json internal key \
                             ($serde_json::private::); parsing declined to \
                             prevent silent data mutation"
                                .to_string(),
                        ));
                    }
                    let parsed: serde_json::Value = serde_json::from_str(&items_json)
                        .map_err(|e| invalid_input(format!("items_json must be JSON: {e}")))?;
                    let items = match parsed {
                        serde_json::Value::Array(a) => a,
                        other => {
                            return Err(invalid_input(format!(
                                "items_json must be a JSON array, got {}",
                                type_name(&other)
                            )))
                        }
                    };
                    let result = self.inner.crush_array(&items, &query, bias);
                    let kept_json = serde_json::to_string(&serde_json::Value::Array(result.items))
                        .map_err(|e| {
                            invalid_input(format!("failed to serialize kept items: {e}"))
                        })?;
                    Ok::<_, PyErr>((
                        kept_json,
                        result.ccr_hash,
                        result.dropped_summary,
                        result.strategy_info,
                        result.compacted,
                        result.compaction_kind,
                        result.dropped_refs,
                    ))
                }))
            })
            .map_err(panic_to_pyerr)??;
        // Bare granular-index key of the top-level row-drop (a
        // CrushArrayResult carries at most ONE RowDrop ref by
        // construction — one array, one drop).
        let row_index_key = dropped_refs.iter().find_map(|d| d.row_index_key());
        build_crush_array_dict(
            py,
            kept_json,
            ccr_hash,
            dropped_summary,
            strategy_info,
            compacted,
            compaction_kind,
            row_index_key,
            py_dropped_refs(&dropped_refs),
        )
    }

    /// Run the document-level walker on `doc_json` (JSON string) and
    /// return the compacted document as JSON.
    ///
    /// The walker recursively descends through objects, arrays, and
    /// strings; tabular sub-arrays become rendered CSV+schema strings,
    /// long opaque blobs become `<<ccr:HASH,KIND,SIZE>>` markers (with
    /// originals stashed in this crusher's CCR store, so `ccr_get`
    /// resolves them).
    ///
    /// Distinct from `crush_array_json`: this is the lossless walker
    /// pass without per-array lossy crushing — useful when the caller
    /// wants document-shape compaction (forms, configs, mixed records)
    /// rather than statistical row drop.
    /// Raises `ValueError` when `doc_json` is not valid JSON — boundary
    /// validation, not a panic (same rationale as `crush_array_json`).
    ///
    /// Deprecated (§4.2 R3/R4): prefer `compact_document_json_typed`,
    /// which additionally returns the typed opaque refs — this shape
    /// forces the caller back onto the text scrape for recovery.
    /// Delegates to the typed sibling and discards the refs, so the
    /// returned JSON is identical by construction.
    fn compact_document_json(&self, py: Python<'_>, doc_json: &str) -> PyResult<String> {
        let (compacted, _refs) = self.compact_document_json_typed(py, doc_json)?;
        Ok(compacted)
    }

    /// `compact_document_json_typed(doc_json) -> (str, list[DroppedRef])`
    /// — the typed sibling of `compact_document_json` (§4.2 R3/R4):
    /// identical compacted JSON, plus every typed opaque ref the walker
    /// shipped (both the live string substitutions and the opaque cells
    /// baked into rendered sub-tables) so the Python mirror consumes
    /// refs directly instead of re-parsing rendered markers.
    fn compact_document_json_typed(
        &self,
        py: Python<'_>,
        doc_json: &str,
    ) -> PyResult<(String, Vec<PyDroppedRef>)> {
        // Heavy: JSON parse + recursive walker + tabular compaction +
        // re-serialize. None of it touches Python; release the GIL.
        let doc_json = doc_json.to_string();
        // catch_unwind wraps the GIL-free walker compute (COR-7). Flatten the
        // panic Result (`map_err(panic_to_pyerr)?`) over the existing
        // input-validation `PyErr` (`?`).
        let (compacted, dropped) = py
            .detach(|| {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    // COR-44: decline magic-key payloads before from_str so
                    // serde_json's arbitrary_precision / raw_value promotions
                    // never fire.  Raises ValueError — same class as the
                    // invalid-JSON path.
                    if has_serde_private_marker(&doc_json) {
                        return Err(invalid_input(
                            "doc_json contains a serde_json internal key \
                             ($serde_json::private::); parsing declined to \
                             prevent silent data mutation"
                                .to_string(),
                        ));
                    }
                    let parsed: serde_json::Value = serde_json::from_str(&doc_json)
                        .map_err(|e| invalid_input(format!("doc_json must be JSON: {e}")))?;
                    let mut dc = DocumentCompactor::new();
                    if let Some(store) = self.inner.ccr_store() {
                        dc = dc.with_ccr_store(store.clone());
                    }
                    let mut sink: Vec<RustDroppedRef> = Vec::new();
                    let out = dc.compact_collecting(parsed, &mut sink);
                    let compacted = serde_json::to_string(&out).map_err(|e| {
                        invalid_input(format!("failed to serialize compacted document: {e}"))
                    })?;
                    Ok::<_, PyErr>((compacted, sink))
                }))
            })
            .map_err(panic_to_pyerr)??;
        Ok((compacted, py_dropped_refs(&dropped)))
    }

    /// Look up an original payload by CCR hash.
    ///
    /// When the lossy path drops rows, it stashes the **full original**
    /// array into the in-memory CCR store keyed by the 12-char hash
    /// embedded in the prompt's `<<ccr:HASH ...>>` marker. The
    /// MCP retrieval tool calls this to serve the
    /// dropped rows back to the LLM on demand.
    ///
    /// Returns the canonical-JSON serialization of the original
    /// `[item, item, ...]` array, or `None` if the hash is unknown,
    /// expired, or the crusher was constructed without a CCR store.
    fn ccr_get(&self, hash: &str) -> Option<String> {
        self.inner.ccr_store().and_then(|s| s.get(hash))
    }

    /// Number of entries currently held by the CCR store. `0` if no
    /// store is configured. Informational; use it from tests and
    /// telemetry, not from the retrieval hot path.
    fn ccr_len(&self) -> usize {
        self.inner.ccr_store().map(|s| s.len()).unwrap_or(0)
    }
}

// ─── ContentDetector ───────────────────────────────────────────────────────

/// Mirror of `furl_ctx.transforms.content_detector.DetectionResult`.
///
/// Field names + types match the Python dataclass exactly so the existing
/// Python `ContentRouter` (which `import`s `DetectionResult` directly) can
/// continue to read `.content_type`, `.confidence`, and `.metadata` without
/// modification.
///
/// `content_type` is exposed as the lowercase string tag (e.g.
/// `"json_array"`). The Python wrapper translates it back into the
/// `ContentType` enum so the call-site looks identical.
#[pyclass(name = "DetectionResult", module = "furl_ctx._core", from_py_object)]
#[derive(Clone)]
struct PyDetectionResult {
    inner: RustDetectionResult,
}

#[pymethods]
impl PyDetectionResult {
    #[getter]
    fn content_type(&self) -> &'static str {
        self.inner.content_type.as_str()
    }

    #[getter]
    fn confidence(&self) -> f64 {
        self.inner.confidence
    }

    /// Metadata bag — always an EMPTY fresh `dict` (ARCH-11).
    ///
    /// The only constructor of this class (`detect_content_type`)
    /// synthesizes the legacy `DetectionResult` shape with an empty
    /// metadata map; the detection chain carries no per-type metadata.
    /// The getter survives purely for field-surface parity with the
    /// Python `DetectionResult` dataclass (callers read
    /// `.content_type` / `.confidence`; none read metadata values).
    /// The former number-coercion ladder here (u64→i64→f64→None,
    /// arrays→JSON strings) was dead code describing values that could
    /// never occur.
    #[getter]
    fn metadata<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        debug_assert!(
            self.inner.metadata.is_empty(),
            "detect_content_type always constructs an empty metadata map; \
             a populated map means a new constructor exists and this \
             getter must convert values again"
        );
        Ok(PyDict::new(py))
    }

    fn __repr__(&self) -> String {
        format!(
            "DetectionResult(content_type={:?}, confidence={}, metadata=<{} keys>)",
            self.inner.content_type.as_str(),
            self.inner.confidence,
            self.inner.metadata.len()
        )
    }
}

/// Detect the type of `content`. Returns a `DetectionResult` with the
/// same field surface as Python's dataclass.
///
/// This runs through the unidiff→PlainText detection chain (the Rust
/// byte-parity port of the regex `content_detector` was removed — it
/// was never on the Rust production path). The chain returns a
/// `ContentType` only; we synthesize the legacy `DetectionResult`
/// shape here with `confidence = 1.0` (the chain doesn't surface a
/// probabilistic score) and an empty metadata bag (no production
/// caller reads metadata from this binding today — see audit notes in
/// `furl_ctx/transforms/content_router.py`).
///
/// Releases the GIL while detecting — unidiff parsing can be
/// substantial on large bodies, and freeing the GIL
/// lets other Python threads make progress in the meantime.
///
/// This is the router's hottest bridge — `_detect_content` calls it on
/// EVERY message — so it carries the COR-7 catch_unwind containment: an
/// engine-bug panic anywhere in the detection chain becomes a catchable
/// `PyRuntimeError` instead of a `pyo3_runtime.PanicException`
/// (`BaseException`) that would sail past every `except Exception` on
/// the way up and crash the host request (P0-1).
#[pyfunction]
fn detect_content_type(py: Python<'_>, content: &str) -> PyResult<PyDetectionResult> {
    let owned = content.to_string();
    // catch_unwind inside detach (see `panic_to_pyerr`): COR-7.
    let content_type = py
        .detach(move || {
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| rust_detect_chain(&owned)))
        })
        .map_err(panic_to_pyerr)?;
    Ok(PyDetectionResult {
        inner: RustDetectionResult {
            content_type,
            confidence: 1.0,
            metadata: serde_json::Map::new(),
        },
    })
}

// ─── signals: line-importance detector bridge ────────────────────────────
//
// One process-wide [`KeywordDetector`] is shared via `OnceLock` because
// the underlying aho-corasick automaton is stateless and cheap to clone
// nothing on call. The Python shim re-exports the keyword tables and a
// pair of thin functions; that's enough surface for the legacy
// `error_detection` callers without dragging the trait into Python.

use std::sync::OnceLock;

fn shared_keyword_detector() -> &'static KeywordDetector {
    static DETECTOR: OnceLock<KeywordDetector> = OnceLock::new();
    DETECTOR.get_or_init(KeywordDetector::new)
}

/// Returns `Some(ctx)` for known names and `None` otherwise — caller
/// converts to PyValueError. Avoids the pyo3 + clippy
/// `useless_conversion` false positive that fires when `?` propagates a
/// `PyResult<_>` through another `PyResult<_>` (first seen under pyo3
/// 0.22; shape kept under the pinned 0.29).
fn ctx_from_str(name: &str) -> Option<ImportanceContext> {
    match name {
        "text" => Some(ImportanceContext::Text),
        "search" => Some(ImportanceContext::Search),
        "diff" => Some(ImportanceContext::Diff),
        "log" => Some(ImportanceContext::Log),
        _ => None,
    }
}

fn category_to_str(cat: ImportanceCategory) -> &'static str {
    match cat {
        ImportanceCategory::Error => "error",
        ImportanceCategory::Warning => "warning",
        ImportanceCategory::Importance => "importance",
        ImportanceCategory::Security => "security",
        ImportanceCategory::Markdown => "markdown",
    }
}

/// Score a line against the default Furl keyword detector.
///
/// Returns `Some((category | None, priority, confidence))` for known
/// contexts (`text|search|diff|log`) and `None` for an unknown context
/// — the Python shim translates `None` into `ValueError` for the
/// caller. (Historical note: this used to return a bare `Option` to
/// dodge a pyo3-0.22-era clippy `useless_conversion` false positive; the
/// P0-1 panic-containment audit wrapped it in `PyResult` for the COR-7
/// catch_unwind, and the lint no longer fires on this shape.)
///
/// No `detach` here: this is called per line in tight Python
/// loops and the compute is a single aho-corasick scan — releasing and
/// reacquiring the GIL per call would cost more than the scan itself.
#[pyfunction]
#[pyo3(signature = (line, context = "text"))]
fn score_line(line: &str, context: &str) -> PyResult<Option<(Option<&'static str>, f32, f32)>> {
    // catch_unwind → PyRuntimeError (see `panic_to_pyerr`): COR-7 (P0-1 audit).
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let ctx = ctx_from_str(context)?;
        let signal = shared_keyword_detector().score(line, ctx);
        Some((
            signal.category.map(category_to_str),
            signal.priority,
            signal.confidence,
        ))
    }))
    .map_err(panic_to_pyerr)
}

/// Lax substring check: does `text` contain any error indicator? Mirrors
/// Python `error_detection.content_has_error_indicators`. Same
/// no-`detach` rationale as `score_line`.
#[pyfunction]
fn content_has_error_indicators(text: &str) -> PyResult<bool> {
    // catch_unwind → PyRuntimeError (see `panic_to_pyerr`): COR-7 (P0-1 audit).
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        shared_keyword_detector().contains_error_indicator(text)
    }))
    .map_err(panic_to_pyerr)
}

/// Snapshot of the default keyword sets, exposed as a dict so the Python
/// shim can recompile the legacy `re.Pattern` objects without
/// re-declaring keyword data on the Python side. Uses `.unwrap()` on
/// `set_item` because keys are static str literals and values are
/// `Vec<&'static str>`, which can't fail — and avoids the pyo3
/// `useless_conversion` clippy false positive (first seen under 0.22;
/// kept defensively under the pinned 0.29).
#[pyfunction]
fn keyword_registry_snapshot(py: Python<'_>) -> Py<PyDict> {
    let registry = KeywordRegistry::default_set();
    let dict = PyDict::new(py);
    for (key, words) in registry.as_map() {
        dict.set_item(key, words).unwrap();
    }
    dict.unbind()
}

// ─── search_compressor bridge (Phase 3e.2) ────────────────────────────
//
// Mirrors `furl_ctx.transforms.search_compressor.SearchCompressor` so the
// Python shim can swap in via PyO3. The Rust implementation consumes the
// `signals::LineImportanceDetector` trait for priority scoring (instead of
// the regex registry the Python original used) and fixes the Windows-path
// + dashes-in-filename parser bugs.
//
// CCR persistence is exposed via a callback hook because the Python
// `CompressionStore` already lives Python-side. The Rust crate holds no
// long-lived store reference; instead the caller passes the dict back
// through the result and the Python shim writes it to the existing
// store. This avoids dragging a second CCR backend into Rust before the
// Phase 3g pipeline formalization owns CCR end-to-end.

#[pyclass(
    name = "SearchCompressorConfig",
    module = "furl_ctx._core",
    from_py_object
)]
#[derive(Clone)]
struct PySearchCompressorConfig {
    inner: RustSearchConfig,
}

#[pymethods]
impl PySearchCompressorConfig {
    #[new]
    #[pyo3(signature = (
        max_matches_per_file = 5,
        always_keep_first = true,
        always_keep_last = true,
        max_total_matches = 30,
        max_files = 15,
        context_keywords = vec![],
        boost_errors = true,
        enable_ccr = true,
        min_matches_for_ccr = 10,
        min_compression_ratio_for_ccr = 0.8,
        group_by_file = false,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        max_matches_per_file: usize,
        always_keep_first: bool,
        always_keep_last: bool,
        max_total_matches: usize,
        max_files: usize,
        context_keywords: Vec<String>,
        boost_errors: bool,
        enable_ccr: bool,
        min_matches_for_ccr: usize,
        min_compression_ratio_for_ccr: f64,
        group_by_file: bool,
    ) -> Self {
        Self {
            inner: RustSearchConfig {
                max_matches_per_file,
                always_keep_first,
                always_keep_last,
                max_total_matches,
                max_files,
                context_keywords,
                boost_errors,
                enable_ccr,
                min_matches_for_ccr,
                min_compression_ratio_for_ccr,
                group_by_file,
            },
        }
    }
}

#[pyclass(name = "SearchCompressionResult", module = "furl_ctx._core")]
struct PySearchCompressionResult {
    inner: RustSearchResult,
    stats: RustSearchStats,
}

#[pymethods]
impl PySearchCompressionResult {
    #[getter]
    fn compressed(&self) -> &str {
        &self.inner.compressed
    }
    #[getter]
    fn original(&self) -> &str {
        &self.inner.original
    }
    #[getter]
    fn original_match_count(&self) -> usize {
        self.inner.original_match_count
    }
    #[getter]
    fn compressed_match_count(&self) -> usize {
        self.inner.compressed_match_count
    }
    #[getter]
    fn files_affected(&self) -> usize {
        self.inner.files_affected
    }
    #[getter]
    fn compression_ratio(&self) -> f64 {
        self.inner.compression_ratio
    }
    #[getter]
    fn cache_key(&self) -> Option<&str> {
        self.inner.cache_key.as_deref()
    }
    #[getter]
    fn summaries<'py>(&self, py: Python<'py>) -> Bound<'py, PyDict> {
        let dict = PyDict::new(py);
        for (k, v) in &self.inner.summaries {
            dict.set_item(k, v).unwrap();
        }
        dict
    }
    /// Sidecar stats — same shape every Rust transform uses for OTel.
    #[getter]
    fn lines_unparsed(&self) -> usize {
        self.stats.lines_unparsed
    }
    #[getter]
    fn files_dropped(&self) -> usize {
        self.stats.files_dropped
    }
    #[getter]
    fn ccr_emitted(&self) -> bool {
        self.stats.ccr_emitted
    }
    #[getter]
    fn ccr_skip_reason(&self) -> Option<&str> {
        self.stats.ccr_skip_reason
    }
}

#[pyclass(name = "SearchCompressor", module = "furl_ctx._core")]
struct PySearchCompressor {
    inner: RustSearchCompressor,
}

#[pymethods]
impl PySearchCompressor {
    #[new]
    #[pyo3(signature = (config = None))]
    fn new(config: Option<PySearchCompressorConfig>) -> Self {
        let cfg = config.map(|c| c.inner).unwrap_or_default();
        Self {
            inner: RustSearchCompressor::new(cfg),
        }
    }

    /// Compress `content`. CCR persistence is the caller's responsibility
    /// — the Rust side never writes to the store. If the result needs a
    /// CCR marker, `cache_key` will be populated and the Python shim
    /// writes the original to the existing `CompressionStore`. This
    /// matches Python's existing CCR plumbing and avoids dragging a
    /// second backend into the Rust crate.
    ///
    /// PERF-8: `compress_key_only` computes the key + marker with NO
    /// store write — the old shape synthesized a throwaway 1000-cap
    /// `InMemoryCcrStore` per call and had the core write the FULL
    /// original into it, dropped on return. `cache_key` is byte-equal
    /// (pinned in furl-core); the Python shim's re-persist is (and
    /// always was) the real backing.
    #[pyo3(signature = (content, context = "", bias = 1.0))]
    fn compress(
        &self,
        py: Python<'_>,
        content: &str,
        context: &str,
        bias: f64,
    ) -> PyResult<PySearchCompressionResult> {
        let owned = content.to_string();
        let owned_ctx = context.to_string();
        // catch_unwind inside detach (see `panic_to_pyerr`): COR-7.
        let (result, stats) = py
            .detach(move || {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    let (r, s) = self.inner.compress_key_only(&owned, &owned_ctx, bias);
                    (r, s)
                }))
            })
            .map_err(panic_to_pyerr)?;
        Ok(PySearchCompressionResult {
            inner: result,
            stats,
        })
    }
}

// ─── log_compressor bridge (Phase 3e.5) ───────────────────────────────
//
// Mirrors `furl_ctx.transforms.log_compressor.LogCompressor`. Same CCR
// pattern as search_compressor: Rust emits a `cache_key`, Python shim
// writes the original to the production `CompressionStore`.

#[pyclass(
    name = "LogCompressorConfig",
    module = "furl_ctx._core",
    from_py_object
)]
#[derive(Clone)]
struct PyLogCompressorConfig {
    inner: RustLogConfig,
}

#[pymethods]
impl PyLogCompressorConfig {
    #[new]
    #[pyo3(signature = (
        max_errors = 10,
        error_context_lines = 3,
        keep_first_error = true,
        keep_last_error = true,
        max_stack_traces = 3,
        stack_trace_max_lines = 20,
        max_warnings = 5,
        dedupe_warnings = true,
        keep_summary_lines = true,
        max_total_lines = 100,
        enable_ccr = true,
        min_lines_for_ccr = 50,
        min_compression_ratio_for_ccr = 0.5,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        max_errors: usize,
        error_context_lines: usize,
        keep_first_error: bool,
        keep_last_error: bool,
        max_stack_traces: usize,
        stack_trace_max_lines: usize,
        max_warnings: usize,
        dedupe_warnings: bool,
        keep_summary_lines: bool,
        max_total_lines: usize,
        enable_ccr: bool,
        min_lines_for_ccr: usize,
        min_compression_ratio_for_ccr: f64,
    ) -> Self {
        Self {
            inner: RustLogConfig {
                max_errors,
                error_context_lines,
                keep_first_error,
                keep_last_error,
                max_stack_traces,
                stack_trace_max_lines,
                max_warnings,
                dedupe_warnings,
                keep_summary_lines,
                max_total_lines,
                enable_ccr,
                min_lines_for_ccr,
                min_compression_ratio_for_ccr,
            },
        }
    }
}

#[pyclass(name = "LogCompressionResult", module = "furl_ctx._core")]
struct PyLogCompressionResult {
    inner: RustLogResult,
    stats: RustLogStats,
}

#[pymethods]
impl PyLogCompressionResult {
    #[getter]
    fn compressed(&self) -> &str {
        &self.inner.compressed
    }
    #[getter]
    fn original(&self) -> &str {
        &self.inner.original
    }
    #[getter]
    fn original_line_count(&self) -> usize {
        self.inner.original_line_count
    }
    #[getter]
    fn compressed_line_count(&self) -> usize {
        self.inner.compressed_line_count
    }
    #[getter]
    fn format_detected(&self) -> &'static str {
        self.inner.format_detected.as_str()
    }
    #[getter]
    fn compression_ratio(&self) -> f64 {
        self.inner.compression_ratio
    }
    #[getter]
    fn cache_key(&self) -> Option<&str> {
        self.inner.cache_key.as_deref()
    }
    #[getter]
    fn stats<'py>(&self, py: Python<'py>) -> Bound<'py, PyDict> {
        let dict = PyDict::new(py);
        for (k, v) in &self.inner.stats {
            dict.set_item(k, v).unwrap();
        }
        dict
    }
    // Sidecar diagnostics
    #[getter]
    fn stack_traces_seen(&self) -> usize {
        self.stats.stack_traces_seen
    }
    #[getter]
    fn stack_traces_kept(&self) -> usize {
        self.stats.stack_traces_kept
    }
    #[getter]
    fn warnings_dropped_by_dedupe(&self) -> usize {
        self.stats.warnings_dropped_by_dedupe
    }
    #[getter]
    fn ccr_emitted(&self) -> bool {
        self.stats.ccr_emitted
    }
    #[getter]
    fn ccr_skip_reason(&self) -> Option<&str> {
        self.stats.ccr_skip_reason
    }
}

#[pyclass(name = "LogCompressor", module = "furl_ctx._core")]
struct PyLogCompressor {
    inner: RustLogCompressor,
}

#[pymethods]
impl PyLogCompressor {
    #[new]
    #[pyo3(signature = (config = None))]
    fn new(config: Option<PyLogCompressorConfig>) -> Self {
        let cfg = config.map(|c| c.inner).unwrap_or_default();
        Self {
            inner: RustLogCompressor::new(cfg),
        }
    }

    /// Compress `content`. Same CCR pattern as search_compressor: Rust
    /// emits the `cache_key`; the Python shim is responsible for
    /// writing the original to the production `CompressionStore`.
    ///
    /// PERF-8: key-only mode — no throwaway store, no dead write of the
    /// full original. `cache_key` bytes are unchanged (pinned in
    /// furl-core).
    #[pyo3(signature = (content, bias = 1.0))]
    fn compress(
        &self,
        py: Python<'_>,
        content: &str,
        bias: f64,
    ) -> PyResult<PyLogCompressionResult> {
        let owned = content.to_string();
        // catch_unwind inside detach (see `panic_to_pyerr`): COR-7.
        let (result, stats) = py
            .detach(move || {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    let (r, s) = self.inner.compress_key_only(&owned, bias);
                    (r, s)
                }))
            })
            .map_err(panic_to_pyerr)?;
        Ok(PyLogCompressionResult {
            inner: result,
            stats,
        })
    }
}

// ─── text_crusher bridge (Engine P2-11) ───────────────────────────────
//
// Mirrors `furl_ctx.transforms.text_crusher.TextCrusher`. Same CCR
// pattern as the log/search bridges: Rust emits a `cache_key` after
// backing the crush in a per-call in-memory store; the Python shim
// re-persists the original into the production `CompressionStore` and
// VETOES the compression (serves the original) if that write fails —
// the marker never ships dangling.

#[pyclass(name = "TextCrusherConfig", module = "furl_ctx._core", from_py_object)]
#[derive(Clone)]
struct PyTextCrusherConfig {
    inner: RustTextCrusherConfig,
}

#[pymethods]
impl PyTextCrusherConfig {
    #[new]
    #[pyo3(signature = (
        target_ratio = 0.35,
        min_chars = 600,
        min_segments = 15,
        min_kept_segments = 5,
        always_keep_first = 2,
        always_keep_last = 2,
        shingle_size = 4,
        dedup_threshold = 0.9,
        max_pairwise_dedup_segments = 2000,
        enable_ccr = true,
        max_shippable_ratio = 0.9,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        target_ratio: f64,
        min_chars: usize,
        min_segments: usize,
        min_kept_segments: usize,
        always_keep_first: usize,
        always_keep_last: usize,
        shingle_size: usize,
        dedup_threshold: f64,
        max_pairwise_dedup_segments: usize,
        enable_ccr: bool,
        max_shippable_ratio: f64,
    ) -> Self {
        Self {
            inner: RustTextCrusherConfig {
                target_ratio,
                min_chars,
                min_segments,
                min_kept_segments,
                always_keep_first,
                always_keep_last,
                shingle_size,
                dedup_threshold,
                max_pairwise_dedup_segments,
                enable_ccr,
                max_shippable_ratio,
            },
        }
    }
}

#[pyclass(name = "TextCrushResult", module = "furl_ctx._core")]
struct PyTextCrushResult {
    inner: RustTextCrushResult,
    stats: RustTextCrusherStats,
}

#[pymethods]
impl PyTextCrushResult {
    #[getter]
    fn compressed(&self) -> &str {
        &self.inner.compressed
    }
    #[getter]
    fn original(&self) -> &str {
        &self.inner.original
    }
    #[getter]
    fn original_segment_count(&self) -> usize {
        self.inner.original_segment_count
    }
    #[getter]
    fn compressed_segment_count(&self) -> usize {
        self.inner.compressed_segment_count
    }
    #[getter]
    fn compression_ratio(&self) -> f64 {
        self.inner.compression_ratio
    }
    #[getter]
    fn cache_key(&self) -> Option<&str> {
        self.inner.cache_key.as_deref()
    }
    // Sidecar diagnostics — same shape every Rust transform uses.
    #[getter]
    fn segments_total(&self) -> usize {
        self.stats.segments_total
    }
    #[getter]
    fn segments_kept(&self) -> usize {
        self.stats.segments_kept
    }
    #[getter]
    fn segments_dropped_by_dedup(&self) -> usize {
        self.stats.segments_dropped_by_dedup
    }
    #[getter]
    fn segments_dropped_by_budget(&self) -> usize {
        self.stats.segments_dropped_by_budget
    }
    #[getter]
    fn protected_tag_blocks(&self) -> usize {
        self.stats.protected_tag_blocks
    }
    #[getter]
    fn mandatory_keeps(&self) -> usize {
        self.stats.mandatory_keeps
    }
    #[getter]
    fn ccr_emitted(&self) -> bool {
        self.stats.ccr_emitted
    }
    #[getter]
    fn ccr_skip_reason(&self) -> Option<&str> {
        self.stats.ccr_skip_reason
    }
    #[getter]
    fn passthrough_reason(&self) -> Option<&str> {
        self.stats.passthrough_reason
    }
}

#[pyclass(name = "TextCrusher", module = "furl_ctx._core")]
struct PyTextCrusher {
    inner: RustTextCrusher,
}

#[pymethods]
impl PyTextCrusher {
    #[new]
    #[pyo3(signature = (config = None))]
    fn new(config: Option<PyTextCrusherConfig>) -> Self {
        let cfg = config.map(|c| c.inner).unwrap_or_default();
        Self {
            inner: RustTextCrusher::new(cfg),
        }
    }

    /// Compress `content`. Same CCR pattern as the log/search bridges:
    /// the Rust side backs the crush in a per-call in-memory store and
    /// emits `cache_key`; the Python shim persists the original to the
    /// production `CompressionStore` and vetoes (serves the original)
    /// on write failure.
    #[pyo3(signature = (content, context = "", bias = 1.0))]
    fn compress(
        &self,
        py: Python<'_>,
        content: &str,
        context: &str,
        bias: f64,
    ) -> PyResult<PyTextCrushResult> {
        let owned = content.to_string();
        let owned_ctx = context.to_string();
        // catch_unwind inside detach (see `panic_to_pyerr`): COR-7.
        let (result, stats) = py
            .detach(move || {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    let store = furl_core::ccr::InMemoryCcrStore::new();
                    let (r, s) =
                        self.inner
                            .compress_with_store(&owned, &owned_ctx, bias, Some(&store));
                    (r, s)
                }))
            })
            .map_err(panic_to_pyerr)?;
        Ok(PyTextCrushResult {
            inner: result,
            stats,
        })
    }
}

// ─── tag_protector bridge (restored in Engine P2-11) ──────────────────
//
// Mirrors `furl_ctx.transforms.tag_protector.{protect_tags,restore_tags,
// is_html_tag,KNOWN_HTML_TAGS}`. The Rust walker is single-pass and
// fixes five real bugs the Python original carried (see crate-level
// docs in `tag_protector.rs`); `restore_tags` additionally carries the
// PERF-15 single-scan fix. The GIL is released during the walk because
// the algorithm holds no Python references.

/// Replace custom workflow tags in `text` with opaque placeholders so
/// downstream lossy compressors can't accidentally drop them.
///
/// Returns `(cleaned_text, blocks)` where `blocks` is a list of
/// `(placeholder, original)` tuples for `restore_tags`.
#[pyfunction]
#[pyo3(signature = (text, compress_tagged_content = false))]
fn protect_tags(
    py: Python<'_>,
    text: &str,
    compress_tagged_content: bool,
) -> PyResult<(String, Vec<(String, String)>)> {
    let owned = text.to_string();
    // catch_unwind inside detach (see `panic_to_pyerr`): COR-7.
    py.detach(move || {
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let (cleaned, blocks, _stats) = rust_protect_tags(&owned, compress_tagged_content);
            (cleaned, blocks)
        }))
    })
    .map_err(panic_to_pyerr)
}

/// Splice protected blocks back into `text`. Missing placeholders are
/// DISCARDED (Hotfix-A9 — no orphan-tag append); each placeholder
/// substitutes at most once (PERF-15 single left-to-right scan).
#[pyfunction]
fn restore_tags(py: Python<'_>, text: &str, blocks: Vec<(String, String)>) -> PyResult<String> {
    let owned = text.to_string();
    // catch_unwind inside detach (see `panic_to_pyerr`): COR-7.
    py.detach(move || {
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            rust_restore_tags(&owned, &blocks)
        }))
    })
    .map_err(panic_to_pyerr)
}

/// Case-insensitive HTML5 tag check. The Python shim uses this to
/// preserve the legacy private `_is_html_tag` import surface for tests.
#[pyfunction]
fn is_html_tag(name: &str) -> bool {
    rust_is_known_html_tag(name)
}

/// Return the canonical HTML5 tag name list. The Python shim
/// reconstructs `KNOWN_HTML_TAGS` from this so callers that import the
/// frozenset continue to work without re-declaring the set in two
/// languages.
#[pyfunction]
fn known_html_tag_names() -> Vec<&'static str> {
    rust_known_html_tag_names().to_vec()
}

// ─── Module init ───────────────────────────────────────────────────────────

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(hello, m)?)?;
    m.add_class::<PyDiffCompressorConfig>()?;
    m.add_class::<PyDiffCompressionResult>()?;
    m.add_class::<PyDiffCompressorStats>()?;
    m.add_class::<PyDiffCompressor>()?;
    m.add_class::<PySearchCompressorConfig>()?;
    m.add_class::<PySearchCompressionResult>()?;
    m.add_class::<PySearchCompressor>()?;
    m.add_class::<PySmartCrusherConfig>()?;
    m.add_class::<PyDroppedRef>()?;
    m.add_class::<PyCrushResult>()?;
    m.add_class::<PySmartCrusher>()?;
    m.add_class::<PyDetectionResult>()?;
    m.add_class::<PyLogCompressorConfig>()?;
    m.add_class::<PyLogCompressionResult>()?;
    m.add_class::<PyLogCompressor>()?;
    m.add_class::<PyTextCrusherConfig>()?;
    m.add_class::<PyTextCrushResult>()?;
    m.add_class::<PyTextCrusher>()?;
    m.add_function(wrap_pyfunction!(protect_tags, m)?)?;
    m.add_function(wrap_pyfunction!(restore_tags, m)?)?;
    m.add_function(wrap_pyfunction!(is_html_tag, m)?)?;
    m.add_function(wrap_pyfunction!(known_html_tag_names, m)?)?;
    m.add_function(wrap_pyfunction!(detect_content_type, m)?)?;
    m.add_function(wrap_pyfunction!(score_line, m)?)?;
    m.add_function(wrap_pyfunction!(content_has_error_indicators, m)?)?;
    m.add_function(wrap_pyfunction!(keyword_registry_snapshot, m)?)?;
    Ok(())
}
