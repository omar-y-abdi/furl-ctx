//! Shared CCR key + persist+mark helpers (ARCH-5).
//!
//! One implementation of the hash/persist/marker tail the compressor
//! family used to carry as byte-identical private copies:
//!
//! * [`md5_hex_24`] — the diff/log/search/text CCR cache key
//!   (previously duplicated in all four compressor modules);
//! * [`sha6_hex12`] — the 12-char SHA-256 hex prefix behind the
//!   crusher's `hash_canonical` (row-drop recovery keys) and the
//!   compaction layer's `hash_opaque` / opaque-marker hashing
//!   (previously duplicated across `crusher.rs`, `compactor.rs`, and
//!   `walker.rs`);
//! * [`persist_and_mark`] / [`retrieve_more_marker_line`] — the
//!   key→put→marker tail of the log/search (and, marker-only, text)
//!   compressors.
//!
//! Three-plus copies is how the next hash or threshold change misses
//! one; consolidating here means a key algorithm can only change in one
//! place — and the existing round-trip / parity pins
//! (`md5_24_matches_python` below, `hash_canonical_pinned_vectors`,
//! `hash_opaque_stable_and_short`) fail loudly if it does.
//!
//! Marker GRAMMAR still lives exclusively in [`super::markers`]; this
//! module owns the *key algorithms* and the persist choreography. The
//! leading `\n` that separates a `Retrieve more:` marker line from the
//! compressed body is composed HERE (not in the grammar module), so the
//! grammar stays newline-free and composable.

// NOTE: md-5 (digest 0.10) and sha2 (digest 0.11) ride DIFFERENT digest
// trait versions in this tree — each hasher needs its own `Digest` in
// scope, imported anonymously to avoid a name clash.
use md5::{Digest as _, Md5};
use sha2::{Digest as _, Sha256};

use super::markers::{marker_for_retrieve_more, RetrieveUnit};
use super::CcrStore;

/// MD5 of `s`'s UTF-8 bytes, hex-encoded, truncated to 24 chars. Matches
/// `hashlib.md5(s.encode()).hexdigest()[:24]` from
/// `furl_ctx.cache.compression_store.CompressionStore.store` — the CCR
/// cache key the diff/log/search/text compressors embed in their
/// retrieval markers. The Python shims persist the original under this
/// exact key (`explicit_hash=`), so the marker's hash resolves in the
/// production store.
pub(crate) fn md5_hex_24(s: &str) -> String {
    let mut hasher = Md5::new();
    hasher.update(s.as_bytes());
    let digest = hasher.finalize();
    let mut hex = String::with_capacity(32);
    for b in digest {
        hex.push_str(&format!("{:02x}", b));
    }
    hex.truncate(24);
    hex
}

/// 12-char SHA-256 hex prefix (first 6 digest bytes) of `bytes`.
/// Collision-resistant enough for a single payload in flight, short
/// enough to keep markers compact. The single implementation behind the
/// crusher's `hash_canonical` (row-drop recovery keys over canonical
/// JSON) and the compaction layer's opaque-blob hashing.
pub(crate) fn sha6_hex12(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    h.finalize()
        .iter()
        .take(6)
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// Store key of the granular per-blob row index for a row-drop `hash`:
/// `"{hash}#rows"`. The index entry holds a JSON array of per-row chunk
/// hashes so retrieval is proportional (resolve the index, fetch only
/// the needed rows). Single construction point for the KEY — the
/// rendered `<<ccr:{hash}#rows {n}_chunks>>` marker interpolates the
/// same `{hash}#rows` shape via [`super::markers::marker_for_row_index`];
/// both are pinned by byte-identity tests, so key and grammar cannot
/// drift apart silently.
pub(crate) fn row_index_key(hash: &str) -> String {
    format!("{hash}#rows")
}

/// The newline-prefixed `Retrieve more:` marker line appended after a
/// compressed body. The leading `\n` lives here — NOT in the grammar
/// (`ccr::markers` stays newline-free) and NOT at each call site (where
/// it was previously duplicated).
pub(crate) fn retrieve_more_marker_line(
    original_units: usize,
    kept_units: usize,
    key: &str,
    unit: RetrieveUnit,
) -> String {
    format!(
        "\n{}",
        marker_for_retrieve_more(original_units, kept_units, key, unit)
    )
}

/// How a compressor backs the `Retrieve more:` marker it emits (PERF-8).
///
/// The FFI bridges used to synthesize a throwaway 1000-cap
/// `InMemoryCcrStore` per call purely to make the core emit a
/// `cache_key` — the core then wrote the FULL original into a store
/// that was dropped on return. `KeyOnly` makes that contract explicit:
/// key + marker are computed identically (byte-equal `cache_key`,
/// byte-equal output), and persistence is the CALLER's job.
#[derive(Clone, Copy)]
pub(crate) enum MarkerBacking<'a> {
    /// Compute the key AND persist the full original into this store.
    Store(&'a dyn CcrStore),
    /// Compute key + marker only — no store write. Used by the PyO3
    /// bridges: the Python shim re-persists the original into the
    /// production `CompressionStore` under the same key (and VETOES the
    /// compression if that write fails), so the marker never dangles.
    /// `tests/test_ccr_persist_failure_vetoes.py` pins both halves.
    KeyOnly,
    /// No CCR backing: no key, no marker
    /// (`ccr_skip_reason = "no store provided"`).
    Disabled,
}

/// The shared persist+mark tail (log/search): compute the MD5[:24] key
/// over the FULL original, persist the original under it, and return
/// `(key, marker_line)` for the caller to append/record. The store
/// write happens unconditionally here — callers run their ratio/size
/// vetoes BEFORE calling (a veto means no key, no write, no marker).
///
/// `text_crusher` deliberately does NOT use this helper: its ratio veto
/// is computed over the FINAL output (body + marker), so it must build
/// the marker first and only persist after the gate — otherwise a
/// passthrough could leave an orphan store entry. It shares
/// [`md5_hex_24`] and [`retrieve_more_marker_line`] instead.
pub(crate) fn persist_and_mark(
    store: &dyn CcrStore,
    content: &str,
    original_units: usize,
    kept_units: usize,
    unit: RetrieveUnit,
) -> (String, String) {
    let (key, marker) = key_and_mark(content, original_units, kept_units, unit);
    store.put(&key, content);
    (key, marker)
}

/// Key-only sibling of [`persist_and_mark`] (PERF-8): identical
/// `(key, marker_line)` bytes, NO store write. The caller owns
/// persistence — see [`MarkerBacking::KeyOnly`].
pub(crate) fn key_and_mark(
    content: &str,
    original_units: usize,
    kept_units: usize,
    unit: RetrieveUnit,
) -> (String, String) {
    let key = md5_hex_24(content);
    let marker = retrieve_more_marker_line(original_units, kept_units, &key, unit);
    (key, marker)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ccr::InMemoryCcrStore;

    #[test]
    fn md5_24_matches_python() {
        // Verified against Python: hashlib.md5(b"hello").hexdigest()[:24].
        // Moved verbatim from diff_compressor.rs when the four per-module
        // copies were consolidated (ARCH-5) — this pins the ONE shared
        // implementation every marker key now rides.
        assert_eq!(md5_hex_24("hello"), "5d41402abc4b2a76b9719d91");
        assert_eq!(md5_hex_24(""), "d41d8cd98f00b204e9800998");
    }

    #[test]
    fn sha6_hex12_matches_python() {
        // Verified against Python: hashlib.sha256(b"...").hexdigest()[:12].
        assert_eq!(sha6_hex12(b""), "e3b0c44298fc");
        assert_eq!(sha6_hex12(b"hello world"), "b94d27b9934d");
    }

    #[test]
    fn row_index_key_is_byte_identical_to_the_marker_interpolation() {
        // The store key and the marker grammar interpolate the same
        // `{hash}#rows` shape; this pins the key half (the marker half is
        // pinned by `markers::tests::row_index_is_byte_identical`).
        assert_eq!(row_index_key("9f3a2b"), "9f3a2b#rows");
        assert_eq!(
            crate::ccr::marker_for_row_index("9f3a2b", 50),
            format!("<<ccr:{} 50_chunks>>", row_index_key("9f3a2b"))
        );
    }

    #[test]
    fn key_and_mark_matches_persist_and_mark_without_the_write() {
        // PERF-8 byte-equality pin: the key-only tail returns the exact
        // (key, marker) bytes the persisting tail returns.
        let store = InMemoryCcrStore::new();
        let persisted = persist_and_mark(&store, "orig content", 10, 3, RetrieveUnit::Lines);
        let key_only = key_and_mark("orig content", 10, 3, RetrieveUnit::Lines);
        assert_eq!(persisted, key_only);
        assert_eq!(store.len(), 1, "persist wrote");
        let store2 = InMemoryCcrStore::new();
        let _ = key_and_mark("orig content", 10, 3, RetrieveUnit::Lines);
        assert_eq!(store2.len(), 0, "key-only never writes");
    }

    #[test]
    fn persist_and_mark_puts_key_and_composes_marker_line() {
        let store = InMemoryCcrStore::new();
        let (key, marker) = persist_and_mark(&store, "orig content", 10, 3, RetrieveUnit::Lines);
        assert_eq!(key, md5_hex_24("orig content"));
        assert_eq!(store.get(&key).as_deref(), Some("orig content"));
        assert_eq!(
            marker,
            format!("\n[10 lines compressed to 3. Retrieve more: hash={key}]")
        );
        // The line helper alone matches the composed marker (text_crusher
        // uses it without the store write).
        assert_eq!(
            retrieve_more_marker_line(10, 3, &key, RetrieveUnit::Lines),
            marker
        );
    }
}
