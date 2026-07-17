"""RG1: agent-supplied regexes must never wedge the process.

The A12 quantifier cap is SYNTACTIC and cannot bound backtracking. The
counter-example is ``(a|b|ab)+Z``: no nested quantifier, no optional chain, 10
characters long -- every parse-time screen passes it, and it still backtracks for
minutes against an 80-character ambiguous line. These pin that a real wall-clock
budget bounds every agent-supplied-regex search path instead.

Fail-before/pass-after: against d9c6691c semantics (``pattern.search(text)`` with
no budget) each adversarial case below runs for minutes;
``test_screens_alone_do_not_reject_the_adversarial_patterns`` documents WHY the
screens cannot be the fix.
"""

from __future__ import annotations

import re
import signal
import threading
import time

import pytest

import furl_ctx.ccr.regex_budget as regex_budget
from furl_ctx.ccr.compress_modes import (
    _MAX_VARIABLE_QUANTIFIERS,
    _NESTED_QUANTIFIER_RE,
    SectionPatterns,
    _resolve_matcher,
    count_variable_quantifiers,
)
from furl_ctx.ccr.regex_budget import (
    MatchVerdict,
    matches_within_budget,
    re2_available,
    search_within_budget,
)
from furl_ctx.ccr.retrieve_filters import _line_matches

# Each pattern is agent-supplyable and passes the parse-time screens (or, for
# ``(a+)+Z``, is the classic shape the screens DO catch -- pinned here so the
# budget holds even if a future screen change lets it through). The line is >5k
# chars and adversarial for that pattern: ambiguous ``ab`` repeats for the
# alternation case, a pure ``a`` run for the ``a+`` chains.
#
# Every pattern ends in ``Z`` that the line does NOT contain, so the true answer
# is always "no match" and the engine must exhaust its search to prove it -- that
# is what makes them catastrophic. (A bare ``"a+" * 30`` against an all-``a`` line
# is NOT a zero-match case: such a line genuinely matches, and RE2 says so in
# microseconds. The trailing ``Z`` is what forces the failing search.)
# Verified fail-before at d9c6691c semantics: each of these runs >6s under a
# plain unbudgeted ``pattern.search(text)``.
ADVERSARIAL: list[tuple[str, str]] = [
    ("(a|b|ab)+Z", "ab" * 2600),  # ambiguous alternation under one +
    ("a+" * 30 + "Z", "a" * 5200),  # flat + chain, no nesting, no ?/*
    ("(a+)+Z", "a" * 5200),  # classic nested quantifier
]

# Well under 2s per the RG1 requirement, and far above the ~100ms budget so a
# loaded CI box does not flake.
_CEILING_SECONDS = 2.0


@pytest.mark.parametrize("pattern,text", ADVERSARIAL, ids=lambda v: v[:14])
def test_budgeted_search_bounds_catastrophic_pattern(pattern: str, text: str) -> None:
    """The budget bounds each adversarial pattern; unbudgeted these hang."""
    compiled = re.compile(pattern)
    start = time.monotonic()
    verdict = search_within_budget(compiled, text)
    elapsed = time.monotonic() - start
    assert elapsed < _CEILING_SECONDS, f"{pattern!r} took {elapsed:.2f}s; the budget did not hold"
    # Either the engine proved no match (RE2) or the budget cut it off. Never a
    # false MATCH -- none of these patterns actually matches its line.
    assert verdict is not MatchVerdict.MATCH


@pytest.mark.parametrize("pattern,text", ADVERSARIAL, ids=lambda v: v[:14])
def test_retrieve_filter_line_match_is_bounded(pattern: str, text: str) -> None:
    """RG1's first reachable path: ``furl_retrieve``'s agent-supplied pattern."""
    start = time.monotonic()
    matched = _line_matches(re.compile(pattern), None, text)
    elapsed = time.monotonic() - start
    assert elapsed < _CEILING_SECONDS, f"{pattern!r} took {elapsed:.2f}s"
    assert matched is False


@pytest.mark.parametrize("pattern,text", ADVERSARIAL, ids=lambda v: v[:14])
def test_compress_filter_matcher_is_bounded(pattern: str, text: str) -> None:
    """RG1's second reachable path: ``furl_compress``'s include/exclude filters."""
    matcher = _resolve_matcher(pattern)
    start = time.monotonic()
    matched = matcher(text)
    elapsed = time.monotonic() - start
    assert elapsed < _CEILING_SECONDS, f"{pattern!r} took {elapsed:.2f}s"
    assert matched is False


@pytest.mark.parametrize("pattern,text", ADVERSARIAL, ids=lambda v: v[:14])
def test_line_eligibility_is_bounded(pattern: str, text: str) -> None:
    """The per-line eligibility hot loop is bounded for both filter kinds."""
    start = time.monotonic()
    SectionPatterns(include=(pattern,), exclude=()).line_is_eligible(text)
    SectionPatterns(include=(), exclude=(pattern,)).line_is_eligible(text)
    elapsed = time.monotonic() - start
    assert elapsed < _CEILING_SECONDS


def test_budget_is_enforced_off_the_main_thread() -> None:
    """The MCP server runs these matches in a worker thread (asyncio.to_thread).

    A SIGALRM watchdog cannot fire there, so this is the case that decided the
    engine choice: without RE2 the fallback is unbudgeted, so this test asserts
    the bound only where the guarantee actually exists.
    """
    if not re2_available():
        pytest.skip("no RE2: the worker-thread path is unbudgeted by design (see module docs)")
    result: dict[str, object] = {}

    def run() -> None:
        start = time.monotonic()
        result["verdict"] = search_within_budget(re.compile("(a|b|ab)+Z"), "ab" * 2600)
        result["elapsed"] = time.monotonic() - start

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=_CEILING_SECONDS * 2)
    assert not worker.is_alive(), "worker thread wedged: the budget did not hold off-main-thread"
    assert result["verdict"] is not MatchVerdict.MATCH


@pytest.fixture
def without_re2(monkeypatch: pytest.MonkeyPatch):
    """Force the stdlib fallback path, as on an install without the re2 extra."""
    monkeypatch.setattr(regex_budget, "_RE2", None)
    regex_budget._compile_re2.cache_clear()
    yield
    regex_budget._compile_re2.cache_clear()


@pytest.mark.parametrize("pattern,text", ADVERSARIAL, ids=lambda v: v[:14])
def test_sigalrm_fallback_bounds_catastrophic_pattern(
    pattern: str, text: str, without_re2: None
) -> None:
    """The graceful fallback must ALSO hold: no RE2, main thread, SIGALRM budget.

    This is the path a plain ``pip install furl-ctx`` takes on the CLI. Without
    it the "optional dependency" story would be a hang for anyone who skipped the
    extra, so it is pinned independently of RE2 being importable.
    """
    assert not regex_budget.re2_available(), "precondition: the RE2 path is disabled"
    start = time.monotonic()
    verdict = search_within_budget(re.compile(pattern), text)
    elapsed = time.monotonic() - start
    assert verdict is MatchVerdict.BUDGET_EXCEEDED, "the watchdog should have cut the match off"
    assert elapsed < _CEILING_SECONDS, f"{pattern!r} took {elapsed:.2f}s; SIGALRM did not fire"


def test_sigalrm_fallback_leaves_no_timer_or_handler_behind(without_re2: None) -> None:
    """A budgeted match must not leak itimer/handler state into the caller."""
    previous = signal.getsignal(signal.SIGALRM)
    search_within_budget(re.compile(ADVERSARIAL[0][0]), ADVERSARIAL[0][1])
    assert signal.getsignal(signal.SIGALRM) is previous, "handler was not restored"
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), "itimer was left armed"


def test_normal_patterns_unaffected_by_the_fallback(without_re2: None) -> None:
    """The stdlib path must agree with plain ``re`` on realistic filters."""
    assert matches_within_budget(re.compile("ERROR.*"), "x" * 5000 + "ERROR: boom") is True
    assert matches_within_budget(re.compile("ERROR.*"), "nothing here") is False
    # No RE2 => no ASCII-class divergence; stdlib Unicode semantics are preserved.
    assert matches_within_budget(re.compile(r"^\d+$"), "٣٤٥") is True


def test_screens_alone_do_not_reject_the_adversarial_patterns() -> None:
    """WHY the budget is required: the syntactic screens pass ``(a|b|ab)+Z``.

    This is the finding RG1 turns on. If a future change makes the screens reject
    it, the budget is still the guarantee -- but this documents that no screen
    caught it.
    """
    pattern = "(a|b|ab)+Z"
    assert _NESTED_QUANTIFIER_RE.search(pattern) is None, "nested screen does not catch it"
    assert count_variable_quantifiers(pattern) <= _MAX_VARIABLE_QUANTIFIERS, "count screen passes"
    assert len(pattern) <= 200, "length screen passes"


def test_plus_chain_is_now_counted() -> None:
    """RG1 defense-in-depth: ``+`` joins the counted set.

    ``"a+" * 30`` backtracks exponentially with no ``?``/``*`` anywhere, so before
    RG1 it scored 0 and passed the A12 cap outright.
    """
    assert count_variable_quantifiers("a+" * 30) == 30
    assert count_variable_quantifiers("a+") == 1
    # A fixed repeat is not variable-length and still must not count.
    assert count_variable_quantifiers("a{3}") == 0
    # Realistic filters stay far under the cap.
    for benign in ("ERROR.*", r"^\d+$", r"\S+@\S+", "(foo|bar)+"):
        assert count_variable_quantifiers(benign) <= _MAX_VARIABLE_QUANTIFIERS


def test_normal_patterns_still_match_normally() -> None:
    """The budget must be invisible to realistic filters (no behavior change)."""
    assert matches_within_budget(re.compile("ERROR.*"), "x" * 5000 + "ERROR: boom") is True
    assert matches_within_budget(re.compile("ERROR.*"), "nothing here") is False
    assert matches_within_budget(re.compile(r"^\d+$"), "12345") is True
    assert search_within_budget(re.compile("a"), "a") is MatchVerdict.MATCH
    assert search_within_budget(re.compile("a"), "b") is MatchVerdict.NO_MATCH


def test_search_within_budget_is_total() -> None:
    """Never raises, whatever the input: a filter loop must not explode."""
    assert search_within_budget(re.compile(""), "") is MatchVerdict.MATCH
    assert search_within_budget(re.compile("(?=.*a)"), "bca") is MatchVerdict.MATCH  # re fallback
    assert search_within_budget(re.compile(r"(\w)\1"), "aa") is MatchVerdict.MATCH  # backref
    assert search_within_budget(re.compile("x"), "y" * 100_000) is MatchVerdict.NO_MATCH


def test_lookaround_and_backref_fall_back_to_re() -> None:
    """RE2 refuses these; the fallback must still give correct ``re`` answers."""
    assert matches_within_budget(re.compile(r"foo(?=bar)"), "foobar") is True
    assert matches_within_budget(re.compile(r"foo(?=bar)"), "foobaz") is False
    assert matches_within_budget(re.compile(r"(\w)\1"), "abb") is True
    assert matches_within_budget(re.compile(r"(\w)\1"), "abc") is False


@pytest.mark.skipif(not re2_available(), reason="documents the RE2-path residual")
def test_known_residual_lookaround_is_not_re2_compilable() -> None:
    """KNOWN LIMITATION (RG1), pinned so it is visible rather than assumed away.

    RE2 refuses lookaround, so ``(a|b|ab)+(?=Z)`` falls back to stdlib ``re``.
    On the main thread SIGALRM still bounds it (asserted below). Off the main
    thread -- the MCP server's ``asyncio.to_thread`` path -- nothing can, and that
    pattern remains unbounded. Closing it means rejecting lookaround/backrefs in
    agent-supplied filters, a capability removal that is a product call.

    This test does NOT run the unbounded case (it would hang the suite by
    construction); it pins the two facts that make the residual real.
    """
    import furl_ctx.ccr.regex_budget as rb

    assert rb._compile_re2("(a|b|ab)+(?=Z)") is None, "RE2 must refuse the lookahead"
    # The screens do not catch it either, so nothing else is bounding it.
    assert _NESTED_QUANTIFIER_RE.search("(a|b|ab)+(?=Z)") is None
    assert count_variable_quantifiers("(a|b|ab)+(?=Z)") <= _MAX_VARIABLE_QUANTIFIERS
    # On the main thread the SIGALRM watchdog still bounds it.
    start = time.monotonic()
    verdict = search_within_budget(re.compile("(a|b|ab)+(?=Z)"), "ab" * 2600)
    assert verdict is MatchVerdict.BUDGET_EXCEEDED
    assert time.monotonic() - start < _CEILING_SECONDS


@pytest.mark.skipif(not re2_available(), reason="documents an RE2-only divergence")
def test_re2_ascii_class_divergence_is_known_and_pinned() -> None:
    """DISCLOSED (RG1): RE2's perl classes are ASCII-only, Python's are Unicode.

    ``^\\d+$`` matches Arabic-Indic digits under stdlib ``re`` but not under RE2.
    This is the ONE divergence measured across the realistic pattern set, and it
    only surfaces for a Unicode-class pattern over non-ASCII content. Pinned so
    the difference is known and visible rather than a silent surprise; bounding an
    agent-reachable hang is worth it.
    """
    arabic_indic = "٣٤٥"  # ٣٤٥
    assert re.compile(r"^\d+$").search(arabic_indic) is not None
    assert matches_within_budget(re.compile(r"^\d+$"), arabic_indic) is False
    # ASCII digits agree under both engines -- the common case is unaffected.
    assert matches_within_budget(re.compile(r"^\d+$"), "12345") is True
