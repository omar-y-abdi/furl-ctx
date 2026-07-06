//! Field-name hashing for preserve-field lookups.
//!
//! Rust is the SOLE owner of field-name hashing — the Python
//! `_hash_field_name` this was once ported from no longer exists (the
//! consolidation removed the Python copy; there is no parity twin to
//! keep in lockstep, only the Rust unit tests that pin the exact digest).
//! `preserve_fields` entries are anonymized field names stored as
//! **SHA-256[:8]** for privacy, so lookups will silently miss if the
//! truncation length drifts.
//!
//! # 16 vs 8 — got it wrong once, now pinned
//!
//! The first version of this file used `[:16]` based on a misread of
//! the Python source. Code review caught the discrepancy: Python uses
//! `[:8]`. Lookups against the stored 8-char hashes would have
//! silently missed every field, defeating the entire preserve-field
//! path. Fixed here; the tests now pin against Python `[:8]` reference
//! values verified via `python3 -c "...hexdigest()[:8]"`.

use sha2::{Digest, Sha256};

/// SHA-256 of the UTF-8 bytes, hex-encoded, truncated to **8** chars.
///
/// Python equivalent: `hashlib.sha256(field_name.encode()).hexdigest()[:8]`.
/// Lowercase hex — both Python `hexdigest()` and Rust's `sha2` default
/// to lowercase, so this is consistent without manual case-coercion.
pub fn hash_field_name(field_name: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(field_name.as_bytes());
    let digest = hasher.finalize();
    // Truncate to first 8 hex chars (4 bytes of digest). MUST match
    // Python's `[:8]` — see module-level note above.
    //
    // Per-byte `{:02x}` (same pattern as walker.rs/compactor.rs/crusher.rs)
    // instead of `format!("{:x}", digest)`: digest 0.11 returns
    // `hybrid_array::Array`, which does not implement `LowerHex` (the old
    // `GenericArray` did). Byte-identical output — `LowerHex` on the array
    // was zero-padded per-byte hex, exactly what `{:02x}` produces.
    digest.iter().take(4).map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_python_sha256_truncated_to_8() {
        // Verified against Python: hashlib.sha256(b"customer_id").hexdigest()[:8]
        assert_eq!(hash_field_name("customer_id"), "1e38d67d");
    }

    #[test]
    fn empty_string() {
        // Verified against Python: hashlib.sha256(b"").hexdigest()[:8]
        assert_eq!(hash_field_name(""), "e3b0c442");
    }

    #[test]
    fn unicode_field_name() {
        // Verified against Python: hashlib.sha256("café".encode()).hexdigest()[:8]
        // UTF-8 bytes for "café" are 63 61 66 c3 a9 — must encode same way.
        assert_eq!(hash_field_name("café"), "850f7dc4");
    }

    #[test]
    fn deterministic() {
        // Same input → same output across calls.
        assert_eq!(hash_field_name("test"), hash_field_name("test"));
    }

    #[test]
    fn output_length_is_8() {
        // Always exactly 8 hex chars regardless of input length.
        // This must match Python's `[:8]`; if you change it, every
        // preserve-field lookup silently misses.
        assert_eq!(hash_field_name("a").len(), 8);
        assert_eq!(hash_field_name(&"x".repeat(1000)).len(), 8);
    }
}
