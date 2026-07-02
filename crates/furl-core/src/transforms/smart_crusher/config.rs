//! SmartCrusher configuration.
//!
//! Direct port of `SmartCrusherConfig` at `smart_crusher.py:927-957`. The
//! defaults must match Python exactly — they're consulted everywhere
//! during compression and any drift breaks parity fixtures.

/// Policy for choosing between the lossless and lossy-recoverable
/// renderings of a compressible array when BOTH are available and both
/// are 100% recoverable.
///
/// Background: a compressible array can render two ways that lose no
/// information —
/// - **lossless**: all rows present, encoded as a CSV-schema table
///   (constant-fold / ditto / ISO-delta / dict / decimal-fold); the
///   reconstruction-aware decoder rebuilds every row from the output.
/// - **lossy-recoverable**: a visible sample of rows + a surfaced
///   `<<ccr:HASH>>` sentinel; the dropped originals live in the CCR
///   store keyed by that hash (the recovery invariant), so every dropped
///   row is retrievable from the output alone.
///
/// Both views are fully recoverable, so the choice between them costs no
/// information — it is purely a SIZE decision. Bytes mislead here (the
/// hex-vs-base64 case proves a smaller-byte render can tokenize larger),
/// so the size metric that matters is TOKENS.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RoutingPolicy {
    /// **Default.** When both a lossless render and a lossy-recoverable
    /// render exist for an array, ship the one with FEWER tokens (real
    /// tokenizer, not bytes). Ties prefer lossless (more rows visible).
    /// This is the max-compression policy: it never ships more tokens
    /// than necessary and never loses information.
    MinTokens,
    /// Legacy policy: prefer the lossless render whenever it clears the
    /// byte-savings gate (`lossless_min_savings_ratio`), even if the
    /// lossy-recoverable render would be fewer tokens. Used by the
    /// lossless round-trip suite (which asserts the lossless rendering
    /// directly) and by callers who want all rows inline regardless of
    /// token cost.
    LosslessFirst,
}

impl RoutingPolicy {
    /// Parse the policy from its kebab-case string form (the wire form
    /// the PyO3 bridge and Python config use). Returns `None` for
    /// unknown values so the caller can surface a clear error rather
    /// than silently defaulting.
    ///
    /// Deliberately an inherent `Option`-returning method (not the
    /// `FromStr` trait): the `None` arm carries the "unknown policy"
    /// signal the PyO3 boundary turns into a `ValueError`, and there is
    /// no `Err` payload to thread.
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
///
/// SCHEMA-PRESERVING: Output contains only items from the original array.
/// No wrappers, no generated text, no metadata keys. (Python comment at
/// line 930-931.)
#[derive(Debug, Clone)]
pub struct SmartCrusherConfig {
    pub enabled: bool,
    /// Don't analyze arrays smaller than this. Default 5.
    pub min_items_to_analyze: usize,
    /// Only crush content with more than this many tokens. Default 200.
    pub min_tokens_to_crush: usize,
    /// Standard deviations from the mean to count as a change point.
    /// Default 2.0.
    pub variance_threshold: f64,
    /// Below this unique-ratio, a field is treated as nearly constant.
    /// Default 0.1.
    pub uniqueness_threshold: f64,
    /// Similarity score above which strings cluster together. Default 0.8.
    pub similarity_threshold: f64,
    /// Target maximum items in the output. Default 15.
    pub max_items_after_crush: usize,
    /// Whether to preserve detected change points. Default true.
    pub preserve_change_points: bool,
    /// Factor out fields with constant values across all items. Default
    /// false (disabled — preserves original schema).
    pub factor_out_constants: bool,
    /// Include generated text summaries in output. Default false (disabled
    /// — no generated text).
    pub include_summaries: bool,
    /// Drop content-identical items before sampling. Default true.
    pub dedup_identical_items: bool,
    /// Fraction of K to allocate to the start of the array. Default 0.3.
    pub first_fraction: f64,
    /// Fraction of K to allocate to the end of the array. Default 0.15.
    pub last_fraction: f64,
    /// Items with `RelevanceScore.score >= this` are pinned by the
    /// planning methods. Mirrors Python's `RelevanceConfig.relevance_threshold`.
    /// Default 0.3 — matches the Python default.
    pub relevance_threshold: f64,
    /// Minimum byte-savings ratio (0.0..1.0) for the lossless compaction
    /// path to be chosen over lossy. Computed as
    /// `1 - len(rendered) / len(input)`. If lossless saves less than
    /// this fraction, `crush_array` falls through to the lossy path
    /// (with CCR-Dropped retrieval markers). Default `0.30`.
    ///
    /// **Override semantics.** OSS users can tune this via the config
    /// directly. Enterprise plug-ins replace the entire decision via
    /// a custom builder; the threshold is the OSS-default policy
    /// expressed as a single knob. Set to `0.0` to always prefer
    /// lossless when available; set to `1.0` to effectively disable
    /// the lossless path (lossy + CCR always).
    pub lossless_min_savings_ratio: f64,
    /// Retrieval-tool advertisement preference, mirrored from the
    /// Python `CCRConfig` (`enabled and inject_retrieval_marker`). Read
    /// back by the router layer to decide whether to inject the
    /// `furl_retrieve` TOOL into the request (the heavier behavior
    /// this flag legitimately owns).
    ///
    /// **It does NOT gate the data-loss recovery pointer.** Whenever the
    /// lossy `crush_array` path drops a distinct item, the
    /// `<<ccr:HASH N_rows_offloaded>>` pointer is surfaced AND the
    /// CCR-store write happens UNCONDITIONALLY — regardless of this flag
    /// (Defect 1). The recovery invariant ("a dropped item is
    /// recoverable from the output alone") cannot hold if the pointer is
    /// suppressed while the rows are still dropped; the pointer is the
    /// retrieval key, not a UX nicety. A flag must never turn a drop
    /// into a silent loss.
    ///
    /// Default `true`. Opaque-string CCR substitutions (in
    /// `walker::process_value`) likewise always emit their pointer.
    pub enable_ccr_marker: bool,
    /// How `crush_array` chooses between a lossless render and a
    /// lossy-recoverable render when BOTH are available (see
    /// [`RoutingPolicy`]). Default [`RoutingPolicy::MinTokens`] — the
    /// max-compression policy: ship whichever render is fewer TOKENS
    /// (both are 100% recoverable, so no information is lost). Set to
    /// [`RoutingPolicy::LosslessFirst`] to keep the legacy byte-ratio
    /// gate (used by the lossless round-trip suite, which asserts the
    /// lossless rendering directly). No Python-parity counterpart — this
    /// governs Rust-side dispatch only.
    pub routing_policy: RoutingPolicy,
    /// Entropy-floor crushability override. When `true` (default) AND a
    /// CCR store is configured, `crush_array_lossy` ignores the
    /// analyzer's no-signal SKIP verdict on near-unique data
    /// (`unique_entities_no_signal` / `medium_uniqueness_no_signal`) and
    /// crushes anyway — recoverably, via the surfaced `<<ccr:HASH>>`
    /// pointer.
    ///
    /// Why it exists: that SKIP was written for permanent-loss drops
    /// ("no signal tells us which distinct row matters → keep them all
    /// visible"). Under the CCR recovery invariant the premise is gone
    /// (every dropped row is retrievable), and the SKIP gate is
    /// NON-DETERMINISTIC on near-unique data — whether a random integer
    /// column trips a >2σ anomaly is a per-seed coin-flip, flipping the
    /// SAME shape between ~34% and ~94% reduction. The override makes the
    /// recoverable render exist DETERMINISTICALLY (it still must win the
    /// `MinTokens` token race to actually ship).
    ///
    /// Set `false` for the byte-faithful live-zone dispatcher, whose
    /// CCR marker injection is owned by its own store layer
    /// (`maybe_inject_ccr_marker`); there the crusher must not change its
    /// drop behavior based on its internal store. No Python-parity
    /// counterpart — Rust-side dispatch only; additive default-`true`
    /// so existing `compress()` output gains the fix.
    pub crush_unique_entities_when_recoverable: bool,
}

impl Default for SmartCrusherConfig {
    fn default() -> Self {
        // These defaults must match smart_crusher.py:934-957 byte-for-byte.
        // The lossless additions (`lossless_min_savings_ratio`) have no
        // Python counterpart — they govern Rust-side dispatch only.
        SmartCrusherConfig {
            enabled: true,
            min_items_to_analyze: 5,
            min_tokens_to_crush: 200,
            variance_threshold: 2.0,
            uniqueness_threshold: 0.1,
            similarity_threshold: 0.8,
            max_items_after_crush: 15,
            preserve_change_points: true,
            factor_out_constants: false,
            include_summaries: false,
            dedup_identical_items: true,
            first_fraction: 0.3,
            last_fraction: 0.15,
            relevance_threshold: 0.3,
            lossless_min_savings_ratio: 0.30,
            enable_ccr_marker: true,
            routing_policy: RoutingPolicy::MinTokens,
            crush_unique_entities_when_recoverable: true,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_python() {
        // Pin every default. Each field is consulted by some compression
        // path and a drift would break parity. If Python ever changes a
        // default, this test must be updated in lockstep.
        let c = SmartCrusherConfig::default();
        assert!(c.enabled);
        assert_eq!(c.min_items_to_analyze, 5);
        assert_eq!(c.min_tokens_to_crush, 200);
        assert_eq!(c.variance_threshold, 2.0);
        assert_eq!(c.uniqueness_threshold, 0.1);
        assert_eq!(c.similarity_threshold, 0.8);
        assert_eq!(c.max_items_after_crush, 15);
        assert!(c.preserve_change_points);
        assert!(!c.factor_out_constants);
        assert!(!c.include_summaries);
        assert!(c.dedup_identical_items);
        assert_eq!(c.first_fraction, 0.3);
        assert_eq!(c.last_fraction, 0.15);
        assert_eq!(c.relevance_threshold, 0.3);
        assert_eq!(c.lossless_min_savings_ratio, 0.30);
        assert!(c.enable_ccr_marker);
        // Route-by-min-tokens is the default max-compression policy.
        assert_eq!(c.routing_policy, RoutingPolicy::MinTokens);
        // Entropy-floor override on by default (recoverable aggressive
        // crush on near-unique data); the byte-faithful live-zone
        // dispatcher opts out explicitly.
        assert!(c.crush_unique_entities_when_recoverable);
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
