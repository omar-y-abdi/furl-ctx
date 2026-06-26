//! ContentRouter detection chain.
//!
//! Wires the per-tier detectors into the single function the
//! ContentRouter calls. The locked design from
//! `project_rust_content_detection_arch.md`:
//!
//! ```text
//! Tier 1: unidiff::is_diff()    → if true, return GitDiff
//! Tier 2: PlainText (fallthrough)
//! ```
//!
//! # Why no regex tier
//!
//! User-locked decision (2026-04-25): the Rust side does not run the
//! regex-based [`crate::transforms::content_detector`] in production
//! detection. The regex path stays in tree as a comparison oracle for
//! parity testing and as an opt-in escape hatch, but the dispatch is
//! the unidiff parser + fallthrough only.
//!
//! # SearchResults / BuildOutput
//!
//! The retired regex detector recognized grep-style search output
//! (`file:line:`) and CMake/log output as their own [`ContentType`]
//! variants. The deterministic chain has no equivalent labels, so
//! these now route to [`ContentType::PlainText`] — we prefer
//! passthrough to misroute. If a benchmark later shows real
//! compression loss on grep/build outputs, we add a focused detector
//! for those specifically; not preemptively.

use crate::transforms::content_detector::ContentType;
use crate::transforms::unidiff_detector::is_diff;

/// Run the detection chain on `content` and return the chosen
/// [`ContentType`].
///
/// Empty input shortcuts to [`ContentType::PlainText`] without
/// touching either tier.
pub fn detect(content: &str) -> ContentType {
    if content.is_empty() {
        return ContentType::PlainText;
    }

    // ── Tier 1: unidiff parser ──────────────────────────────────
    if is_diff(content) {
        return ContentType::GitDiff;
    }

    // ── Tier 2: fallthrough ─────────────────────────────────────
    ContentType::PlainText
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_input_short_circuits_to_plain_text() {
        assert_eq!(detect(""), ContentType::PlainText);
    }

    #[test]
    fn standard_git_diff_routes_via_tier_1() {
        let diff = "diff --git a/foo.py b/foo.py\n\
                    --- a/foo.py\n\
                    +++ b/foo.py\n\
                    @@ -1,1 +1,2 @@\n \
                    def hello():\n\
                    +    print(\"new\")\n";
        // The unidiff parser (Tier 1) recognizes the patch set and
        // returns GitDiff.
        assert_eq!(detect(diff), ContentType::GitDiff);
    }

    #[test]
    fn naked_hunk_diff_routes_via_tier_1() {
        // Naked hunks (no `diff --git` wrapper) still parse as a
        // patch set, so the unidiff parser (Tier 1) catches these.
        let diff = "--- a/foo.py\n\
                    +++ b/foo.py\n\
                    @@ -1,2 +1,2 @@\n\
                    -old line\n\
                    +new line\n \
                    context line\n";
        assert_eq!(detect(diff), ContentType::GitDiff);
    }

    #[test]
    fn plain_prose_routes_to_plain_text() {
        let prose = "The quick brown fox jumps over the lazy dog. \
                     Just regular English with no special structure.";
        assert_eq!(detect(prose), ContentType::PlainText);
    }

    #[test]
    fn grep_search_results_route_to_plain_text_per_locked_design() {
        // Locked design (2026-04-25): no regex tier on Rust side, so
        // grep-style `file:line:content` output now goes through
        // PlainText. This is a deliberate behavior change vs. the
        // retired regex detector. If benchmarks later show
        // real compression loss on grep output, we add a focused
        // detector then — not preemptively.
        let grep = "src/foo.py:42:def process():\n\
                    src/bar.py:10:    return True\n\
                    src/baz.py:7:class Worker:\n";
        // The deterministic chain only special-cases diffs, so grep
        // output (not a diff) routes to the safe-default PlainText.
        assert_eq!(detect(grep), ContentType::PlainText);
    }

    #[test]
    fn build_log_output_routes_to_plain_text() {
        // Build/test log output has no explicit detector on the Rust
        // side and is not a diff, so the deterministic chain routes it
        // to the safe-default PlainText passthrough rather than a
        // degenerate type like JsonArray or GitDiff.
        let log = "[INFO] Building target foo\n\
                   [WARN] Deprecated API usage in foo.cpp:45\n\
                   [ERROR] Compilation failed: undefined reference\n";
        assert_eq!(detect(log), ContentType::PlainText);
    }

    #[test]
    fn chain_is_deterministic_across_repeated_calls() {
        // The chain is a pure function of its input, so identical
        // input yields identical output on repeated calls.
        let payload = r#"{"users": [{"id": 1}, {"id": 2}]}"#;
        let a = detect(payload);
        let b = detect(payload);
        let c = detect(payload);
        assert_eq!(a, b);
        assert_eq!(b, c);
    }
}
