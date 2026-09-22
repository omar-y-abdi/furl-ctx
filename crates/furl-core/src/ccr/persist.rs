//! Shared CCR key and persistence helpers. MD5-24 keys back diff/log/search/text; SHA-256
//! prefixes back row and opaque recovery. Marker grammar remains centralized separately.

// NOTE: md-5 and sha2 both ride digest 0.11 in this tree, so both re-export the *same* `digest::Digest` trait.
use md5::{Digest as _, Md5};
use sha2::Sha256;

use super::markers::{marker_for_retrieve_more, RetrieveUnit};
use super::CcrStore;

/// Return the first 24 hex chars of MD5(UTF-8), matching Python CCR keys. Python persists originals under this exact hash so emitted markers resolve.
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

/// Number of hex characters in a CCR recovery key: 24 hex = 96 bits. A 48-bit (12-hex) key collided by the birthday
/// bound after ~2^24 distinct payloads, which let one dropped row silently recover as another row's content (T3).
pub(crate) const CCR_KEY_HEX_WIDTH: usize = 24;

/// `SHA-256(bytes)` truncated to [`CCR_KEY_HEX_WIDTH`] hex chars (the leading `CCR_KEY_HEX_WIDTH / 2` digest bytes).
pub(crate) fn sha256_recovery_key(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    h.finalize()
        .iter()
        .take(CCR_KEY_HEX_WIDTH / 2)
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// The newline-prefixed `Retrieve more:` marker line appended after a compressed body.
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

/// How a compressor backs the `Retrieve more:` marker it emits (PERF-8). `KeyOnly` makes that contract explicit: key
/// + marker are computed identically (byte-equal `cache_key`, byte-equal output), and persistence is the CALLER's job.
#[derive(Clone, Copy)]
pub(crate) enum MarkerBacking<'a> {
    /// Compute the key AND persist the full original into this store.
    Store(&'a dyn CcrStore),
    /// Compute key + marker only — no store write. Used by the PyO3 bridges: the Python shim re-persists the original into the
    /// production `CompressionStore` under the same key (and VETOES the compression if that write fails), so the marker never dangles.
    KeyOnly,
    /// No CCR backing: no key, no marker
    /// (`ccr_skip_reason = "no store provided"`).
    Disabled,
}

/// The shared persist+mark tail (log/search) The store write happens unconditionally here
/// callers run their ratio/size vetoes BEFORE calling (a veto means no key, no write, no marker).
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

/// Key-only sibling of [`persist_and_mark`] (PERF-8): identical `(key, marker_line)`
/// bytes, NO store write. The caller owns persistence — see [`MarkerBacking::KeyOnly`].
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
        // Verified against Python: hashlib.md5(b"hello").hexdigest()[:24]. Moved verbatim from the Rust module when the four
        // per-module copies were consolidated (ARCH-5) — this pins the ONE shared implementation every marker key now rides.
        assert_eq!(md5_hex_24("hello"), "5d41402abc4b2a76b9719d91");
        assert_eq!(md5_hex_24(""), "d41d8cd98f00b204e9800998");
    }

    #[test]
    fn sha256_recovery_key_matches_python() {
        // Verified against Python: hashlib.sha256(b"...").hexdigest()[:24].
        assert_eq!(sha256_recovery_key(b""), "e3b0c44298fc1c149afbf4c8");
        assert_eq!(
            sha256_recovery_key(b"hello world"),
            "b94d27b9934d3e08a52e52d7"
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
