//! Cross-cutting detection traits used by transforms. Prefer structured parsers when grammar exists;
//! otherwise use deterministic pattern fallback. Detectors return signals only and must not mutate input.

pub mod keyword_detector;
pub mod line_importance;

pub use keyword_detector::{KeywordDetector, KeywordRegistry};
pub use line_importance::{
    ImportanceCategory, ImportanceContext, ImportanceSignal, LineImportanceDetector,
};
