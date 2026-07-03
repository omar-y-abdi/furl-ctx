//! Core data types for SmartCrusher.
//!
//! Direct port of the dataclasses in `smart_crusher.py:318-924`. These
//! mirror the Python shapes 1:1 so the PyO3 bridge in stage 3c.1b can
//! reconstruct Python dataclasses from the Rust output without a manual
//! field-by-field translator.

use serde_json::Value;
use std::collections::BTreeMap;

use crate::ccr::marker_for_row_index;
use crate::ccr::persist::row_index_key;

/// One CCR-recoverable reduction produced by a crush — the typed carrier
/// the FFI hands to Python so recovery mirroring never depends on
/// re-parsing rendered `<<ccr:...>>` marker text (§4.2 / ARCH-2 / TYPE-2).
///
/// The values are exactly those the emission sites already compute when
/// they render the markers (`persist_dropped` for row-drops,
/// `emit_opaque_ccr_marker` / `cell_from_value` for opaque
/// substitutions): carrying them here is pure plumbing — it never
/// changes the rendered bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DroppedRef {
    /// Whole rows dropped from an array. The full original array is
    /// recoverable via `ccr_get(hash)`; when a granular row index was
    /// written, single rows are recoverable proportionally via the
    /// `{hash}#rows` index (see [`DroppedRef::row_index_key`]).
    RowDrop {
        /// 12-char SHA-256 hex prefix keying the stored full-original
        /// array — the same hash the rendered
        /// `<<ccr:HASH N_rows_offloaded>>` marker carries.
        hash: String,
        /// Number of per-row chunks the store-side row index holds
        /// (exactly the in-range dropped rows — COR-20). `None` when no
        /// store was configured to chunk into or the drop exceeded the
        /// COR-4 granular budget (whole-blob recovery still covers it).
        ///
        /// The plan's `row_index_key` is exposed as a derived accessor
        /// instead of a stored field: the key is `"{hash}#rows"` by
        /// store contract (single owner: `ccr::persist::row_index_key`),
        /// while the chunk count is the datum the back-compat
        /// [`CrushResult::row_index_markers`] getter needs to
        /// reconstruct the rendered marker byte-identically.
        row_index_chunks: Option<usize>,
    },
    /// An opaque payload (long base64 / HTML / long-text blob)
    /// substituted in place by a `<<ccr:HASH,KIND,SIZE>>` marker. The
    /// original bytes are recoverable via `ccr_get(hash)`.
    Opaque {
        /// 12-char SHA-256 hex prefix of the payload bytes — the same
        /// hash the rendered marker carries.
        hash: String,
        /// Pre-resolved wire kind token (`"base64"` / `"string"` /
        /// `"html"` / custom) — byte-identical to the KIND field of the
        /// rendered marker (`OpaqueKind::wire_str`).
        kind: String,
        /// EXACT original payload length in bytes. The rendered marker
        /// only carries the lossy humanized form (`"2.1KB"`); the typed
        /// ref preserves the precise size.
        byte_size: usize,
    },
}

impl DroppedRef {
    /// The CCR store hash of this ref, whichever variant.
    pub fn hash(&self) -> &str {
        match self {
            DroppedRef::RowDrop { hash, .. } | DroppedRef::Opaque { hash, .. } => hash,
        }
    }

    /// Bare store key of the granular row index (`"{hash}#rows"`) — the
    /// key Python mirrors per-row chunks from. NOT marker text. `Some`
    /// only for a [`DroppedRef::RowDrop`] that actually has an index.
    pub fn row_index_key(&self) -> Option<String> {
        match self {
            DroppedRef::RowDrop {
                hash,
                row_index_chunks: Some(_),
            } => Some(row_index_key(hash)),
            _ => None,
        }
    }

    /// Rendered `<<ccr:{hash}#rows {n}_chunks>>` marker for this ref's
    /// row index, byte-identical to the one embedded in the output
    /// (grammar owner: `ccr::markers`). Back-compat derivation for
    /// [`CrushResult::row_index_markers`].
    pub fn row_index_marker(&self) -> Option<String> {
        match self {
            DroppedRef::RowDrop {
                hash,
                row_index_chunks: Some(n),
            } => Some(marker_for_row_index(hash, *n)),
            _ => None,
        }
    }
}

/// Compression strategies based on data patterns.
///
/// Mirrors `CompressionStrategy` enum at `smart_crusher.py:318-326`. The
/// string variants must match Python's `Enum.value` exactly — they appear
/// in strategy debug strings (e.g. `"top_n(100->10)"`) and the parity
/// fixtures lock those bytes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum CompressionStrategy {
    /// No compression needed.
    None,
    /// Explicitly skip — not safe to crush.
    Skip,
    /// Time-series: keep change points, summarize stable runs.
    TimeSeries,
    /// Cluster-sample: dedupe similar items.
    ClusterSample,
    /// Top-N: keep highest-scored items.
    TopN,
    /// Smart-sample: statistical sampling with anchor-preservation.
    SmartSample,
}

impl CompressionStrategy {
    /// Lowercase string matching Python's `Enum.value`. Pinned by the
    /// parity fixtures — must not drift.
    pub fn as_str(self) -> &'static str {
        match self {
            CompressionStrategy::None => "none",
            CompressionStrategy::Skip => "skip",
            CompressionStrategy::TimeSeries => "time_series",
            CompressionStrategy::ClusterSample => "cluster",
            CompressionStrategy::TopN => "top_n",
            CompressionStrategy::SmartSample => "smart_sample",
        }
    }
}

/// Statistics for a single field across array items.
///
/// Mirrors the `FieldStats` dataclass at `smart_crusher.py:864-885`.
/// Field naming and Optional<T> shape match Python exactly so the PyO3
/// bridge can `from_dict`-reconstruct the Python dataclass.
#[derive(Debug, Clone)]
pub struct FieldStats {
    pub name: String,
    /// One of: `"numeric"`, `"string"`, `"boolean"`, `"object"`, `"array"`,
    /// `"null"`. String literals match Python's `field_type` values.
    pub field_type: String,
    pub count: usize,
    pub unique_count: usize,
    pub unique_ratio: f64,
    pub is_constant: bool,
    pub constant_value: Option<Value>,

    // Numeric-specific
    pub min_val: Option<f64>,
    pub max_val: Option<f64>,
    pub mean_val: Option<f64>,
    pub variance: Option<f64>,
    pub change_points: Vec<usize>,

    // String-specific
    pub avg_length: Option<f64>,
    /// Top values by frequency, descending. Bounded list so this stays
    /// cheap to build and serialize. Same shape as Python's `list[tuple[str, int]]`.
    pub top_values: Vec<(String, usize)>,
}

/// Analysis of whether an array is safe to crush.
///
/// Mirrors `CrushabilityAnalysis` at `smart_crusher.py:833-860`. The key
/// invariant: **if we don't have a reliable signal to determine which
/// items are important, we don't crush at all**. Signals include score
/// fields, error keywords, numeric anomalies, and low uniqueness.
#[derive(Debug, Clone)]
pub struct CrushabilityAnalysis {
    pub crushable: bool,
    pub confidence: f64,
    pub reason: String,
    pub signals_present: Vec<String>,
    pub signals_absent: Vec<String>,

    // Detailed metrics (mirroring Python field-by-field)
    pub has_id_field: bool,
    pub id_uniqueness: f64,
    pub avg_string_uniqueness: f64,
    pub has_score_field: bool,
    pub error_item_count: usize,
    pub anomaly_count: usize,
}

impl CrushabilityAnalysis {
    /// Helper to build a "not crushable" verdict — used in several early
    /// exits in `analyze_crushability`. Mirrors the Python pattern where
    /// `crushable=False` paths don't bother filling in detail metrics.
    pub fn skip(reason: impl Into<String>, confidence: f64) -> Self {
        CrushabilityAnalysis {
            crushable: false,
            confidence,
            reason: reason.into(),
            signals_present: Vec::new(),
            signals_absent: Vec::new(),
            has_id_field: false,
            id_uniqueness: 0.0,
            avg_string_uniqueness: 0.0,
            has_score_field: false,
            error_item_count: 0,
            anomaly_count: 0,
        }
    }
}

/// Complete analysis of an array.
///
/// Mirrors `ArrayAnalysis` at `smart_crusher.py:887-897`. `field_stats`
/// uses `BTreeMap` for sorted-by-key iteration. (Per-field constancy
/// lives on `FieldStats.is_constant` / `constant_value`, which
/// `estimate_reduction` reads; the compaction stage's constant-column
/// fold computes its own. The old aggregate `constant_fields` snapshot —
/// and the `factor_out_constants` config knob that toggled copying it
/// into `CompressionPlan` — had zero downstream readers and were
/// deleted as dead config.)
///
/// # Sort vs insertion order — known parity nuance
///
/// Python's `dict` preserves insertion order, and `_analyze_field` is
/// called once per key as it appears in `items[0].keys()` (i.e., JSON
/// parse order). With `serde_json/preserve_order` enabled at the
/// workspace level, `serde_json::Map` is an `IndexMap` and parse order
/// matches Python.
///
/// `BTreeMap` here gives sorted-key iteration — which differs from
/// Python's parse-order `dict`. This matters only if downstream code
/// observes the iteration order of `field_stats` (e.g., when emitting
/// debug output, picking a "first" field, or computing strategy
/// strings that include field names).
///
/// If any code path observes iteration order, the options are to either
///   1. Switch this to `IndexMap`, OR
///   2. Rewrite Python's order-sensitive paths to iterate sorted, then
///      mirror that in Rust.
#[derive(Debug, Clone)]
pub struct ArrayAnalysis {
    pub item_count: usize,
    pub field_stats: BTreeMap<String, FieldStats>,
    /// One of: `"time_series"`, `"logs"`, `"search_results"`, `"generic"`.
    pub detected_pattern: String,
    pub recommended_strategy: CompressionStrategy,
    pub estimated_reduction: f64,
    pub crushability: Option<CrushabilityAnalysis>,
}

/// Plan for how to compress an array.
///
/// Mirrors `CompressionPlan` at `smart_crusher.py:900-910`. `keep_indices`
/// is the list of original-array indices that survive compression;
/// `summary_ranges` carries `(start, end, summary_dict)` for runs we
/// summarized rather than dropped (currently unused in the Python impl
/// but plumbed through for parity with the dataclass).
#[derive(Debug, Clone)]
pub struct CompressionPlan {
    pub strategy: CompressionStrategy,
    pub keep_indices: Vec<usize>,
    /// `(start, end, summary)` triples for summarized runs. Python uses
    /// `list[tuple[int, int, dict]]`; we use `Value` for the summary so
    /// any JSON shape is representable.
    pub summary_ranges: Vec<(usize, usize, Value)>,
    pub cluster_field: Option<String>,
    pub sort_field: Option<String>,
    pub keep_count: usize,
}

impl Default for CompressionPlan {
    fn default() -> Self {
        // Mirrors Python's @dataclass defaults at line 900-910.
        CompressionPlan {
            strategy: CompressionStrategy::None,
            keep_indices: Vec::new(),
            summary_ranges: Vec::new(),
            cluster_field: None,
            sort_field: None,
            keep_count: 10,
        }
    }
}

/// Result from `SmartCrusher.crush()` — used by ContentRouter when
/// routing JSON arrays. Mirrors `CrushResult` at `smart_crusher.py:913-923`.
#[derive(Debug, Clone)]
pub struct CrushResult {
    pub compressed: String,
    pub original: String,
    pub was_modified: bool,
    pub strategy: String,
    /// Every CCR-recoverable reduction this crush produced, TYPED.
    /// Unlike `CrushArrayResult::ccr_hash` (a single hash for one
    /// top-level array), `crush()` recurses via `process_value` and can
    /// reduce MANY spots at any depth — row-drops from dict arrays via
    /// `crush_array`, string/number/mixed arrays via
    /// `ccr_dropped_sentinel`, and opaque-blob substitutions from the
    /// compaction/`process_string` paths. Each contributes one
    /// [`DroppedRef`] here, in emission order. The Python shim mirrors
    /// each DIRECTLY into the compression_store (typed recovery) instead
    /// of substring-scraping `<<ccr:...>>` out of `compressed`. These
    /// are the SAME hashes the embedded markers carry — pure plumbing of
    /// values the emission sites already computed, NOT a re-hash, so
    /// `compressed` is byte-identical to before this field existed.
    /// Empty when nothing was reduced.
    pub dropped: Vec<DroppedRef>,
}

impl CrushResult {
    /// Pass-through result: same as input, no modification, strategy
    /// `"passthrough"`. Used when content can't be compressed (not JSON,
    /// too small, no crushable arrays, etc.).
    pub fn passthrough(content: impl Into<String>) -> Self {
        let s = content.into();
        CrushResult {
            compressed: s.clone(),
            original: s,
            was_modified: false,
            strategy: "passthrough".to_string(),
            // Passthrough drops nothing → no recovery refs.
            dropped: Vec::new(),
        }
    }

    /// Row-drop CCR hashes, in emission order — derived back-compat
    /// getter, byte-identical to the retired `ccr_hashes` FIELD (which
    /// carried row-drop hashes ONLY; opaque refs live in [`Self::dropped`]
    /// and are deliberately excluded here so pre-§4.2 consumers see the
    /// exact values the field held).
    pub fn ccr_hashes(&self) -> Vec<String> {
        self.dropped
            .iter()
            .filter_map(|d| match d {
                DroppedRef::RowDrop { hash, .. } => Some(hash.clone()),
                DroppedRef::Opaque { .. } => None,
            })
            .collect()
    }

    /// Granular per-blob row-index markers (`<<ccr:HASH#rows N_chunks>>`)
    /// in emission order — derived back-compat getter, byte-identical to
    /// the retired `row_index_markers` FIELD: one marker per row-drop
    /// that had a store configured to chunk into. May be shorter than
    /// [`Self::ccr_hashes`] (a drop with no store produces a hash but no
    /// row index); never longer. New consumers should read the bare
    /// [`DroppedRef::row_index_key`] instead of re-parsing marker text.
    pub fn row_index_markers(&self) -> Vec<String> {
        self.dropped
            .iter()
            .filter_map(DroppedRef::row_index_marker)
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compression_strategy_strings_match_python() {
        // Strategy debug strings appear in the parity fixtures; these must
        // not drift. If a value here changes, every fixture breaks.
        assert_eq!(CompressionStrategy::None.as_str(), "none");
        assert_eq!(CompressionStrategy::Skip.as_str(), "skip");
        assert_eq!(CompressionStrategy::TimeSeries.as_str(), "time_series");
        assert_eq!(CompressionStrategy::ClusterSample.as_str(), "cluster");
        assert_eq!(CompressionStrategy::TopN.as_str(), "top_n");
        assert_eq!(CompressionStrategy::SmartSample.as_str(), "smart_sample");
    }

    #[test]
    fn crushability_skip_helper() {
        let r = CrushabilityAnalysis::skip("too small", 1.0);
        assert!(!r.crushable);
        assert_eq!(r.confidence, 1.0);
        assert_eq!(r.reason, "too small");
    }

    #[test]
    fn compression_plan_default_keep_count_matches_python() {
        // Python's @dataclass default is `keep_count: int = 10`.
        let p = CompressionPlan::default();
        assert_eq!(p.keep_count, 10);
        assert_eq!(p.strategy, CompressionStrategy::None);
        assert!(p.keep_indices.is_empty());
    }

    #[test]
    fn crush_result_passthrough() {
        let r = CrushResult::passthrough("hello");
        assert_eq!(r.compressed, "hello");
        assert_eq!(r.original, "hello");
        assert!(!r.was_modified);
        assert_eq!(r.strategy, "passthrough");
        assert!(r.dropped.is_empty());
        assert!(r.ccr_hashes().is_empty());
        assert!(r.row_index_markers().is_empty());
    }

    fn result_with(dropped: Vec<DroppedRef>) -> CrushResult {
        CrushResult {
            compressed: String::new(),
            original: String::new(),
            was_modified: true,
            strategy: "smart_sample".to_string(),
            dropped,
        }
    }

    #[test]
    fn dropped_ref_row_drop_accessors() {
        let with_index = DroppedRef::RowDrop {
            hash: "9f3a2b9f3a2b".to_string(),
            row_index_chunks: Some(50),
        };
        assert_eq!(with_index.hash(), "9f3a2b9f3a2b");
        // Bare key — NOT marker text (the datum R5's Python consumes).
        assert_eq!(
            with_index.row_index_key().as_deref(),
            Some("9f3a2b9f3a2b#rows")
        );
        // Reconstructed marker is byte-identical to the pinned grammar.
        assert_eq!(
            with_index.row_index_marker().as_deref(),
            Some("<<ccr:9f3a2b9f3a2b#rows 50_chunks>>")
        );

        let no_index = DroppedRef::RowDrop {
            hash: "abc123def456".to_string(),
            row_index_chunks: None,
        };
        assert_eq!(no_index.row_index_key(), None);
        assert_eq!(no_index.row_index_marker(), None);
    }

    #[test]
    fn dropped_ref_opaque_accessors() {
        let opaque = DroppedRef::Opaque {
            hash: "ff00ff00ff00".to_string(),
            kind: "base64".to_string(),
            byte_size: 2150,
        };
        assert_eq!(opaque.hash(), "ff00ff00ff00");
        // An opaque ref never has a row index of any form.
        assert_eq!(opaque.row_index_key(), None);
        assert_eq!(opaque.row_index_marker(), None);
    }

    #[test]
    fn derived_getters_match_the_retired_field_semantics() {
        // The retired fields carried: every ROW-DROP hash in emission
        // order, and one marker per drop that HAD a row index (shorter,
        // never longer). Opaque refs — new in the typed carrier — must
        // be excluded from both back-compat getters.
        let r = result_with(vec![
            DroppedRef::RowDrop {
                hash: "aaaaaaaaaaaa".to_string(),
                row_index_chunks: Some(3),
            },
            DroppedRef::Opaque {
                hash: "cccccccccccc".to_string(),
                kind: "html".to_string(),
                byte_size: 512,
            },
            DroppedRef::RowDrop {
                hash: "bbbbbbbbbbbb".to_string(),
                row_index_chunks: None,
            },
        ]);
        assert_eq!(r.ccr_hashes(), vec!["aaaaaaaaaaaa", "bbbbbbbbbbbb"]);
        assert_eq!(
            r.row_index_markers(),
            vec!["<<ccr:aaaaaaaaaaaa#rows 3_chunks>>"]
        );
    }
}
