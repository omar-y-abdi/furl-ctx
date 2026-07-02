"""Regression tests for parser waste-signal metric bugs #14, #15, #16.

All three are metric/diagnostic-only — compression OUTPUT is unaffected
(WasteSignals is computed after compression, only for otel diagnostics). Each
fix corrects a formula that reported a wrong number; the tests pin the CORRECT
values (derived against a length-based mock tokenizer) and assert both
directions so a fix that is newly-wrong the other way still fails.

#14: whitespace_tokens was always 0 — the normalizer did `" ".join(runs)`,
     which keeps every run intact, so it never showed any savings. Fixed to
     collapse each run to a single space.
#15: html_noise_tokens double-counted comments — the tag pattern `<[^>]+>`
     also matches a whole comment, so comments were counted as tag + comment.
     Fixed by stripping comments before matching tags.
#16: json_bloat_tokens merged several small JSON objects (separated by prose)
     into one greedy `{...}` span over the 500-char gate. Fixed by detecting
     each real JSON object span and gating each on its own token count.
"""

from __future__ import annotations

import json

import pytest

from headroom.parser import detect_waste_signals


class _LenTokenizer:
    """count_text returns character length — deterministic, mock-free metric math."""

    def count_text(self, text: str) -> int:
        return len(text)


@pytest.fixture
def tok() -> _LenTokenizer:
    return _LenTokenizer()


# ── #14: whitespace savings are non-zero ─────────────────────────────────


def test_whitespace_tokens_reports_real_savings(tok) -> None:
    # Two runs (20 + 15 chars = 35) collapse to 2 single spaces -> 33 saved.
    text = "a" + " " * 20 + "b" + " " * 15 + "c"
    assert detect_waste_signals(text, tok).whitespace_tokens == 33


def test_whitespace_tokens_zero_when_no_runs(tok) -> None:
    # No 4+ space / 3+ newline runs -> nothing to normalize.
    assert detect_waste_signals("a b c\nd", tok).whitespace_tokens == 0


# ── #15: comments counted once, tags still counted ───────────────────────


def test_html_comment_counted_once(tok) -> None:
    # The 23-char comment "<!-- simple comment -->" is counted once, not twice.
    text = "Text <!-- simple comment --> end"
    assert detect_waste_signals(text, tok).html_noise_tokens == 23


def test_plain_tag_still_counted(tok) -> None:
    # Stripping comments must NOT remove real tags.
    text = 'before <div class="x"> after'
    assert detect_waste_signals(text, tok).html_noise_tokens == len('<div class="x">')


def test_comment_and_tag_each_counted_once(tok) -> None:
    # "<!-- c -->" (10) + "<span>" (6) = 16, no double count.
    text = "a <!-- c --> b <span> c"
    assert detect_waste_signals(text, tok).html_noise_tokens == 16


# ── #16: no greedy merge across prose; real bloat still counted ───────────


def test_subthreshold_json_objects_do_not_merge(tok) -> None:
    # Three small objects separated by prose must NOT merge into one 500+ span.
    small = json.dumps({"k": "v" * 50})  # ~59 chars, individually sub-threshold
    text = f"{small} prose between {small} more prose {small}"
    assert detect_waste_signals(text, tok).json_bloat_tokens == 0


def test_single_large_json_object_counted_once(tok) -> None:
    # One genuinely large object (>500 chars) is counted exactly once.
    big = json.dumps({"data": "x" * 600})
    bloat = detect_waste_signals(big, tok).json_bloat_tokens
    assert bloat == len(big)
    assert bloat > 500
