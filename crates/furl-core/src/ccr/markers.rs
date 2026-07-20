//! CCR marker grammar — the single construction point for every CCR
//! marker emitted by the Rust engine.
//!
//! Every Rust producer (smart-crusher row-drop + opaque substitution,
//! diff/log/search compressors) routes its final marker string through
//! one of the `marker_for_*` functions here. Centralizing the grammar in
//! one module means the wire format is defined in exactly one place: the
//! consumer-side parser (`furl_ctx/ccr/tool_injection.py`) has a single
//! Rust counterpart to stay in lockstep with, and a marker shape can only
//! change by editing this file.
//!
//! Hashing lives in the sibling `ccr::persist` module: producers compute
//! their key there (`sha256_recovery_key` row/opaque hashes, `md5_hex_24`
//! diff/log/search/text keys) and hand the finished `hash` string in.
//! This module owns the *grammar*, not the *hash algorithm* — the two
//! are deliberately separate so producers keep their existing keys
//! byte-for-byte.
//!
//! # Marker shapes
//!
//! | Fn                          | Shape                                                                 |
//! |-----------------------------|-----------------------------------------------------------------------|
//! | `marker_for_rows_offloaded` | `<<ccr:{hash} {n}_rows_offloaded>>`                                    |
//! | `marker_for_row_index`      | `<<ccr:{hash}#rows {n}_chunks>>`                                       |
//! | `marker_for_opaque`         | `<<ccr:{hash},{kind},{size}>>`                                         |
//! | `marker_for_diff`           | `[{orig} lines compressed to {comp}. Retrieve full diff: hash={h}]`   |
//! | `marker_for_retrieve_more`  | `[{orig} {unit} compressed to {comp}. Retrieve more: hash={h}]`       |
//!
//! The diff/log/search markers do NOT carry a leading newline: the
//! compressors that emit them prepend the `\n` separately at the call
//! site (so the marker text itself stays composable). See each call
//! site for the exact concatenation.

/// Recovery pointer surfaced when whole rows are offloaded to the CCR
/// store. `<<ccr:{hash} {n_rows}_rows_offloaded>>`. The hash is the
/// whole-blob key the consumer resolves via `furl_retrieve`.
pub(crate) fn marker_for_rows_offloaded(hash: &str, n_rows: usize) -> String {
    format!("<<ccr:{hash} {n_rows}_rows_offloaded>>")
}

/// Granular row-index pointer. `<<ccr:{hash}#rows {n_chunks}_chunks>>`.
/// The `{hash}#rows` index key resolves to a JSON array of per-row
/// hashes so the consumer can address each dropped row independently.
pub(crate) fn marker_for_row_index(hash: &str, n_chunks: usize) -> String {
    format!("<<ccr:{hash}#rows {n_chunks}_chunks>>")
}

/// Opaque-blob substitution marker. `<<ccr:{hash},{kind},{size}>>` where
/// `kind` is the pre-resolved wire string (`base64` / `string` / `html`
/// / custom) and `byte_size` is the original payload length in bytes,
/// rendered human-readable (`123B`, `4.5KB`, `1.2MB`). Used by both the
/// walker (live substitution) and the CSV/KV formatters (rendering an
/// already-classified opaque cell).
pub(crate) fn marker_for_opaque(hash: &str, kind: &str, byte_size: usize) -> String {
    format!("<<ccr:{},{},{}>>", hash, kind, humanize_bytes(byte_size))
}

/// Diff-compressor retrieval marker (no leading newline — the compressor
/// pushes `\n` separately). `[{orig} lines compressed to {comp}.
/// Retrieve full diff: hash={hash}]`.
pub(crate) fn marker_for_diff(orig_lines: usize, comp_lines: usize, hash: &str) -> String {
    format!("[{orig_lines} lines compressed to {comp_lines}. Retrieve full diff: hash={hash}]")
}

/// Unit word carried by the `Retrieve more:` marker — which countable
/// thing the producer reduced. One variant per producer family; an
/// invalid unit is unrepresentable (TYPE-5), so a producer typo can no
/// longer silently change the wire text the Python consumer grammar
/// (shape H, `marker_grammar.BRACKET_RETRIEVE_PATTERN`) matches on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RetrieveUnit {
    /// Log compressor: whole log lines.
    Lines,
    /// Search compressor: grep/ripgrep matches.
    Matches,
    /// Text crusher: prose/paragraph segments.
    Segments,
}

impl RetrieveUnit {
    /// The wire word interpolated into the marker. Byte-identical to the
    /// historical string literals (pinned by the byte-identity tests
    /// below); the consumer regex captures it as the `\w+` unit token.
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            RetrieveUnit::Lines => "lines",
            RetrieveUnit::Matches => "matches",
            RetrieveUnit::Segments => "segments",
        }
    }
}

/// Log / search / text retrieval marker (no leading newline — the
/// compressor prepends `\n` at the call site). `[{orig} {unit}
/// compressed to {comp}. Retrieve more: hash={hash}]` where `unit` is
/// [`RetrieveUnit::Lines`] (log), [`RetrieveUnit::Matches`] (search) or
/// [`RetrieveUnit::Segments`] (text crusher).
pub(crate) fn marker_for_retrieve_more(
    orig: usize,
    comp: usize,
    hash: &str,
    unit: RetrieveUnit,
) -> String {
    let unit = unit.as_str();
    format!("[{orig} {unit} compressed to {comp}. Retrieve more: hash={hash}]")
}

/// Human-readable byte size for the opaque marker's SIZE field. Shared
/// by every opaque producer so the rendering can only be defined once.
/// `<1024 → "{n}B"`, `<1024KB → "{kb:.1}KB"`, else `"{mb:.1}MB"`.
pub(crate) fn humanize_bytes(n: usize) -> String {
    if n < 1024 {
        return format!("{n}B");
    }
    let kb = n as f64 / 1024.0;
    if kb < 1024.0 {
        return format!("{kb:.1}KB");
    }
    let mb = kb / 1024.0;
    format!("{mb:.1}MB")
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── Byte-identity locks ──
    // Each test pins a family fn against the EXACT literal the producer
    // emitted before centralization. If a shape ever drifts by one byte,
    // these fail — and so would the Python consumer parser + CCR recovery.

    #[test]
    fn rows_offloaded_is_byte_identical() {
        // crusher.rs old: format!("<<ccr:{hash} {dropped_count}_rows_offloaded>>")
        assert_eq!(
            marker_for_rows_offloaded("abc123def456", 7),
            "<<ccr:abc123def456 7_rows_offloaded>>"
        );
    }

    #[test]
    fn row_index_is_byte_identical() {
        // crusher.rs old: index_key = "{hash}#rows";
        //                 format!("<<ccr:{index_key} {dropped_count}_chunks>>")
        assert_eq!(
            marker_for_row_index("9f3a2b", 50),
            "<<ccr:9f3a2b#rows 50_chunks>>"
        );
    }

    #[test]
    fn opaque_is_byte_identical() {
        // walker.rs / formatter.rs old: format!("<<ccr:{},{},{}>>", hash, kind_str, humanize(len))
        assert_eq!(
            marker_for_opaque("abc123def456", "base64", 2150),
            "<<ccr:abc123def456,base64,2.1KB>>"
        );
        // Small payload → bytes; custom kind passes through verbatim.
        assert_eq!(
            marker_for_opaque("ff00ff00ff00", "html", 512),
            "<<ccr:ff00ff00ff00,html,512B>>"
        );
    }

    #[test]
    fn diff_is_byte_identical() {
        // diff_compressor.rs old:
        // format!("[{} lines compressed to {}. Retrieve full diff: hash={}]", orig, comp, key)
        assert_eq!(
            marker_for_diff(120, 18, "deadbeefcafedeadbeefcafe"),
            "[120 lines compressed to 18. Retrieve full diff: hash=deadbeefcafedeadbeefcafe]"
        );
    }

    #[test]
    fn retrieve_more_is_byte_identical() {
        // log_compressor.rs old (sans the \n the call site prepends):
        // "[{} lines compressed to {}. Retrieve more: hash={}]"
        assert_eq!(
            marker_for_retrieve_more(200, 30, "0011223344556677889900aa", RetrieveUnit::Lines),
            "[200 lines compressed to 30. Retrieve more: hash=0011223344556677889900aa]"
        );
        // search_compressor.rs old (unit = "matches"):
        assert_eq!(
            marker_for_retrieve_more(12, 4, "0011223344556677889900aa", RetrieveUnit::Matches),
            "[12 matches compressed to 4. Retrieve more: hash=0011223344556677889900aa]"
        );
        // text_crusher.rs old (unit = "segments"):
        assert_eq!(
            marker_for_retrieve_more(40, 9, "0011223344556677889900aa", RetrieveUnit::Segments),
            "[40 segments compressed to 9. Retrieve more: hash=0011223344556677889900aa]"
        );
    }

    #[test]
    fn retrieve_unit_wire_words_are_pinned() {
        // The unit vocabulary is FFI-visible marker text the Python
        // consumer grammar tokenizes — pin every variant's exact bytes.
        assert_eq!(RetrieveUnit::Lines.as_str(), "lines");
        assert_eq!(RetrieveUnit::Matches.as_str(), "matches");
        assert_eq!(RetrieveUnit::Segments.as_str(), "segments");
    }

    #[test]
    fn humanize_bytes_covers_every_branch() {
        // walker::humanize + formatter::humanize_bytes collapsed into one.
        assert_eq!(humanize_bytes(512), "512B"); // < 1024 → bytes
        assert_eq!(humanize_bytes(1023), "1023B"); // boundary, still bytes
        assert_eq!(humanize_bytes(2048), "2.0KB"); // KB branch
        assert_eq!(humanize_bytes(2150), "2.1KB"); // KB rounding
        assert_eq!(humanize_bytes(5 * 1024 * 1024), "5.0MB"); // MB branch
    }
}
