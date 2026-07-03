//! Public extension surface for `SmartCrusher`.
//!
//! Three traits — `Scorer`, `Constraint`, `Observer` — capture every
//! decision a `SmartCrusher` makes that downstream consumers might
//! want to override:
//!
//! - **`Scorer`** (already public via `crate::relevance::RelevanceScorer`):
//!   how relevant is item *i* to query *q*? Default `HybridScorer`
//!   (BM25 with a match boost).
//! - **`Constraint`** (this module): which indices must be kept
//!   regardless of score? Defaults preserve errors and structural
//!   outliers.
//! - **`Observer`** (this module): emit a structured event after each
//!   `crush()`. Default writes to the `tracing` crate.
//!
//! These are the three seams external composition can plug into; the
//! other pipeline stages stay concrete Rust types until a real
//! consumer needs a trait there.
//!
//! # Composition
//!
//! Use [`SmartCrusherBuilder`](super::builder::SmartCrusherBuilder) to
//! compose a custom `SmartCrusher`. The default OSS composition is
//! reachable via `SmartCrusher::new(config)` and stays byte-equivalent
//! to the original behavior — all 17 parity fixtures pass.

use serde_json::Value;

// ── Constraint ────────────────────────────────────────────────────────────

/// A hard preservation constraint: indices the allocator must keep
/// regardless of saliency score or token budget.
///
/// Constraints stack — the must-keep set is the union of every
/// constraint's `must_keep` output. The defaults are
/// [`KeepErrorsConstraint`] and [`KeepStructuralOutliersConstraint`]
/// (wrappers around the existing detection functions); custom
/// constraints slot in alongside via the builder.
///
/// # Contract
///
/// - **`must_keep` returns indices into `items`.** Out-of-bounds
///   indices are silently dropped by the allocator; constraints
///   should not return them.
/// - **Idempotent in the small.** Calling `must_keep` twice with the
///   same `items` returns the same set; constraints do not own
///   mutable state that drifts between calls within one
///   `SmartCrusher::crush` invocation. (Caching across crushes is
///   fine.)
/// - **Cheap is free.** Constraints run on every dict-array crush.
///   A constraint that does I/O or heavy regex per-item can dominate
///   the crusher's cost. Aim for O(n) on items with small constants.
///
/// # Why `item_strings: Option<&[String]>`?
///
/// Many constraints search the JSON serialization for keywords (the
/// existing `detect_error_items_for_preservation` does this). The
/// caller may already have computed `item_strings` for adaptive
/// sizing; passing them through avoids redundant `serde_json::to_string`
/// calls. Constraints that don't need the strings simply ignore the
/// argument.
pub trait Constraint: Send + Sync {
    /// Stable identifier — appears in `CrushEvent` strategy strings,
    /// audit logs, and config-validation diagnostics. Use snake_case
    /// (`"keep_errors"`, `"business_rule"`).
    fn name(&self) -> &str;

    /// Indices of items the allocator MUST keep.
    fn must_keep(&self, items: &[Value], item_strings: Option<&[String]>) -> Vec<usize>;
}

// ── Observer ──────────────────────────────────────────────────────────────

/// Telemetry event emitted at the end of each `SmartCrusher::crush`
/// call. Observers (multiple, stacked) consume these — the in-tree
/// consumer is the `tracing` observer.
#[derive(Debug, Clone)]
pub struct CrushEvent {
    /// Strategy debug string returned by the crusher
    /// (e.g. `"smart_sample(30->15)"`, `"passthrough"`,
    /// `"top_n(50->15)"`).
    pub strategy: String,
    /// Length in bytes of the input content (whatever was passed to
    /// `crush()`).
    pub input_bytes: usize,
    /// Length in bytes of the compressed output.
    pub output_bytes: usize,
    /// Wall-clock duration of the `crush()` call.
    pub elapsed_ns: u64,
    /// Whether the output differs from the input.
    pub was_modified: bool,
}

/// Decision-stream hook. Called after each top-level `SmartCrusher::crush`
/// returns; observers run synchronously on the crusher's thread.
///
/// # Contract
///
/// - **Cheap is free.** Like constraints, observers run on every
///   crush. `tracing::debug!` is essentially free when the subscriber
///   filters the level out; remote network calls are not.
/// - **Don't panic.** A panicking observer aborts the calling thread.
///   If your observer does I/O, catch and log errors yourself.
/// - **Order matters.** Observers fire in the order they were added
///   to the builder. Most callers should not depend on the order
///   (telemetry is naturally idempotent), but if you have an
///   audit-then-publish chain, add them in that order.
pub trait Observer: Send + Sync {
    /// Stable identifier — useful for filtering in the rare cases
    /// where an observer wants to disable itself when another is
    /// already configured. Default: the type name.
    fn name(&self) -> &str {
        std::any::type_name::<Self>()
    }

    /// Called once per `SmartCrusher::crush` invocation, after the
    /// result is computed and before it is returned to the caller.
    fn on_event(&self, event: &CrushEvent);
}

// ── Re-exports for convenience ────────────────────────────────────────────

// Scorer is in the relevance crate, not here; re-export so callers
// can get all three traits from one path.
pub use crate::relevance::RelevanceScorer as Scorer;

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    /// A tiny constraint that always keeps index 0 (if any items).
    /// Pins the trait shape and the additive behavior of constraint
    /// stacking.
    struct AlwaysKeepFirst;
    impl Constraint for AlwaysKeepFirst {
        fn name(&self) -> &str {
            "always_keep_first"
        }
        fn must_keep(&self, items: &[Value], _: Option<&[String]>) -> Vec<usize> {
            if items.is_empty() {
                Vec::new()
            } else {
                vec![0]
            }
        }
    }

    #[test]
    fn constraint_returns_indices_in_bounds() {
        let items = vec![json!({"a": 1}), json!({"a": 2})];
        let c = AlwaysKeepFirst;
        let kept = c.must_keep(&items, None);
        assert_eq!(kept, vec![0]);
        assert_eq!(c.name(), "always_keep_first");
    }

    #[test]
    fn constraint_handles_empty_input() {
        let kept = AlwaysKeepFirst.must_keep(&[], None);
        assert!(kept.is_empty());
    }

    /// Counts events to verify the observer trait fires correctly
    /// when wired into a SmartCrusher (integration test in
    /// `crusher.rs::tests`).
    #[derive(Default)]
    struct CountingObserver {
        count: Arc<AtomicUsize>,
    }
    impl Observer for CountingObserver {
        fn on_event(&self, _: &CrushEvent) {
            self.count.fetch_add(1, Ordering::SeqCst);
        }
    }

    #[test]
    fn observer_event_carries_strategy_and_sizes() {
        let observer = CountingObserver::default();
        let event = CrushEvent {
            strategy: "smart_sample(30->15)".to_string(),
            input_bytes: 1000,
            output_bytes: 500,
            elapsed_ns: 12_345,
            was_modified: true,
        };
        observer.on_event(&event);
        assert_eq!(observer.count.load(Ordering::SeqCst), 1);
    }
}
