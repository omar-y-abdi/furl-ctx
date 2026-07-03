"""Pinning tests for `_detect_analysis_intent` keyword semantics (COR-16).

The old implementation substring-matched an extremely broad keyword set
("fix" matched *prefix*, "error" matched any mention, and fix/error/bug/
issue/problem/wrong trip on virtually every coding-agent message), so
`analysis_intent` was ~always true and SOURCE_CODE was ~never compressed
(protection 3) — a savings feature near-disabled.

Pinned here:
* matching is word-boundary, not substring ("prefix" no longer trips via
  "fix"; "preview" no longer trips via "review"; "debugger" no longer
  trips via "debug");
* the set is trimmed to genuine analysis verbs — fix/error/bug/issue/
  problem/wrong (per the finding) plus broken/improve/"clean up"/
  security/vulnerability no longer trip;
* genuine analysis verbs and analysis-question phrases still trip;
* a DEBUG log records WHICH keyword fired (the over-breadth was
  previously invisible).
"""

from __future__ import annotations

import logging

import pytest

from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig


def _router() -> ContentRouter:
    return ContentRouter(ContentRouterConfig())


def _detect(text: str) -> bool:
    return _router()._detect_analysis_intent([{"role": "user", "content": text}])


# ── word-boundary semantics ──────────────────────────────────────────────


def test_prefix_does_not_trip() -> None:
    """ "fix" is dropped AND substring hits like "prefix" no longer match."""
    assert _detect("Add a prefix to every config key.") is False


def test_preview_does_not_trip_kept_keyword_review() -> None:
    """Word boundary on a KEPT keyword: "review" must not match "preview"."""
    assert _detect("Open the preview pane.") is False


def test_debugger_does_not_trip_kept_keyword_debug() -> None:
    """Word boundary on a KEPT keyword: "debug" must not match "debugger"."""
    assert _detect("The debugger is attached to the process.") is False


def test_keyword_followed_by_punctuation_trips() -> None:
    """\\b matches at a word/punctuation edge — "debug?" still fires."""
    assert _detect("Can you debug?") is True


# ── trimmed keywords no longer trip ──────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Fix the failing test.",
        "The build printed an error yesterday.",
        "There is a bug in the login flow.",
        "Open a GitHub issue for this.",
        "No problem, continue.",
        "The wrong file was deleted.",
        "The CI badge looks broken.",
        "Improve the wording of the README.",
        "Clean up the temp directory.",
        "Add the security headers to nginx.",
        "Bump the dependency to patch the vulnerability.",
    ],
    ids=[
        "fix",
        "error",
        "bug",
        "issue",
        "problem",
        "wrong",
        "broken",
        "improve",
        "clean-up",
        "security",
        "vulnerability",
    ],
)
def test_trimmed_keywords_do_not_trip(text: str) -> None:
    assert _detect(text) is False


# ── genuine analysis verbs still trip ────────────────────────────────────


def test_analyze_this_trips() -> None:
    """The spec-mandated positive case."""
    assert _detect("analyze this") is True


@pytest.mark.parametrize(
    "text",
    [
        "Please analyze this function.",
        "Analyse the data flow.",
        "Review this code for correctness.",
        "Audit the auth module.",
        "Inspect the returned payload.",
        "Explain what this regex matches.",
        "Help me understand this callback.",
        "Debug the race condition.",
        "Refactor the parser module.",
        "Optimize the hot loop.",
        "How does the cache eviction work?",
        "What does this flag control?",
    ],
    ids=[
        "analyze",
        "analyse",
        "review",
        "audit",
        "inspect",
        "explain",
        "understand",
        "debug",
        "refactor",
        "optimize",
        "how-does",
        "what-does",
    ],
)
def test_kept_analysis_verbs_trip(text: str) -> None:
    assert _detect(text) is True


def test_matching_is_case_insensitive() -> None:
    assert _detect("REVIEW this module carefully.") is True


def test_block_format_user_content_still_scanned() -> None:
    """COR-53 concat behavior survives the regex rewrite."""
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "analyze this"}]},
    ]
    assert _router()._detect_analysis_intent(msgs) is True


def test_no_keywords_no_trip() -> None:
    assert _detect("Write a haiku about spring.") is False


# ── observability: which keyword fired ───────────────────────────────────


def test_debug_log_records_which_keyword_fired(caplog: pytest.LogCaptureFixture) -> None:
    """The over-breadth was invisible because nothing recorded WHICH
    keyword tripped. A DEBUG record must now name the matched keyword."""
    router = _router()
    msgs = [{"role": "user", "content": "Please review the diff."}]
    with caplog.at_level(logging.DEBUG, logger="furl_ctx.transforms.content_router"):
        assert router._detect_analysis_intent(msgs) is True
    debug_messages = [
        record.getMessage() for record in caplog.records if record.levelno == logging.DEBUG
    ]
    assert any("review" in message for message in debug_messages), (
        f"expected a DEBUG record naming the matched keyword 'review', got: {debug_messages!r}"
    )
