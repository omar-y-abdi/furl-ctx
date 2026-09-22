//! CCR stores originals removed by row-drop or opaque substitution under the exact hash emitted in prompt markers, scoped to the configured store lifetime.

pub mod in_memory;
mod markers;
pub(crate) mod persist;

use std::time::Duration;

pub use in_memory::InMemoryCcrStore;
pub(crate) use markers::{
    marker_for_diff, marker_for_opaque, marker_for_rows_offloaded, RetrieveUnit,
};

/// Pluggable CCR storage backend. `Send + Sync` so it can sit behind an
/// `Arc` and be shared across threads in the engine.
// `len` is a telemetry counter, not a container length — no `is_empty`.
#[allow(clippy::len_without_is_empty)]
pub trait CcrStore: Send + Sync {
    /// Stash `payload` under `hash`. The store is content-addressed so `hash` should uniquely determine `payload` * hash absent → the payload is stored. The
    /// binding is DROPPED (the entry is removed and the new payload refused) and the collision is logged a recoverable recompute rather than silent corruption (T3).
    fn put(&self, hash: &str, payload: &str);

    /// Look up `hash`. Returns `None` if missing or expired.
    fn get(&self, hash: &str) -> Option<String>;

    /// Number of live entries — stored AND not past TTL. Backends with lazy expiry must not count expired-but-unreaped entries (a `get` would refuse them).
    fn len(&self) -> usize;
}

/// Default capacity — matches Python's `CompressionStore` default.
pub const DEFAULT_CAPACITY: usize = 1000;

/// Default TTL is 30 minutes, matching Python. Agent sessions can exceed five minutes; expiry mid-session would break lossless retrieval.
pub const DEFAULT_TTL: Duration = Duration::from_secs(1800);

// Centralize Rust CCR marker construction here so every producer uses the same recovery-key helpers.
