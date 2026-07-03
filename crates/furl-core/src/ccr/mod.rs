//! CCR (Compress-Cache-Retrieve) storage layer.
//!
//! When a transform compresses data with row-drop or opaque-string
//! substitution, the *original payload* is stashed here keyed by the
//! hash that ends up in the prompt. The runtime later honors retrieval
//! tool calls by looking up the hash in this store and serving back the
//! original. This is the cornerstone of CCR: lossy on the wire, lossless
//! end-to-end.
//!
//! Mirrors the semantics of Python's [`CompressionStore`] (`furl_ctx/
//! cache/compression_store.py`) but stripped down to the contract that
//! actually matters for retrieval — no BM25 search, no retrieval-event
//! feedback, no per-tool metadata. Those live in the runtime layer; this
//! crate only needs put/get.
//!
//! # Backend
//!
//! - [`backends::InMemoryCcrStore`] — process-local, sharded `DashMap`.
//!   Constructed once at startup, shared across worker threads behind an
//!   `Arc`; entries are lost on restart. CCR recovery is scoped to the
//!   process / request window (see `CCR-RETENTION.md`).
//!
//! [`CompressionStore`]: ../../../../furl_ctx/cache/compression_store.py

pub mod backends;
mod markers;
pub(crate) mod persist;

use std::time::Duration;

pub use backends::InMemoryCcrStore;
pub(crate) use markers::{
    marker_for_diff, marker_for_opaque, marker_for_row_index, marker_for_rows_offloaded,
    RetrieveUnit,
};

/// Pluggable CCR storage backend. `Send + Sync` so it can sit behind an
/// `Arc` and be shared across threads in the engine.
pub trait CcrStore: Send + Sync {
    /// Stash `payload` under `hash`. If the hash already exists, the
    /// new payload overwrites — same hash should mean same content, so
    /// re-storing is idempotent.
    fn put(&self, hash: &str, payload: &str);

    /// Look up `hash`. Returns `None` if missing or expired.
    fn get(&self, hash: &str) -> Option<String>;

    /// Number of live entries — stored AND not past TTL. Backends with
    /// lazy expiry must not count expired-but-unreaped entries (a `get`
    /// would refuse them). Informational; used by tests + telemetry.
    /// Some backends (notably Redis) cannot answer this efficiently and
    /// return 0 — see backend-specific docs.
    fn len(&self) -> usize;

    /// Capacity bound of the backend — the maximum number of live
    /// entries before eviction kicks in. Producers use this to bound
    /// how many entries a single persist may write: the SmartCrusher
    /// skips per-row granular chunking when the chunk count would flood
    /// the store and evict whole-blob entries the SAME document's
    /// markers still reference (silent-loss class COR-4).
    fn capacity(&self) -> usize;

    fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// Default capacity — matches Python's `CompressionStore` default.
pub const DEFAULT_CAPACITY: usize = 1000;

/// Default TTL — 30 minutes, matching Python's `DEFAULT_CCR_TTL_SECONDS`
/// (`furl_ctx/cache/compression_store.py`). Session-scale (Engine P0-3):
/// agentic sessions routinely outlive 5 minutes, and an entry that expires
/// mid-session silently converts "lossless + retrieval" into lossy.
pub const DEFAULT_TTL: Duration = Duration::from_secs(1800);

// CCR marker construction lives in `markers.rs` — the single
// construction point every Rust producer routes through. CCR *key
// algorithms* live in `persist.rs` — one `md5_hex_24` (diff/log/search/
// text cache keys) and one `sha6_hex12` (crusher row hashes, opaque
// prefixes), consolidated from the per-producer copies (ARCH-5) so a
// hash change can only happen in one place. Grammar and hashing remain
// deliberately separate concerns (separate modules).
