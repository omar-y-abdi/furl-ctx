//! Base trait and types for relevance scoring.
//!
//! Direct port of `headroom/relevance/base.py`. The Python version uses
//! ABC + abstractmethod; we use a Rust trait with the same required
//! method (`score`) plus a default `score_batch` that delegates to
//! per-item `score`.

/// Relevance score with explainability fields.
///
/// Mirrors Python's `RelevanceScore` dataclass. The `__post_init__`
/// score-clamp is enforced via the `new` constructor.
#[derive(Debug, Clone)]
pub struct RelevanceScore {
    pub score: f64,
    pub reason: String,
    pub matched_terms: Vec<String>,
}

impl RelevanceScore {
    /// Build a score, clamping to `[0.0, 1.0]` to mirror Python's
    /// `__post_init__` behavior.
    pub fn new(score: f64, reason: impl Into<String>, matched_terms: Vec<String>) -> Self {
        RelevanceScore {
            score: score.clamp(0.0, 1.0),
            reason: reason.into(),
            matched_terms,
        }
    }

    /// Convenience for "no match" scores.
    pub fn empty(reason: impl Into<String>) -> Self {
        RelevanceScore::new(0.0, reason, Vec::new())
    }
}

impl Default for RelevanceScore {
    fn default() -> Self {
        RelevanceScore::new(0.0, "", Vec::new())
    }
}

/// Trait that every relevance scorer implements.
///
/// Mirrors Python's `RelevanceScorer` ABC: required `score` for single
/// items, with a default `score_batch` for collections (subclasses
/// override for vectorized impls; otherwise the per-item fallback runs).
pub trait RelevanceScorer {
    /// Score a single item against the context.
    fn score(&self, item: &str, context: &str) -> RelevanceScore;

    /// Score a batch of items. Default impl delegates to per-item
    /// `score` — override when the scorer can amortize work across
    /// items (BM25 pre-tokenizes context once, embeddings batch the
    /// matrix multiplication, etc.).
    fn score_batch(&self, items: &[&str], context: &str) -> Vec<RelevanceScore> {
        items.iter().map(|item| self.score(item, context)).collect()
    }

    /// Whether this scorer is available in the current environment.
    /// Override for scorers with optional deps (e.g. ONNX embeddings).
    fn is_available(&self) -> bool {
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn relevance_score_clamps_above_one() {
        let s = RelevanceScore::new(1.5, "", Vec::new());
        assert_eq!(s.score, 1.0);
    }

    #[test]
    fn relevance_score_clamps_below_zero() {
        let s = RelevanceScore::new(-0.5, "", Vec::new());
        assert_eq!(s.score, 0.0);
    }

    #[test]
    fn relevance_score_passes_through_valid_range() {
        let s = RelevanceScore::new(0.5, "test", vec!["term".to_string()]);
        assert_eq!(s.score, 0.5);
        assert_eq!(s.reason, "test");
        assert_eq!(s.matched_terms, vec!["term"]);
    }

    #[test]
    fn empty_score_zero_with_reason() {
        let s = RelevanceScore::empty("no match");
        assert_eq!(s.score, 0.0);
        assert_eq!(s.reason, "no match");
        assert!(s.matched_terms.is_empty());
    }

    // Trivial scorer to exercise the trait's default `score_batch` fallback.
    struct StubScorer;
    impl RelevanceScorer for StubScorer {
        fn score(&self, _item: &str, _context: &str) -> RelevanceScore {
            RelevanceScore::new(0.42, "stub", Vec::new())
        }
    }

    #[test]
    fn trait_default_batch_uses_per_item_score() {
        let scorer = StubScorer;
        let items = ["x", "y"];
        let scores = scorer.score_batch(&items, "ctx");
        assert_eq!(scores.len(), 2);
    }
}
