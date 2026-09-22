//! PyO3 bindings for furl-core.

use std::any::Any;

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
use furl_core::transforms::{
    detect as rust_detect_chain, DetectionResult as RustDetectionResult, DiffCompressionResult,
    DiffCompressor, DiffCompressorConfig, LogCompressionResult as RustLogResult,
    LogCompressor as RustLogCompressor, LogCompressorConfig as RustLogConfig,
    LogCompressorStats as RustLogStats, SearchCompressionResult as RustSearchResult,
    SearchCompressor as RustSearchCompressor, SearchCompressorConfig as RustSearchConfig,
    SearchCompressorStats as RustSearchStats, TextCrushResult as RustTextCrushResult,
    TextCrusher as RustTextCrusher, TextCrusherConfig as RustTextCrusherConfig,
    TextCrusherStats as RustTextCrusherStats,
};
use pyo3::exceptions::PyDeprecationWarning;
use pyo3::prelude::*;
use pyo3::types::PyDict;

/// Identity stub used by the Python smoke test to verify linkage.
#[pyfunction]
fn hello() -> &'static str {
    furl_core::hello()
}

/// Build the `ValueError` raised for invalid caller input at the FFI boundary. Centralized so
/// every binding reports bad input the same way (and none of them panic — see `crush_array_json`).
fn invalid_input(msg: String) -> PyErr {
    pyo3::exceptions::PyValueError::new_err(msg)
}

/// Convert a caught `catch_unwind` panic payload into a `PyRuntimeError`. A bare Rust panic crossing the PyO3 boundary surfaces as
/// `pyo3_runtime.PanicException`, a `BaseException` that escapes the caller's `except Exception` (and Python `compress()`'s fail-open).
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

/// Build the dict returned by `SmartCrusher.crush_array_json`.
#[allow(clippy::too_many_arguments)]
fn build_crush_array_dict<'py>(
    py: Python<'py>,
    kept_json: String,
    ccr_hash: Option<String>,
    dropped_summary: String,
    strategy_info: String,
    compacted: Option<String>,
    compaction_kind: Option<&'static str>,
    dropped_refs: Vec<PyDroppedRef>,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("items", kept_json).unwrap();
    dict.set_item("ccr_hash", ccr_hash).unwrap();
    dict.set_item("dropped_summary", dropped_summary).unwrap();
    dict.set_item("strategy_info", strategy_info).unwrap();
    dict.set_item("compacted", compacted).unwrap();
    dict.set_item("compaction_kind", compaction_kind).unwrap();
    dict.set_item("dropped_refs", dropped_refs)?;
    Ok(dict)
}

// ─── DiffCompressorConfig ──────────────────────────────────────────────────

/// Mirror of `furl_ctx.transforms.diff_compressor.DiffCompressorConfig`. Defaults match Python; constructor
/// accepts every field as a kwarg with the same name and type as the Python dataclass for drop-in compatibility.
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
             enable_ccr={}, \
             min_lines_for_ccr={}, min_compression_ratio_for_ccr={}, drop_noise_hunks={})",
            self.inner.max_context_lines,
            self.inner.max_hunks_per_file,
            self.inner.max_files,
            self.inner.enable_ccr,
            self.inner.min_lines_for_ccr,
            self.inner.min_compression_ratio_for_ccr,
            self.inner.drop_noise_hunks,
        )
    }
}

// ─── DiffCompressionResult ─────────────────────────────────────────────────

/// Mirror of `furl_ctx.transforms.diff_compressor.DiffCompressionResult`. Read-only on the Python side: ContentRouter
/// consumes fields, doesn't mutate. `compression_ratio` and `tokens_saved_estimate` are exposed as methods (not `@property`)
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

    /// Mirror of Python `@property compression_ratio`. Returns `compressed_line_count / original_line_count` (1.0 if input was empty).
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

// ─── DiffCompressor ────────────────────────────────────────────────────────

/// Mirror of `furl_ctx.transforms.diff_compressor.DiffCompressor`. The Python adapter wraps
/// this in `RustBackedDiffCompressor` so `ContentRouter` can swap backends transparently.
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

    /// `compress(content: str, context: str = "") -> DiffCompressionResult`. Argument order and keyword names match the Python implementation. Releases the GIL
    /// across the Rust compress call so concurrent Python threads (uvicorn workers, asyncio tasks) can keep running while we hash + parse + filter the diff.
    #[pyo3(signature = (content, context = ""))]
    fn compress(
        &self,
        py: Python<'_>,
        content: &str,
        context: &str,
    ) -> PyResult<PyDiffCompressionResult> {
        let content = content.to_string();
        let context = context.to_string();
        // catch_unwind inside detach: keep the GIL released during the Rust compute, catch any panic there, convert after
        // re-acquiring so an engine bug becomes a catchable PyRuntimeError instead of a BaseException that crashes the host (COR-7).
        let inner = py
            .detach(|| {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    self.inner.compress(&content, &context)
                }))
            })
            .map_err(panic_to_pyerr)?;
        Ok(PyDiffCompressionResult { inner })
    }
}

// ─── SmartCrusherConfig ────────────────────────────────────────────────────

/// Mirror of `furl_ctx.transforms.smart_crusher.SmartCrusherConfig`. The constructor accepts every dataclass field
/// as a kwarg with the same name and type, so the Python shim passes `SmartCrusherConfig(**asdict(py_cfg), ...)`.
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
        advertise_retrieval_tool = None,
        routing_policy = "min-tokens",
        lossless_only = false,
        enable_ccr_marker = None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
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
        advertise_retrieval_tool: Option<bool>,
        routing_policy: &str,
        lossless_only: bool,
        enable_ccr_marker: Option<bool>,
    ) -> PyResult<Self> {
        // `advertise_retrieval_tool` (renamed from `enable_ccr_marker`) gates ONLY the retrieval-tool advertisement — never the recovery pointer or store write.
        let advertise_retrieval_tool = match (advertise_retrieval_tool, enable_ccr_marker) {
            (Some(_), Some(_)) => {
                return Err(invalid_input(
                    "pass only one of advertise_retrieval_tool or its deprecated \
                     alias enable_ccr_marker, not both"
                        .to_string(),
                ));
            }
            (Some(value), None) => value,
            (None, Some(value)) => {
                PyErr::warn(
                    py,
                    py.get_type::<PyDeprecationWarning>().as_any(),
                    c"enable_ccr_marker is deprecated, use advertise_retrieval_tool; \
                      removed in the next minor release",
                    1,
                )?;
                value
            }
            (None, None) => true,
        };
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
                advertise_retrieval_tool,
                routing_policy,
                lossless_only,
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
    fn advertise_retrieval_tool(&self) -> bool {
        self.inner.advertise_retrieval_tool
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

/// Each shipped reduction crosses FFI as a typed ref: `kind` (`row_drop`/`opaque`), CCR `hash`, optional opaque wire kind, and
/// original `byte_size`. The hash is the rendered recovery key; opaque kind identifies base64/string/html/custom substitutions.
#[pyclass(name = "DroppedRef", module = "furl_ctx._core", from_py_object)]
#[derive(Clone)]
struct PyDroppedRef {
    kind_tag: &'static str,
    hash: String,
    opaque_kind: Option<String>,
    byte_size: Option<usize>,
}

impl From<&RustDroppedRef> for PyDroppedRef {
    fn from(r: &RustDroppedRef) -> Self {
        match r {
            RustDroppedRef::RowDrop { hash, .. } => PyDroppedRef {
                kind_tag: "row_drop",
                hash: hash.clone(),
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
            "row_drop" => format!("DroppedRef(kind_tag='row_drop', hash='{}')", self.hash),
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

/// Mirror of `furl_ctx.transforms.smart_crusher.CrushResult`. Read-only; the Python shim builds its own
/// dataclass instance from these attributes so callers that destructure with `asdict()` keep working.
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

    /// Row-drop CCR hashes produced anywhere in this crush. The Python shim mirrors EACH directly into
    /// the compression_store (typed recovery) instead of scraping `<<ccr:HASH>>` out of `compressed`.
    #[getter]
    fn ccr_hashes(&self) -> Vec<String> {
        self.inner.ccr_hashes()
    }

    /// Every CCR-recoverable reduction this crush shipped, typed (§4.2): row-drops AND opaque
    /// substitutions, in emission order. The Python shim mirrors each directly — no marker re-parsing.
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

/// Mirror of `furl_ctx.transforms.smart_crusher.SmartCrusher`. Constructor accepts only `config` Python's `relevance_config`, `scorer`, and `ccr_config`
/// parameters are handled in the Python shim (the optional subsystems are disabled in Rust; the shim drops those args to preserve call-site compatibility).
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

    /// Construct WITHOUT the lossless-first compaction stage.
    #[staticmethod]
    #[pyo3(signature = (config = None))]
    fn without_compaction(config: Option<&PySmartCrusherConfig>) -> Self {
        let cfg = config.map(|c| c.inner.clone()).unwrap_or_default();
        Self {
            inner: RustSmartCrusher::without_compaction(cfg),
        }
    }

    /// Construct with the lossless-first compaction stage's formatter chosen by name: `"csv-schema"` (the `new()` default), `"json"`, or `"markdown-kv"`.
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

    /// `crush(content, query="", bias=1.0) -> CrushResult`. Argument order and keyword names mirror the Python implementation. Concurrent
    /// Python threads in the engine keep running during the JSON parse + recursive process_value + per-array compression work.
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

    /// `smart_crush_content_typed(content, query="", bias=1.0) -> (str, bool, str, list[DroppedRef])`.
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

    /// Expose structured array compression to Python: kept items, recovery hash/marker, strategy, optional compacted render,
    /// kind, and typed dropped refs. Invalid/non-array JSON raises `ValueError`; Rust panics become catchable `PyRuntimeError`.
    #[pyo3(signature = (items_json, query = "", bias = 1.0))]
    fn crush_array_json<'py>(
        &self,
        py: Python<'py>,
        items_json: &str,
        query: &str,
        bias: f64,
    ) -> PyResult<Bound<'py, PyDict>> {
        // GIL-release pattern: own the inputs, do all heavy compute (JSON parse, crush,
        // re-serialize) without the GIL, then re-acquire to build the PyDict from the owned outputs.
        let items_json = items_json.to_string();
        let query = query.to_string();
        // catch_unwind wraps the whole GIL-free compute so a panic in the JSON parse / crush / re-serialize becomes a catchable PyRuntimeError (COR-7).
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
                    // COR-44: decline magic-key payloads before from_str so serde_json's arbitrary_precision / raw_value promotions never fire.
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
        build_crush_array_dict(
            py,
            kept_json,
            ccr_hash,
            dropped_summary,
            strategy_info,
            compacted,
            compaction_kind,
            py_dropped_refs(&dropped_refs),
        )
    }

    /// `compact_document_json_typed(doc_json) -> (str, list[DroppedRef])` the document-level walker on `doc_json` (JSON string). tabular sub-arrays become rendered CSV+schema
    /// strings, long opaque blobs become `<<ccr:HASH,KIND,SIZE>>` markers (with originals stashed in this crusher's CCR store Raises `ValueError` when `doc_json` is not valid JSON
    fn compact_document_json_typed(
        &self,
        py: Python<'_>,
        doc_json: &str,
    ) -> PyResult<(String, Vec<PyDroppedRef>)> {
        // Heavy: JSON parse + recursive walker + tabular compaction +
        // re-serialize. None of it touches Python; release the GIL.
        let doc_json = doc_json.to_string();
        // catch_unwind wraps the GIL-free walker compute (COR-7). Flatten the panic Result
        // (`map_err(panic_to_pyerr)?`) over the existing input-validation `PyErr` (`?`).
        let (compacted, dropped) = py
            .detach(|| {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    // COR-44: decline magic-key payloads before from_str so serde_json's arbitrary_precision /
                    // raw_value promotions never fire. Raises ValueError — same class as the invalid-JSON path.
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

    /// Look up an original payload by CCR hash. When the lossy path drops rows, it stashes the **full original** array
    /// into the in-memory CCR store keyed by the 12-char hash embedded in the prompt's `<<ccr:HASH ...>>` marker.
    fn ccr_get(&self, hash: &str) -> Option<String> {
        self.inner.ccr_store().and_then(|s| s.get(hash))
    }

    /// Number of entries currently held by the CCR store. `0` if no store is configured.
    /// Informational; use it from tests and telemetry, not from the retrieval hot path.
    fn ccr_len(&self) -> usize {
        self.inner.ccr_store().map(|s| s.len()).unwrap_or(0)
    }
}

// ─── ContentDetector ───────────────────────────────────────────────────────

/// Mirror of `furl_ctx.transforms.content_detector.DetectionResult`. Field names + types match the Python dataclass exactly so the existing Python
/// `ContentRouter` (which `import`s `DetectionResult` directly) can continue to read `.content_type`, `.confidence`, and `.metadata` without modification.
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

    /// Metadata bag — always an EMPTY fresh `dict` (ARCH-11). The only constructor of this class (`detect_content_type`)
    /// synthesizes the legacy `DetectionResult` shape with an empty metadata map; the detection chain carries no per-type metadata.
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

/// Detect content through the unified-diff→plain-text chain and return the legacy result shape with
/// confidence 1.0. Release the GIL during detection and convert Rust panics to `PyRuntimeError`.
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

// ─── signals: line-importance detector bridge ──────────────────────────── One process-wide [`KeywordDetector`] is
// shared via `OnceLock` because the underlying aho-corasick automaton is stateless and cheap to clone nothing on call.

use std::sync::OnceLock;

fn shared_keyword_detector() -> &'static KeywordDetector {
    static DETECTOR: OnceLock<KeywordDetector> = OnceLock::new();
    DETECTOR.get_or_init(KeywordDetector::new)
}

/// Returns `Some(ctx)` for known names and `None` otherwise — caller converts to PyValueError. Avoids the pyo3 + clippy `useless_conversion` false
/// positive that fires when `?` propagates a `PyResult<_>` through another `PyResult<_>` (first seen under pyo3 0.22; shape kept under the pinned 0.29).
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

/// Lax substring check: does `text` contain any error indicator? Mirrors Python
/// `error_detection.content_has_error_indicators`. Same no-`detach` rationale as `score_line`.
#[pyfunction]
fn content_has_error_indicators(text: &str) -> PyResult<bool> {
    // catch_unwind → PyRuntimeError (see `panic_to_pyerr`): COR-7 (P0-1 audit).
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        shared_keyword_detector().contains_error_indicator(text)
    }))
    .map_err(panic_to_pyerr)
}

/// Expose Rust’s default keyword registry so Python can rebuild legacy regex objects without
/// duplicating keyword data. Static string keys and values make `set_item` infallible here.
#[pyfunction]
fn keyword_registry_snapshot(py: Python<'_>) -> Py<PyDict> {
    let registry = KeywordRegistry::default_set();
    let dict = PyDict::new(py);
    for (key, words) in registry.as_map() {
        dict.set_item(key, words).unwrap();
    }
    dict.unbind()
}

//

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

    /// Compress `content`. CCR persistence is the caller's responsibility — the Rust side never writes to the store. If the result
    /// needs a CCR marker, `cache_key` will be populated and the Python shim writes the original to the existing `CompressionStore`.
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

// ─── log_compressor bridge (Phase 3e.5) ─────────────────────────────── Mirrors `furl_ctx.transforms.log_compressor.LogCompressor`.
// Same CCR pattern as search_compressor: Rust emits a `cache_key`, Python shim writes the original to the production `CompressionStore`.

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
        max_unique_logs = 10,
        unique_log_threshold = 3,
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
        max_unique_logs: usize,
        unique_log_threshold: usize,
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
                max_unique_logs,
                unique_log_threshold,
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
    #[getter]
    fn unique_logs_kept(&self) -> usize {
        self.stats.unique_logs_kept
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

    /// Compress `content`.
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

// ─── text_crusher bridge (Engine P2-11) ─────────────────────────────── Mirrors `furl_ctx.transforms.text_crusher.TextCrusher`.
// Same CCR pattern as the log/search bridges: Rust emits a `cache_key` after backing the crush in a per-call in-memory store.

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
        secret_keep_rail = true,
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
        secret_keep_rail: bool,
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
                secret_keep_rail,
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
    fn secret_keep_segments(&self) -> usize {
        self.stats.secret_keep_segments
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

    /// Compress `content`. Same CCR pattern as the log/search bridges: the Rust side backs the crush in a per-call in-memory store and emits
    /// `cache_key`; the Python shim persists the original to the production `CompressionStore` and vetoes (serves the original) on write failure.
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

// ─── Module init ───────────────────────────────────────────────────────────

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(hello, m)?)?;
    m.add_class::<PyDiffCompressorConfig>()?;
    m.add_class::<PyDiffCompressionResult>()?;
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
    m.add_function(wrap_pyfunction!(detect_content_type, m)?)?;
    m.add_function(wrap_pyfunction!(score_line, m)?)?;
    m.add_function(wrap_pyfunction!(content_has_error_indicators, m)?)?;
    m.add_function(wrap_pyfunction!(keyword_registry_snapshot, m)?)?;
    Ok(())
}
