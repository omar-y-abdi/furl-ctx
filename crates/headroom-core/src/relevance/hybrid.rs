//! BM25 relevance scorer with a single-/multi-term match boost.
//!
//! Direct port of the BM25-fallback path of `headroom/relevance/hybrid.py`.
//! The optional ONNX embedding-fusion path was feature-gated (`embeddings`),
//! never shipped in the default wheel, and never CI-tested; it has been
//! removed. `HybridScorer` now always scores BM25-only with the boost below.
//!
//! # BM25 match boost
//!
//! BM25 alone is weak on single-term matches (BM25 of "Alice" against
//! `{"name": "Alice"}` is ~0.07 raw, well below typical relevance
//! thresholds), so the scorer applies a boost:
//!
//! - Items with any matched term get `score >= 0.3`.
//! - Items with two or more matched terms get `+0.2`, capped at 1.0.
//!
//! Pinned to Python's `score`/`score_batch` boost rules
//! (`hybrid.py:171-181` and `225-238`).

use super::base::{RelevanceScore, RelevanceScorer};
use super::bm25::BM25Scorer;

/// BM25 relevance scorer with a match boost. Historically a BM25 +
/// embedding hybrid; the embedding-fusion path was removed with the
/// never-shipped `embeddings` feature, so this now scores BM25-only.
#[derive(Default)]
pub struct HybridScorer {
    pub bm25: BM25Scorer,
}

impl HybridScorer {
    /// BM25 match boost. Mirrors Python `score`/`score_batch` boost rules
    /// (`hybrid.py:171-181` and `225-238`).
    fn boost_bm25_only(&self, bm25_result: &RelevanceScore) -> RelevanceScore {
        let mut boosted = bm25_result.score;
        if !bm25_result.matched_terms.is_empty() {
            boosted = boosted.max(0.3);
            if bm25_result.matched_terms.len() >= 2 {
                boosted = (boosted + 0.2).min(1.0);
            }
        }
        RelevanceScore::new(
            boosted,
            format!("Hybrid (BM25 only, boosted): {}", bm25_result.reason),
            bm25_result.matched_terms.clone(),
        )
    }
}

impl RelevanceScorer for HybridScorer {
    fn score(&self, item: &str, context: &str) -> RelevanceScore {
        let bm25_result = self.bm25.score(item, context);
        self.boost_bm25_only(&bm25_result)
    }

    fn score_batch(&self, items: &[&str], context: &str) -> Vec<RelevanceScore> {
        if items.is_empty() {
            return Vec::new();
        }
        self.bm25
            .score_batch(items, context)
            .iter()
            .map(|r| self.boost_bm25_only(r))
            .collect()
    }

    fn is_available(&self) -> bool {
        // Always available — BM25 with a match boost.
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scorer() -> HybridScorer {
        HybridScorer::default()
    }

    #[test]
    fn hybrid_always_available() {
        assert!(scorer().is_available());
    }

    #[test]
    fn fallback_score_boosts_single_match_to_at_least_0_3() {
        // BM25 alone often gives ~0.07 for a single-term match.
        // The boost ensures the item clears 0.3.
        let s = scorer();
        let r = s.score(r#"{"name": "alice"}"#, "alice");
        assert!(
            r.score >= 0.3,
            "single-match item should clear 0.3 in fallback: got {}",
            r.score
        );
        assert!(r.reason.starts_with("Hybrid (BM25 only, boosted)"));
    }

    #[test]
    fn fallback_score_no_match_stays_zero() {
        let r = scorer().score(r#"{"id": 1}"#, "completely unrelated query");
        assert_eq!(r.score, 0.0);
    }

    #[test]
    fn fallback_score_two_or_more_matches_gets_extra_boost() {
        let s = scorer();
        // Three matched terms: should get the +0.2 second-tier boost.
        let r = s.score(
            r#"{"name": "alice", "role": "admin", "team": "engineering"}"#,
            "alice admin engineering",
        );
        assert!(
            r.score >= 0.5,
            "multi-match item should clear 0.5 in fallback: got {}",
            r.score
        );
    }

    #[test]
    fn fallback_score_batch_consistent_with_single() {
        let s = scorer();
        let items = [r#"{"name": "alice"}"#, r#"{"name": "bob"}"#];
        let single = s.score(items[0], "alice");
        let batch = s.score_batch(&items, "alice");
        // Boost rules apply to both paths; first item should match.
        assert!((batch[0].score - single.score).abs() < 0.5);
    }

    #[test]
    fn fallback_score_batch_empty_returns_empty() {
        let s = scorer();
        let result = s.score_batch(&[], "anything");
        assert!(result.is_empty());
    }
}
