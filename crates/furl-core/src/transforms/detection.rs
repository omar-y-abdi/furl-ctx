//! Production detection is intentionally two-tier: parsed unified diff → `GitDiff`, otherwise
//! `PlainText`. Grep/build output stays plain text until a focused deterministic detector is justified.

use serde_json::{Map, Value};

use crate::transforms::unidiff_detector::is_diff;

/// Content-type string tags must match the Python enum; PyO3 sends the tag and Python reconstructs its enum.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ContentType {
    GitDiff,
    PlainText,
}

impl ContentType {
    /// Stable string tag — matches Python's `ContentType.<NAME>.value`.
    pub fn as_str(&self) -> &'static str {
        match self {
            ContentType::GitDiff => "diff",
            ContentType::PlainText => "text",
        }
    }
}

/// Detection result shipped across the PyO3 boundary. `metadata` uses `serde_json::Map`
/// so PyO3 can convert it to a Python dict on the boundary without losing type fidelity.
#[derive(Debug, Clone)]
pub struct DetectionResult {
    pub content_type: ContentType,
    pub confidence: f64,
    pub metadata: Map<String, Value>,
}

/// Run the detection chain on `content` and return the chosen [`ContentType`].
/// Empty input shortcuts to [`ContentType::PlainText`] without touching either tier.
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
    fn set_x_trace_with_orphaned_plus_lines_routes_to_plain_text() {
        // Regression (P0-1): `+++ cmd` lines from `set -x` nested expansion panic unidiff 0.4.0 (orphaned target header → unwrap on None).
        let trace = "+ make build\n\
                     ++ nproc\n\
                     +++ getconf _NPROCESSORS_ONLN\n\
                     + ./run_tests.sh\n";
        assert_eq!(detect(trace), ContentType::PlainText);
    }

    #[test]
    fn plain_prose_routes_to_plain_text() {
        let prose = "The quick brown fox jumps over the lazy dog. \
                     Just regular English with no special structure.";
        assert_eq!(detect(prose), ContentType::PlainText);
    }

    #[test]
    fn grep_search_results_route_to_plain_text_per_locked_design() {
        // Locked design (2026-04-25): no regex tier on Rust side, so grep-style `file:line:content` output now goes through PlainText.
        let grep = "src/foo.py:42:def process():\n\
                    src/bar.py:10:    return True\n\
                    src/baz.py:7:class Worker:\n";
        // The deterministic chain only special-cases diffs, so grep
        // output (not a diff) routes to the safe-default PlainText.
        assert_eq!(detect(grep), ContentType::PlainText);
    }

    #[test]
    fn build_log_output_routes_to_plain_text() {
        // Build/test log output has no explicit detector on the Rust side and is not a diff, so the deterministic
        // chain routes it to the safe-default PlainText passthrough rather than the degenerate GitDiff.
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
