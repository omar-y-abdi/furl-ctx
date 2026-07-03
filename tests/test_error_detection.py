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

import pytest

from furl_ctx.transforms.error_detection import (
    ERROR_KEYWORDS,
    ERROR_PATTERN,
    content_has_error_indicators,
    content_has_strong_error_indicators,
    score_line,
)


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
