//! SmartCrusher Rust implementation preserves Python output parity while adding lossless-first routing
//! and CCR recovery. Selection-critical parity fixes must land consistently across both implementations.

mod analyzer;
mod anchors;
mod builder;
mod classifier;
pub mod compaction;
mod config;
mod crusher;
mod crushers;
mod error_keywords;
mod field_detect;
mod field_role;
mod orchestration;
mod outliers;
mod persist;
mod planning;
mod route;
mod statistics;
mod stats_math;
mod types;
mod walk;

/// The relevance-scoring seam — the one remaining `SmartCrusher` extension point.
/// Re-exported here so callers can reach it alongside the rest of the smart-crusher surface.
pub use crate::relevance::RelevanceScorer as Scorer;
pub use analyzer::SmartAnalyzer;
pub use anchors::{extract_query_anchors, item_matches_anchors};
pub use builder::SmartCrusherBuilder;
pub use classifier::{classify_array, ArrayType};
pub use config::{RoutingPolicy, SmartCrusherConfig};
pub use crusher::{CrushArrayResult, SmartCrusher};
pub use crushers::{compute_k_split, crush_number_array, crush_object, crush_string_array};
pub use error_keywords::ERROR_KEYWORDS;
pub use field_detect::{detect_id_field_statistically, detect_score_field_statistically};
pub use field_role::{classify_field, compute_exclude_set, FieldRole};
pub use orchestration::{
    deduplicate_indices_by_content, fill_remaining_slots, prioritize_indices, PrioritizeParams,
};
pub use outliers::{
    detect_error_items_for_preservation, detect_rare_status_values, detect_structural_outliers,
};
pub use planning::{
    hash_field_name, item_has_preserve_field_match, map_to_anchor_pattern, SmartCrusherPlanner,
};
pub use statistics::{calculate_string_entropy, detect_sequential_pattern, is_uuid_format};
pub use stats_math::{format_g, mean, median, sample_stdev, sample_variance};
pub use types::{
    ArrayAnalysis, CompressionPlan, CompressionStrategy, CrushResult, CrushabilityAnalysis,
    DroppedRef, FieldStats, FieldType, SkipReason,
};
