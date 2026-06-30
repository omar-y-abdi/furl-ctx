//! CCR (Compress-Cache-Retrieve) storage layer.
//!
//! When a transform compresses data with row-drop or opaque-string
//! substitution, the *original payload* is stashed here keyed by the
//! hash that ends up in the prompt. The runtime later honors retrieval
//! tool calls by looking up the hash in this store and serving back the
//! original. This is the cornerstone of CCR: lossy on the wire, lossless
//! end-to-end.
//!
//! Mirrors the semantics of Python's [`CompressionStore`] (`headroom/
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
//! [`CompressionStore`]: ../../../../headroom/cache/compression_store.py

pub mod backends;
mod markers;

use std::time::Duration;

pub use backends::InMemoryCcrStore;
pub(crate) use markers::{
    marker_for_diff, marker_for_opaque, marker_for_retrieve_more, marker_for_row_index,
    marker_for_rows_offloaded,
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

    /// Number of live entries. Informational; used by tests + telemetry.
    /// Some backends (notably Redis) cannot answer this efficiently and
    /// return 0 — see backend-specific docs.
    fn len(&self) -> usize;

    fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// Default capacity — matches Python's `CompressionStore` default.
pub const DEFAULT_CAPACITY: usize = 1000;

/// Default TTL — 5 minutes, matching Python.
pub const DEFAULT_TTL: Duration = Duration::from_secs(300);

// CCR marker construction lives in `markers.rs` — the single
// construction point every Rust producer routes through. CCR *keys* are
// computed at each producer's call site (the algorithm is per-producer
// by design: SHA-256[:6] row hashes in the crusher, SHA-256 opaque prefixes
// in the walker/formatter, MD5[:24] in the diff/log/search compressors).
// Grammar and hashing are deliberately separate concerns.
