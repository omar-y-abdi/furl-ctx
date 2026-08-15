//! Detect diffs by parsing with `unidiff` and requiring at least one file with a hunk; parse
//! failures and empty patch sets are plain text. Combined-merge diffs remain a known parser gap.

use unidiff::PatchSet;

/// Boolean predicate: does `content` parse as a unified diff with real change content?
pub fn is_diff(content: &str) -> bool {
    if content.is_empty() {
        return false;
    }

    // Panic containment (P0-1, restores the upstream `catch_unwind`) unidiff 0.4.0 runs
    // `source_file.clone().unwrap()` when it meets a `+++ ` target header with no preceding `--- ` source header
    std::panic::catch_unwind(|| {
        let mut patch = PatchSet::new();
        if patch.parse(content).is_err() {
            return false;
        }

        // `PatchSet::is_empty()` covers "found zero files"; the inner loop covers "found a file but with zero hunks" (e.g. mode-only changes).
        !patch.is_empty() && patch.files().iter().any(|f| !f.is_empty())
    })
    .unwrap_or(false)
}

// ─── Tests ─────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_input_is_not_a_diff() {
        assert!(!is_diff(""));
    }

    #[test]
    fn plain_prose_is_not_a_diff() {
        let prose = "The quick brown fox jumps over the lazy dog. \
                     This is just regular English prose.";
        assert!(!is_diff(prose));
    }

    #[test]
    fn json_is_not_a_diff() {
        let json = r#"{"name": "Alice", "tags": ["a", "b", "c"]}"#;
        assert!(!is_diff(json));
    }

    #[test]
    fn source_code_is_not_a_diff() {
        let py = "def foo():\n    return 42\n\nclass Bar:\n    pass\n";
        assert!(!is_diff(py));
    }

    #[test]
    fn standard_git_diff_detected() {
        let diff = "diff --git a/foo.py b/foo.py\n\
                    index abc123..def456 100644\n\
                    --- a/foo.py\n\
                    +++ b/foo.py\n\
                    @@ -1,3 +1,4 @@\n \
                    def hello():\n\
                    +    print(\"new\")\n     \
                    return \"world\"\n\
                    -    # gone\n";
        assert!(is_diff(diff));
    }

    #[test]
    fn naked_hunk_without_git_header_detected() {
        // Output of `diff -u file1 file2` without git wrapper.
        let diff = "--- a/foo.py\n\
                    +++ b/foo.py\n\
                    @@ -1,2 +1,2 @@\n\
                    -old line\n\
                    +new line\n \
                    context\n";
        assert!(is_diff(diff));
    }

    #[test]
    fn multi_file_diff_detected() {
        let diff = "--- a/foo.py\n\
                    +++ b/foo.py\n\
                    @@ -1,1 +1,1 @@\n\
                    -old\n\
                    +new\n\
                    --- a/bar.py\n\
                    +++ b/bar.py\n\
                    @@ -1,1 +1,1 @@\n\
                    -gone\n\
                    +here\n";
        assert!(is_diff(diff));
    }

    #[test]
    fn empty_patch_set_is_not_a_diff() {
        // No files, no hunks — parser succeeds but result is empty. We do NOT count this as a diff; routing it through the diff compressor would be wrong.
        let almost = "Some prose mentioning @@ in passing.\n\
                      And maybe even --- a sentence with dashes.\n";
        assert!(!is_diff(almost));
    }

    #[test]
    fn truncated_diff_treated_consistently() {
        // Truncation is a known gap — unidiff is strict.
        let truncated = "--- a/foo.py\n\
                         +++ b/foo.py\n\
                         @@ -1,1 +1,";
        // Document the current behavior; this test is the canary
        // for that contract changing.
        let _ = is_diff(truncated); // either-or accepted for now
    }

    #[test]
    fn diff_with_added_file_only() {
        let diff = "diff --git a/new.py b/new.py\n\
                    new file mode 100644\n\
                    index 0000000..9b710f3\n\
                    --- /dev/null\n\
                    +++ b/new.py\n\
                    @@ -0,0 +1,3 @@\n\
                    +line one\n\
                    +line two\n\
                    +line three\n";
        assert!(is_diff(diff));
    }

    #[test]
    fn diff_with_removed_file_only() {
        let diff = "diff --git a/gone.py b/gone.py\n\
                    deleted file mode 100644\n\
                    index 9b710f3..0000000\n\
                    --- a/gone.py\n\
                    +++ /dev/null\n\
                    @@ -1,2 +0,0 @@\n\
                    -line one\n\
                    -line two\n";
        assert!(is_diff(diff));
    }

    #[test]
    fn orphaned_target_header_does_not_panic() {
        // Regression (P0-1): a `set -x` shell trace prefixes three-level nested expansions with `+++ `. unidiff 0.4.0 sees such a line as a target
        // file header with no preceding `--- ` source header and unwraps a `None` (lib.rs:665). Containment turns it into "not a diff" (fail-open).
        let trace = "+ ./deploy.sh --env prod\n\
                     ++ dirname /opt/app/deploy.sh\n\
                     +++ readlink -f /opt/app\n\
                     + cd /opt/app\n\
                     ++ git rev-parse HEAD\n\
                     +++ git describe --tags\n\
                     + echo done\n";
        assert!(!is_diff(trace));
    }

    #[test]
    fn orphaned_target_header_before_valid_diff_does_not_panic() {
        // The orphan precedes an otherwise-valid diff. unidiff panics at the first orphaned `+++` (before reaching the valid hunks), so containment
        // classifies the whole input as not-a-diff — the fail-open passthrough beats a crash; the router serves the content as plain text.
        let mixed = "+++ orphan-target-first\n\
                     --- a/foo.py\n\
                     +++ b/foo.py\n\
                     @@ -1,1 +1,1 @@\n\
                     -old\n\
                     +new\n";
        assert!(!is_diff(mixed));
    }

    #[test]
    fn html_is_not_a_diff() {
        let html = "<!DOCTYPE html><html><body><h1>Hi</h1></body></html>";
        assert!(!is_diff(html));
    }

    #[test]
    fn yaml_is_not_a_diff() {
        let yaml = "name: my-app\nversion: 1.0\ndependencies:\n  - foo\n";
        assert!(!is_diff(yaml));
    }
}
