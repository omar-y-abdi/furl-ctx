//! Property tests for the tokenizer module. Invariants we lean on for downstream callers (cost tracking, compression decisions, cache keys).

use furl_core::tokenizer::{get_tokenizer, EstimatingCounter, TiktokenCounter, Tokenizer};
use proptest::prelude::*;

proptest! {
    #![proptest_config(ProptestConfig {
        cases: 256,
        .. ProptestConfig::default()
    })]

    /// Determinism: counting the same text twice yields the same count
    /// regardless of which tokenizer is used.
    #[test]
    fn deterministic_per_instance(s in any::<String>()) {
        let tt = TiktokenCounter::for_model("gpt-4o-mini").unwrap();
        let est = EstimatingCounter::default();
        prop_assert_eq!(tt.count_text(&s), tt.count_text(&s));
        prop_assert_eq!(est.count_text(&s), est.count_text(&s));
    }

    /// Empty input produces zero tokens for every backend.
    #[test]
    fn empty_is_zero_for_all_backends(_dummy in 0u8..1) {
        for model in ["gpt-4o-mini", "claude-3-opus", "gemini-1.5-pro", "unknown-model"] {
            let t = get_tokenizer(model);
            prop_assert_eq!(t.count_text(""), 0, "{}", model);
        }
    }

    /// Non-empty input produces at least one token. The regex `+` quantifier
    /// already guarantees `s` is non-empty; no `prop_assume!` needed.
    #[test]
    fn nonempty_input_is_at_least_one_token(s in "[a-zA-Z0-9 ]+") {
        let tt = TiktokenCounter::for_model("gpt-4o-mini").unwrap();
        prop_assert!(tt.count_text(&s) >= 1);
        let est = EstimatingCounter::default();
        prop_assert!(est.count_text(&s) >= 1);
    }

    /// Concatenation is not strictly token-subadditive because BPE pre-tokenization can change at the boundary, especially for
    /// Unicode. Assert only that concatenation adds a small constant overhead and never causes multiplicative token growth.
    #[test]
    fn concat_does_not_explode(a in any::<String>(), b in any::<String>()) {
        let tt = TiktokenCounter::for_model("gpt-4o-mini").unwrap();
        let na = tt.count_text(&a);
        let nb = tt.count_text(&b);
        let mut combined = a.clone();
        combined.push_str(&b);
        let nc = tt.count_text(&combined);
        // Allow up to 8 extra tokens at the boundary (generous) to absorb
        // pre-tokenizer regex disagreements on exotic Unicode.
        prop_assert!(nc <= na + nb + 8,
            "concat blew up: count({a:?})={na} + count({b:?})={nb} = {} but count({combined:?}) = {nc}",
            na + nb);
    }

    /// Estimator monotone in input length (chars/token formula is monotone
    /// non-decreasing as char count grows).
    #[test]
    fn estimator_monotone_in_length(extra in "[a-z]{1,32}", base in "[a-z]{0,32}") {
        let est = EstimatingCounter::default();
        let n_base = est.count_text(&base);
        let mut longer = base.clone();
        longer.push_str(&extra);
        let n_long = est.count_text(&longer);
        prop_assert!(n_long >= n_base);
    }
}
