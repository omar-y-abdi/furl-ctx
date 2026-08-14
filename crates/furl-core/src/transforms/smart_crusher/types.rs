//! Core data types for SmartCrusher.

use serde_json::Value;

use crate::transforms::anchor_selector::DataPattern;
use std::collections::BTreeMap;

/// One CCR-recoverable reduction produced by a crush — the typed carrier the FFI hands to Python so recovery
/// mirroring never depends on re-parsing rendered `<<ccr:...>>` marker text (§4.2 / ARCH-2 / TYPE-2).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DroppedRef {
    /// Whole rows dropped from an array.
    RowDrop {
        /// 12-char SHA-256 hex prefix keying the stored full-original array — the same hash the rendered `<<ccr:HASH N_rows_offloaded>>` marker carries.
        hash: String,
    },
    /// An opaque payload (long base64 / HTML / long-text blob) substituted in place by a
    /// `<<ccr:HASH,KIND,SIZE>>` marker. The original bytes are recoverable via `ccr_get(hash)`.
    Opaque {
        /// 12-char SHA-256 hex prefix of the payload bytes — the same
        /// hash the rendered marker carries.
        hash: String,
        /// Pre-resolved wire kind token (`"base64"` / `"string"` / `"html"` / custom) —
        /// byte-identical to the KIND field of the rendered marker (`OpaqueKind::wire_str`).
        kind: String,
        /// EXACT original payload length in bytes. The rendered marker only carries the
        /// lossy humanized form (`"2.1KB"`); the typed ref preserves the precise size.
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
}

/// Compression strategies based on data patterns. The string variants must match
/// Python's `Enum.value` exactly — they appear in strategy debug strings (e.g.
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

/// JSON type classification of a field's first non-null value (TYPE-1).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FieldType {
    Null,
    Boolean,
    Numeric,
    String,
    Object,
    Array,
    /// Totality escape hatch — unreachable for parsed JSON (every `serde_json::Value` variant maps
    /// to one of the above), kept so the classifying `match` stays total without a panic arm.
    Unknown,
}

impl FieldType {
    /// Byte-identical to the historical `field_type` string literals
    /// (which match Python's values).
    pub fn as_str(self) -> &'static str {
        match self {
            FieldType::Null => "null",
            FieldType::Boolean => "boolean",
            FieldType::Numeric => "numeric",
            FieldType::String => "string",
            FieldType::Object => "object",
            FieldType::Array => "array",
            FieldType::Unknown => "unknown",
        }
    }
}

/// Statistics for a single field across array items. Field naming and Optional<T> shape
/// match Python exactly so the PyO3 bridge can `from_dict`-reconstruct the Python dataclass.
#[derive(Debug, Clone)]
pub struct FieldStats {
    pub name: String,
    /// Typed JSON classification of the field (TYPE-1). The historical
    /// string forms (`"numeric"`, `"string"`, ...) are `as_str()`.
    pub field_type: FieldType,
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

/// Crushability verdict label This is a cross-module contract, not just a debug string: - `the Rust module`'s entropy-floor override gate matches the two
/// `*NoSignal` variants ([`SkipReason::is_no_signal`]) to decide whether a CCR-backed store may override the analyzer's veto. - The string form is FFI-visible.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SkipReason {
    /// Crushable: near-constant content distinguished only by an ID
    /// field.
    RepetitiveContentWithIds,
    /// Crushable: low uniqueness — safe to sample.
    LowUniquenessSafeToSample,
    /// NOT crushable: high uniqueness + ID field + no signal.
    UniqueEntitiesNoSignal,
    /// Crushable: high uniqueness but a signal anchors the sample.
    UniqueEntitiesWithSignal,
    /// NOT crushable: medium uniqueness + no signal.
    MediumUniquenessNoSignal,
    /// Crushable (with caution): medium uniqueness + signal.
    MediumUniquenessWithSignal,
}

impl SkipReason {
    /// Byte-identical to the historical reason strings (FFI-visible via the `skip:<reason>` passthrough `strategy_info`; parity fixtures pin those bytes).
    pub fn as_str(self) -> &'static str {
        match self {
            SkipReason::RepetitiveContentWithIds => "repetitive_content_with_ids",
            SkipReason::LowUniquenessSafeToSample => "low_uniqueness_safe_to_sample",
            SkipReason::UniqueEntitiesNoSignal => "unique_entities_no_signal",
            SkipReason::UniqueEntitiesWithSignal => "unique_entities_with_signal",
            SkipReason::MediumUniquenessNoSignal => "medium_uniqueness_no_signal",
            SkipReason::MediumUniquenessWithSignal => "medium_uniqueness_with_signal",
        }
    }

    /// Is this a "no SIGNAL on distinct data" verdict (as opposed to a structural one)? Only
    /// these two are eligible for the CCR-backed entropy-floor override in `the Rust module`.
    pub fn is_no_signal(self) -> bool {
        match self {
            SkipReason::UniqueEntitiesNoSignal | SkipReason::MediumUniquenessNoSignal => true,
            SkipReason::RepetitiveContentWithIds
            | SkipReason::LowUniquenessSafeToSample
            | SkipReason::UniqueEntitiesWithSignal
            | SkipReason::MediumUniquenessWithSignal => false,
        }
    }
}

impl std::fmt::Display for SkipReason {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Analysis of whether an array is safe to crush. The key invariant: **if we don't have
/// a reliable signal to determine which items are important, we don't crush at all**.
#[derive(Debug, Clone)]
pub struct CrushabilityAnalysis {
    pub crushable: bool,
    pub confidence: f64,
    pub reason: SkipReason,
    /// True iff at least one crushability signal fired (score field, structural outliers, error keywords, numeric anomalies, or a change point).
    pub has_any_signal: bool,

    // Memoized detection indices (PERF-3).
    /// Indices flagged by `detect_structural_outliers` — ascending.
    pub structural_outlier_indices: Vec<usize>,
    /// Indices flagged by `detect_error_items_for_preservation` — ascending.
    pub error_keyword_indices: Vec<usize>,
}

impl CrushabilityAnalysis {
    /// Helper to build a "not crushable" verdict — used in several early exits in `analyze_crushability`.
    /// Mirrors the Python pattern where `crushable=False` paths don't bother filling in detail metrics.
    pub fn skip(reason: SkipReason, confidence: f64) -> Self {
        CrushabilityAnalysis {
            crushable: false,
            confidence,
            reason,
            has_any_signal: false,
            structural_outlier_indices: Vec::new(),
            error_keyword_indices: Vec::new(),
        }
    }
}

/// Array analysis stores field statistics in deterministic sorted order. If downstream behavior becomes
/// order-sensitive, align Python and Rust explicitly rather than relying on Python insertion order versus Rust sorting.
#[derive(Debug, Clone)]
pub struct ArrayAnalysis {
    pub item_count: usize,
    pub field_stats: BTreeMap<String, FieldStats>,
    /// Typed data-pattern classification (TYPE-1). The historical string forms (`"time_series"` /
    /// `"logs"` / `"search_results"` / `"generic"`) are recoverable via [`DataPattern::as_str`].
    pub detected_pattern: DataPattern,
    pub recommended_strategy: CompressionStrategy,
    pub estimated_reduction: f64,
    pub crushability: Option<CrushabilityAnalysis>,
}

/// Plan for array compression; keep fields behaviorally aligned with the Python plan.
/// `keep_indices` is the list of original-array indices that survive compression.
#[derive(Debug, Clone)]
pub struct CompressionPlan {
    pub strategy: CompressionStrategy,
    pub keep_indices: Vec<usize>,
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
            cluster_field: None,
            sort_field: None,
            keep_count: 10,
        }
    }
}

/// Result from `SmartCrusher.crush()` used for JSON-array routing; keep its contract aligned with Python.
#[derive(Debug, Clone)]
pub struct CrushResult {
    pub compressed: String,
    pub original: String,
    pub was_modified: bool,
    pub strategy: String,
    /// Every CCR-recoverable reduction this crush produced, TYPED. Unlike `CrushArrayResult::ccr_hash` (a single hash for one top-level array), `crush()` recurses via `process_value` and can reduce MANY
    /// spots at any depth — row-drops from dict arrays via `crush_array`, string/number/mixed arrays via `ccr_dropped_sentinel`, and opaque-blob substitutions from the compaction/`process_string` paths.
    pub dropped: Vec<DroppedRef>,
}

impl CrushResult {
    /// Pass-through result: same as input, no modification, strategy `"passthrough"`. Used
    /// when content can't be compressed (not JSON, too small, no crushable arrays, etc.).
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

    /// Row-drop CCR hashes, in emission order — derived back-compat getter, byte-identical to the retired `ccr_hashes` FIELD (which carried row-drop
    /// hashes ONLY; opaque refs live in [`Self::dropped`] and are deliberately excluded here so pre-§4.2 consumers see the exact values the field held).
    pub fn ccr_hashes(&self) -> Vec<String> {
        self.dropped
            .iter()
            .filter_map(|d| match d {
                DroppedRef::RowDrop { hash, .. } => Some(hash.clone()),
                DroppedRef::Opaque { .. } => None,
            })
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
        let r = CrushabilityAnalysis::skip(SkipReason::UniqueEntitiesNoSignal, 1.0);
        assert!(!r.crushable);
        assert_eq!(r.confidence, 1.0);
        assert_eq!(r.reason, SkipReason::UniqueEntitiesNoSignal);
    }

    #[test]
    fn skip_reason_strings_are_byte_identical_to_the_historical_literals() {
        // The reason string is FFI-visible via the `skip:<reason>` passthrough strategy_info (the Rust module) — parity fixtures pin the exact bytes.
        assert_eq!(
            SkipReason::RepetitiveContentWithIds.as_str(),
            "repetitive_content_with_ids"
        );
        assert_eq!(
            SkipReason::LowUniquenessSafeToSample.as_str(),
            "low_uniqueness_safe_to_sample"
        );
        assert_eq!(
            SkipReason::UniqueEntitiesNoSignal.as_str(),
            "unique_entities_no_signal"
        );
        assert_eq!(
            SkipReason::UniqueEntitiesWithSignal.as_str(),
            "unique_entities_with_signal"
        );
        assert_eq!(
            SkipReason::MediumUniquenessNoSignal.as_str(),
            "medium_uniqueness_no_signal"
        );
        assert_eq!(
            SkipReason::MediumUniquenessWithSignal.as_str(),
            "medium_uniqueness_with_signal"
        );
        // Display mirrors as_str (used by the `skip:{}` format site).
        assert_eq!(
            format!("skip:{}", SkipReason::UniqueEntitiesNoSignal),
            "skip:unique_entities_no_signal"
        );
    }

    #[test]
    fn skip_reason_no_signal_gate_is_exactly_the_two_no_signal_variants() {
        // Entropy-floor override eligibility is fail-closed: only the two no-signal verdicts qualify.
        assert!(SkipReason::UniqueEntitiesNoSignal.is_no_signal());
        assert!(SkipReason::MediumUniquenessNoSignal.is_no_signal());
        assert!(!SkipReason::RepetitiveContentWithIds.is_no_signal());
        assert!(!SkipReason::LowUniquenessSafeToSample.is_no_signal());
        assert!(!SkipReason::UniqueEntitiesWithSignal.is_no_signal());
        assert!(!SkipReason::MediumUniquenessWithSignal.is_no_signal());
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
    fn dropped_ref_row_drop_hash_accessor() {
        let row_drop = DroppedRef::RowDrop {
            hash: "9f3a2b9f3a2b".to_string(),
        };
        assert_eq!(row_drop.hash(), "9f3a2b9f3a2b");
    }

    #[test]
    fn dropped_ref_opaque_hash_accessor() {
        let opaque = DroppedRef::Opaque {
            hash: "ff00ff00ff00".to_string(),
            kind: "base64".to_string(),
            byte_size: 2150,
        };
        assert_eq!(opaque.hash(), "ff00ff00ff00");
    }

    #[test]
    fn ccr_hashes_lists_row_drops_in_order_excluding_opaque() {
        // `ccr_hashes` carries every ROW-DROP hash in emission order;
        // opaque refs are excluded from the back-compat getter.
        let r = result_with(vec![
            DroppedRef::RowDrop {
                hash: "aaaaaaaaaaaa".to_string(),
            },
            DroppedRef::Opaque {
                hash: "cccccccccccc".to_string(),
                kind: "html".to_string(),
                byte_size: 512,
            },
            DroppedRef::RowDrop {
                hash: "bbbbbbbbbbbb".to_string(),
            },
        ]);
        assert_eq!(r.ccr_hashes(), vec!["aaaaaaaaaaaa", "bbbbbbbbbbbb"]);
    }
}
