//! Single construction point for Rust CCR marker wire formats. Producers supply precomputed
//! hashes; this module owns marker grammar only, while hashing stays in CCR persistence helpers.

/// Recovery pointer surfaced when whole rows are offloaded to the CCR store. `<<ccr:{hash}
/// {n_rows}_rows_offloaded>>`. The hash is the whole-blob key the consumer resolves via `furl_retrieve`.
pub(crate) fn marker_for_rows_offloaded(hash: &str, n_rows: usize) -> String {
    format!("<<ccr:{hash} {n_rows}_rows_offloaded>>")
}

/// Build opaque markers as `<<ccr:{hash},{kind},{size}>>`. Replace `>` in `kind` so
/// untrusted display hints cannot terminate the consumer's bounded `>>` marker scan early.
pub(crate) fn marker_for_opaque(hash: &str, kind: &str, byte_size: usize) -> String {
    let safe_kind = kind.replace('>', "_");
    format!(
        "<<ccr:{},{},{}>>",
        hash,
        safe_kind,
        humanize_bytes(byte_size)
    )
}

/// Diff-compressor retrieval marker (no leading newline — the compressor pushes `\n`
/// separately). `[{orig} lines compressed to {comp}. Retrieve full diff: hash={hash}]`.
pub(crate) fn marker_for_diff(orig_lines: usize, comp_lines: usize, hash: &str) -> String {
    format!("[{orig_lines} lines compressed to {comp_lines}. Retrieve full diff: hash={hash}]")
}

/// Unit word carried by the `Retrieve more:` marker — which countable thing the producer reduced.
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
    /// The wire word interpolated into the marker. Byte-identical to the historical string literals
    /// (pinned by the byte-identity tests below); the consumer regex captures it as the `\w+` unit token.
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            RetrieveUnit::Lines => "lines",
            RetrieveUnit::Matches => "matches",
            RetrieveUnit::Segments => "segments",
        }
    }
}

/// Log / search / text retrieval marker (no leading newline — the compressor prepends `\n` at the call site). Retrieve more: hash={hash}]`
/// where `unit` is [`RetrieveUnit::Lines`] (log), [`RetrieveUnit::Matches`] (search) or [`RetrieveUnit::Segments`] (text crusher).
pub(crate) fn marker_for_retrieve_more(
    orig: usize,
    comp: usize,
    hash: &str,
    unit: RetrieveUnit,
) -> String {
    let unit = unit.as_str();
    format!("[{orig} {unit} compressed to {comp}. Retrieve more: hash={hash}]")
}

/// Human-readable byte size for the opaque marker's SIZE field. Shared by every opaque producer so the rendering can only be defined once.
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

    // ── Byte-identity locks ── Each test pins a family fn against the EXACT literal the producer emitted before centralization.

    #[test]
    fn rows_offloaded_is_byte_identical() {
        // Legacy row-drop marker: `<<ccr:{hash} {dropped_count}_rows_offloaded>>`.
        assert_eq!(
            marker_for_rows_offloaded("abc123def456", 7),
            "<<ccr:abc123def456 7_rows_offloaded>>"
        );
    }

    #[test]
    fn opaque_is_byte_identical() {
        // Legacy opaque marker: `<<ccr:{hash},{kind},{size}>>`.
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
        // Legacy diff marker: `[{orig} lines compressed to {comp}. Retrieve full diff: hash={key}]`.
        assert_eq!(
            marker_for_diff(120, 18, "deadbeefcafedeadbeefcafe"),
            "[120 lines compressed to 18. Retrieve full diff: hash=deadbeefcafedeadbeefcafe]"
        );
    }

    #[test]
    fn retrieve_more_is_byte_identical() {
        // Legacy log marker: `[{orig} lines compressed to {comp}. Retrieve more: hash={key}]`.
        assert_eq!(
            marker_for_retrieve_more(200, 30, "0011223344556677889900aa", RetrieveUnit::Lines),
            "[200 lines compressed to 30. Retrieve more: hash=0011223344556677889900aa]"
        );
        // Legacy search marker uses the `matches` unit.
        assert_eq!(
            marker_for_retrieve_more(12, 4, "0011223344556677889900aa", RetrieveUnit::Matches),
            "[12 matches compressed to 4. Retrieve more: hash=0011223344556677889900aa]"
        );
        // Legacy text marker uses the `segments` unit.
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

    // Opaque marker bodies must never contain `>`. Neutralizing it prevents a future
    // custom kind from creating an internal `>>` that truncates marker substitution.
    #[test]
    fn opaque_marker_neutralizes_angle_bracket_in_kind() {
        // Two adjacent '>' in `kind` is the dangerous case: unguarded, it would align with
        // DOUBLE_ANGLE_FULL_PATTERN's own `>>` terminator and truncate the substitution mid-marker.
        assert_eq!(
            marker_for_opaque("abc123def456", "weird>>injected", 512),
            "<<ccr:abc123def456,weird__injected,512B>>"
        );
        // A single stray '>' is neutralized too, not just the adjacent pair.
        assert_eq!(
            marker_for_opaque("abc123def456", "html>hack", 10),
            "<<ccr:abc123def456,html_hack,10B>>"
        );
        // The three real production kinds never contain '>', so this must
        // not change their existing byte-identical wire text.
        assert_eq!(
            marker_for_opaque("abc123def456", "base64", 2150),
            "<<ccr:abc123def456,base64,2.1KB>>"
        );
    }
}
