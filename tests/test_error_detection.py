"""TEST-25: furl_ctx.transforms.error_detection had zero direct tests.

The module is the Python shim over the Rust keyword/scoring engine and is
invoked on the live routing path (``router_message_policy`` gates
compression-protection on ``content_has_strong_error_indicators``). These
tests pin the shim's own contracts: the ValueError translation for unknown
contexts, the delegation results, and the two Rust-side bug fixes the
module docstring records (timeout-family regex coverage; ``token`` removed
from the security keywords).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from furl_ctx.transforms.error_detection import (
    ERROR_KEYWORDS,
    ERROR_PATTERN,
    content_has_error_indicators,
    content_has_strong_error_indicators,
    score_line,
)
from furl_ctx.transforms.router_message_policy import _is_unstructured_error_output


class TestScoreLine:
    def test_error_line_scores_error_category(self) -> None:
        category, priority, confidence = score_line("ERROR: connection refused")
        assert category == "error"
        assert priority > 0.0
        assert 0.0 < confidence <= 1.0

    def test_plain_line_scores_no_category(self) -> None:
        category, priority, _confidence = score_line("just an ordinary sentence")
        assert category is None
        assert priority == 0.0

    def test_unknown_context_raises_valueerror(self) -> None:
        # The Rust binding returns None for unknown contexts (pyo3/clippy
        # workaround); the shim owns translating that into the explicit
        # error every caller expects. This is the module's ONE error path.
        with pytest.raises(ValueError, match="unknown importance context: banana"):
            score_line("ERROR: whatever", context="banana")


class TestErrorIndicators:
    def test_error_content_flags(self) -> None:
        assert content_has_error_indicators("Traceback (most recent call last):")

    def test_clean_content_does_not_flag(self) -> None:
        assert not content_has_error_indicators("all systems nominal, 200 OK-ish prose")

    def test_strong_indicator_gate_is_stricter_than_weak(self) -> None:
        # The strong gate protects compression decisions; a line that only
        # trips the weak scan must not trip the strong one.
        weak_only = "the deploy timeout window is configurable"
        assert not content_has_strong_error_indicators(weak_only)
        assert content_has_strong_error_indicators("FATAL: process crashed\nTraceback")


class TestUnstructuredErrorOutputBoundary:
    """Boundary pins for ``_is_unstructured_error_output`` — the tool-message
    gate that ships a raw error dump verbatim (``router:protected:error_output``)
    rather than compressing it.

    Substring matching is LOAD-BEARING: a Python exception carries its failure
    kind only as a SUBSTRING (``ValueError`` contains ``error``; ``\\berror\\b``
    does not match it), so a word-boundary "tightening" would blind the gate to
    real tracebacks. These pins lock both the genuine-error positives and the
    benign negatives the ``>= 2 distinct indicators`` threshold screens out, so
    any future change to either becomes visible.
    """

    # --- MUST still protect: genuine unstructured error output ---------------
    def test_python_traceback_is_protected(self) -> None:
        tb = (
            "Traceback (most recent call last):\n"
            "  File 'app.py', line 42, in run\n"
            "    raise ValueError('bad input')\n"
            "ValueError: bad input"
        )
        assert _is_unstructured_error_output(tb)

    def test_valueerror_substring_is_load_bearing(self) -> None:
        # A word-boundary match would MISS this (``\\berror\\b`` does not match
        # "ValueError"); substring matching is exactly why the gate sees it.
        # Paired with a second distinct indicator to clear the strict threshold.
        assert _is_unstructured_error_output("ValueError raised\nprocess failed midway")

    # --- MUST NOT protect: benign content that merely mentions errors --------
    def test_brief_example_fixed_error_handling_not_protected(self) -> None:
        # The brief's exact worry: a changelog line naming a fix + error
        # handling. Only ONE distinct indicator (``error``) → the >= 2 threshold
        # already screens it out. The reassuring result.
        assert not _is_unstructured_error_output("Fixed: better error handling in the parser")

    def test_benign_single_error_mention_not_protected(self) -> None:
        assert not _is_unstructured_error_output("The report shows zero errors across all runs.")

    def test_json_errors_field_not_protected(self) -> None:
        # Structured JSON is never a traceback — the JSON guard excludes it even
        # though ``error`` and ``fail`` both appear as substrings.
        assert not _is_unstructured_error_output('{"errors": [], "failed": 0, "status": "ok"}')

    def test_repo_changelog_head_not_protected(self) -> None:
        head = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")[
            :2179
        ]
        assert not _is_unstructured_error_output(head)

    # --- KNOWN, bounded over-protection (characterization — see PR report) ---
    def test_known_fp_changelog_prose_with_two_indicators(self) -> None:
        # Documented limitation reproducing the symptom the evaluator saw once:
        # release-note PROSE pairing two DISTINCT error substrings (``error`` in
        # "error handling" + ``fail`` in "failed") clears the >= 2 gate and is
        # protected. Deliberately NOT tightened: the only signal separating this
        # from a real dump is structural, and every keyword-level narrowing that
        # drops this case also blinds the gate to genuine tracebacks (see
        # ``test_valueerror_substring_is_load_bearing``). The cost is bounded —
        # protection ships the bytes verbatim (no data loss) and now surfaces as
        # an EXPLAINED ``router:protected:error_output`` transform, not a silent
        # 0%. This pin is ONE-directional: it fails only when the FP is fixed
        # (a structural distinguisher lands), forcing that fix to be a
        # deliberate flip to a negative assertion — it does NOT guard against
        # new false positives elsewhere.
        changelog_prose = (
            "## v1.2.0\n"
            "- Improved error handling for upstream timeouts\n"
            "- The cache failed to evict under load; now fixed"
        )
        assert _is_unstructured_error_output(changelog_prose)


class TestReExportedTables:
    """The legacy names re-exported from the Rust registry snapshot."""

    def test_error_keywords_is_nonempty_frozenset(self) -> None:
        assert isinstance(ERROR_KEYWORDS, frozenset)
        assert "error" in ERROR_KEYWORDS

    def test_timeout_family_present_in_both_keywords_and_pattern(self) -> None:
        # The documented Rust-side bug fix: ERROR_KEYWORDS listed
        # timeout/abort/denied/rejected but the old regex omitted them.
        # Pin that keywords and the recompiled pattern agree on the family.
        for word in ("timeout", "abort", "denied", "rejected"):
            assert word in ERROR_KEYWORDS, f"{word} missing from ERROR_KEYWORDS"
            assert ERROR_PATTERN.search(f"FATAL: {word} connecting upstream"), (
                f"ERROR_PATTERN must match the {word} family (documented fix)"
            )

    def test_pattern_respects_word_boundaries(self) -> None:
        # `\b`-anchored: substrings inside larger identifiers must not match.
        assert not ERROR_PATTERN.search("the timeouts_config_v2 identifier")
        assert ERROR_PATTERN.search("request denied by policy")
