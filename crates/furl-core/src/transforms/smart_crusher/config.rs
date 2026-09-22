//! SmartCrusher configuration. The defaults must match Python exactly — they're consulted everywhere during compression and any drift breaks parity fixtures. Four historical knobs (`enabled`,
//! `uniqueness_threshold`, `similarity_threshold`, `include_summaries`) were read by ZERO core paths and were deleted in lockstep across Rust + PyO3 kwargs + the Python dataclass (SIMP-7 wire-contract).

/// Choose between two fully recoverable array renders: lossless CSV-schema output with every row visible, or lossy-visible
/// output backed by CCR. Routing policy decides which representation ships; neither path may make data unrecoverable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RoutingPolicy {
    /// **Default.** When both a lossless render and a lossy-recoverable render exist
    /// for an array, ship the one with FEWER tokens (real tokenizer, not bytes).
    MinTokens,
    /// Legacy policy: prefer the lossless render whenever it clears the byte-savings gate
    /// (`lossless_min_savings_ratio`), even if the lossy-recoverable render would be fewer tokens.
    LosslessFirst,
}

impl RoutingPolicy {
    /// Parse the policy from its kebab-case string form (the wire form the PyO3 bridge and Python config use).
    /// Returns `None` for unknown values so the caller can surface a clear error rather than silently defaulting.
    #[allow(clippy::should_implement_trait)]
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "min-tokens" => Some(RoutingPolicy::MinTokens),
            "lossless-first" => Some(RoutingPolicy::LosslessFirst),
            _ => None,
        }
    }

    /// Kebab-case string form. Inverse of [`RoutingPolicy::from_str`].
    pub fn as_str(self) -> &'static str {
        match self {
            RoutingPolicy::MinTokens => "min-tokens",
            RoutingPolicy::LosslessFirst => "lossless-first",
        }
    }
}

/// Configuration for SmartCrusher.
#[derive(Debug, Clone)]
pub struct SmartCrusherConfig {
    /// Don't analyze arrays smaller than this. Default 5.
    pub min_items_to_analyze: usize,
    /// Object-only token floor: small flat objects pass through. Arrays ignore this knob and use `min_items_to_analyze` plus the adaptive-K boundary.
    pub min_tokens_to_crush: usize,
    /// Standard deviations from the mean to count as a change point.
    /// Default 2.0.
    pub variance_threshold: f64,
    /// Target maximum items in the output. Default 15.
    pub max_items_after_crush: usize,
    /// Whether to preserve detected change points. Default true.
    pub preserve_change_points: bool,
    /// Drop content-identical items before sampling. Default true.
    pub dedup_identical_items: bool,
    /// Fraction of K to allocate to the start of the array. Default 0.3.
    pub first_fraction: f64,
    /// Fraction of K to allocate to the end of the array. Default 0.15.
    pub last_fraction: f64,
    /// Items with `RelevanceScore.score >= this` are pinned by the planning methods. Mirrors
    /// Python's `RelevanceConfig.relevance_threshold`. Default 0.3 — matches the Python default.
    pub relevance_threshold: f64,
    /// Minimum byte-savings ratio (0.0..1.0) for the lossless compaction path to be chosen over lossy. If lossless saves less than this fraction,
    /// `crush_array` falls through to the lossy path (with CCR-Dropped retrieval markers). Set to `0.0` to always prefer lossless when available;
    pub lossless_min_savings_ratio: f64,
    /// Validate SmartCrusher configuration before use so invalid bounds/fractions cannot produce impossible
    /// budgets or unstable routing. Keep defaults and validation semantics aligned across constructors.
    pub advertise_retrieval_tool: bool,
    /// How `crush_array` chooses between a lossless render and a lossy-recoverable render when BOTH are available (see [`RoutingPolicy`]). ship whichever render is
    /// fewer TOKENS (both are 100% recoverable Set to [`RoutingPolicy::LosslessFirst`] to keep the legacy byte-ratio gate (used by the lossless round-trip suite
    pub routing_policy: RoutingPolicy,
    /// STRICT lossless-or-passthrough mode. When `true`, only PROVEN-lossless transforms may change the output: an array is either replaced by a decoder-verifiable,
    /// opaque-free lossless render (every row reconstructible from the output alone, no `<<ccr:` pointer of any shape) or passed through untouched.
    pub lossless_only: bool,
}

impl Default for SmartCrusherConfig {
    fn default() -> Self {
        // These defaults must match Python byte-for-byte. Lossless routing knobs without Python counterparts govern Rust dispatch only.
        SmartCrusherConfig {
            min_items_to_analyze: 5,
            min_tokens_to_crush: 200,
            variance_threshold: 2.0,
            max_items_after_crush: 15,
            preserve_change_points: true,
            dedup_identical_items: true,
            first_fraction: 0.3,
            last_fraction: 0.15,
            relevance_threshold: 0.3,
            lossless_min_savings_ratio: 0.30,
            advertise_retrieval_tool: true,
            routing_policy: RoutingPolicy::MinTokens,
            lossless_only: false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_python() {
        // Pin every default. If Python ever changes a default, this test must be updated in lockstep.
        let c = SmartCrusherConfig::default();
        assert_eq!(c.min_items_to_analyze, 5);
        assert_eq!(c.min_tokens_to_crush, 200);
        assert_eq!(c.variance_threshold, 2.0);
        assert_eq!(c.max_items_after_crush, 15);
        assert!(c.preserve_change_points);
        assert!(c.dedup_identical_items);
        assert_eq!(c.first_fraction, 0.3);
        assert_eq!(c.last_fraction, 0.15);
        assert_eq!(c.relevance_threshold, 0.3);
        assert_eq!(c.lossless_min_savings_ratio, 0.30);
        assert!(c.advertise_retrieval_tool);
        // Route-by-min-tokens is the default max-compression policy.
        assert_eq!(c.routing_policy, RoutingPolicy::MinTokens);
        // Strict lossless-or-passthrough mode is OFF by default —
        // current (lossy-recoverable) behavior unchanged.
        assert!(!c.lossless_only);
    }

    #[test]
    fn routing_policy_string_round_trips() {
        assert_eq!(
            RoutingPolicy::from_str("min-tokens"),
            Some(RoutingPolicy::MinTokens)
        );
        assert_eq!(
            RoutingPolicy::from_str("lossless-first"),
            Some(RoutingPolicy::LosslessFirst)
        );
        assert_eq!(RoutingPolicy::from_str("bogus"), None);
        assert_eq!(RoutingPolicy::MinTokens.as_str(), "min-tokens");
        assert_eq!(RoutingPolicy::LosslessFirst.as_str(), "lossless-first");
    }
}
