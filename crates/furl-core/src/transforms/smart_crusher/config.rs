//! SmartCrusher configuration.
//!
//! Direct port of `SmartCrusherConfig` at `smart_crusher.py:927-957`. The
//! defaults must match Python exactly — they're consulted everywhere
//! during compression and any drift breaks parity fixtures.
//!
//! Four historical knobs (`enabled`, `uniqueness_threshold`,
//! `similarity_threshold`, `include_summaries`) were read by ZERO core
//! paths and were deleted in lockstep across Rust + PyO3 kwargs + the
//! Python dataclass (SIMP-7 wire-contract): passing them now fails loud
//! with a `TypeError` at either boundary instead of silently doing
//! nothing.

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
/// SCHEMA-PRESERVING: kept items ship byte-shaped as they arrived — no
/// wrappers and no generated prose. Two DOCUMENTED sentinel keys are the
/// exception: the lossy path appends the `{"_ccr_dropped": ..}` recovery
/// pointer object (plus `_ccr_rows` when granular chunks exist), and
/// dedup may annotate a kept representative with `_dup_count`.
#[derive(Debug, Clone)]
pub struct SmartCrusherConfig {
    /// Don't analyze arrays smaller than this. Default 5.
    pub min_items_to_analyze: usize,
    /// Floor for OBJECT key-crush only: `crush_object` passes a flat
    /// object through when its estimated token total is below this
    /// (`crushers.rs`). ARRAY compression IGNORES this knob — array
    /// gating is `min_items_to_analyze` + the adaptive-K boundary (the
    /// historical "only crush content with more than this many tokens"
    /// wording over-promised — SIMP-7). Default 200.
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
    ///
    /// Renamed from `enable_ccr_marker` (which invited reading it as a
    /// marker/persist gate — it never was). The Python FFI kwarg
    /// `enable_ccr_marker` is accepted as a deprecation alias for one
    /// release (see `crates/furl-py/src/lib.rs`).
    pub advertise_retrieval_tool: bool,
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
    /// STRICT lossless-or-passthrough mode. Default `false`.
    ///
    /// When `true`, only PROVEN-lossless transforms may change the
    /// output: an array is either replaced by a decoder-verifiable,
    /// opaque-free lossless render (every row reconstructible from the
    /// output alone, no `<<ccr:` pointer of any shape) or passed through
    /// untouched. Lossy-recoverable candidates are NEVER BUILT — no row
    /// drops, no `_ccr_dropped` sentinels, no opaque-blob substitution,
    /// no string/number/mixed-array sampling, no object key-crush — so
    /// no CCR store write happens on this path either (nothing is
    /// dropped, so there is nothing to recover).
    ///
    /// For users who cannot tolerate ANY visible information reduction,
    /// even a CCR-recoverable one. The recovery invariant is trivially
    /// preserved: it constrains drops, and this mode has none.
    pub lossless_only: bool,
}

impl Default for SmartCrusherConfig {
    fn default() -> Self {
        // These defaults must match smart_crusher.py:934-957 byte-for-byte.
        // The lossless additions (`lossless_min_savings_ratio`) have no
        // Python counterpart — they govern Rust-side dispatch only.
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
        // Pin every default. Each field is consulted by some compression
        // path and a drift would break parity. If Python ever changes a
        // default, this test must be updated in lockstep.
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
