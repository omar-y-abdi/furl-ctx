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
    _validate_pattern,
    count_variable_quantifiers,
)
from furl_ctx.ccr.regex_budget import (
    MatchVerdict,
    matches_within_budget,
    re2_available,
    search_within_budget,
)
from furl_ctx.ccr.retrieve_filters import (
    FilterError,
    RetrieveFilters,
    _line_matches,
    _reject_pathological_pattern,
)

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


@pytest.mark.skipif(not re2_available(), reason="the ingress bound requires RE2 to judge")
def test_residual_lookaround_is_closed_at_ingress_not_left_unbounded() -> None:
    """B1: the RG1 residual is now REJECTED at ingress instead of run unbounded.

    RE2 refuses lookaround, so ``(a|b|ab)+(?=Z)`` cannot be bounded off the main
    thread -- and the MCP server matches on worker threads, where a wedged ``sre``
    holds the GIL and freezes the whole event loop for every session on the
    process. This previously shipped as a documented residual. It no longer
    reaches the matcher at all: both validators reject it.

    The facts that made the residual real are still pinned below, because they are
    exactly why ingress rejection (not a budget) has to be the bound here.
    """
    import furl_ctx.ccr.regex_budget as rb

    assert rb._compile_re2("(a|b|ab)+(?=Z)") is None, "RE2 must refuse the lookahead"
    # The syntactic screens do not catch it, so nothing else could bound it.
    assert _NESTED_QUANTIFIER_RE.search("(a|b|ab)+(?=Z)") is None
    assert count_variable_quantifiers("(a|b|ab)+(?=Z)") <= _MAX_VARIABLE_QUANTIFIERS
    # Which is why ingress must refuse it outright, on BOTH agent-reachable paths.
    assert rb.classify_boundability("(a|b|ab)+(?=Z)") is rb.Boundability.UNBOUNDABLE
    assert _reject_pathological_pattern("(a|b|ab)+(?=Z)") is not None
    assert _validate_pattern("(a|b|ab)+(?=Z)", "include") is not None


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


# ---------------------------------------------------------------------------
# B1 -- ingress rejects what cannot be bounded off the main thread
# ---------------------------------------------------------------------------

# Patterns RE2 refuses. Each is agent-supplyable, passes every syntactic screen,
# and would otherwise run unbudgeted on the MCP worker thread.
UNBOUNDABLE_PATTERNS = ["(a|b|ab)+(?=Z)", r"(a)\1", "foo(?!bar)", "(?<=x)y"]


@pytest.mark.skipif(not re2_available(), reason="ingress cannot pre-judge without RE2")
@pytest.mark.parametrize("pattern", UNBOUNDABLE_PATTERNS)
def test_retrieve_ingress_rejects_unboundable_pattern(pattern: str) -> None:
    """B1: furl_retrieve refuses a pattern it could not time-bound, loudly."""
    error = _reject_pathological_pattern(pattern)
    assert error is not None, f"{pattern!r} must not reach the matcher"
    assert "lookaround" in error.reason
    # And end-to-end through the real parse entry point the MCP tool calls.
    parsed = RetrieveFilters.parse({"pattern": pattern})
    assert isinstance(parsed, FilterError)


@pytest.mark.skipif(not re2_available(), reason="ingress cannot pre-judge without RE2")
@pytest.mark.parametrize("pattern", UNBOUNDABLE_PATTERNS)
def test_compress_ingress_rejects_unboundable_pattern(pattern: str) -> None:
    """B1: furl_compress include/exclude filters refuse it too, with a typed error."""
    error = _validate_pattern(pattern, "include")
    assert error is not None, f"{pattern!r} must not reach the matcher"
    assert "lookaround" in error


@pytest.mark.skipif(not re2_available(), reason="ingress cannot pre-judge without RE2")
def test_ingress_still_accepts_the_benign_catastrophic_pattern() -> None:
    """B1 must not over-reject: ``(a|b|ab)+Z`` is catastrophic under ``re`` but RE2
    bounds it, so it stays a working filter and answers fast."""
    assert _reject_pathological_pattern("(a|b|ab)+Z") is None
    assert _validate_pattern("(a|b|ab)+Z", "include") is None
    start = time.monotonic()
    assert matches_within_budget(re.compile("(a|b|ab)+Z"), "ab" * 2600) is False
    assert time.monotonic() - start < _CEILING_SECONDS


@pytest.mark.skipif(not re2_available(), reason="ingress cannot pre-judge without RE2")
@pytest.mark.parametrize("pattern", ["ERROR.*", r"^\d+$", "foo|bar", "*.py", "[unclosed"])
def test_ingress_accepts_normal_and_non_regex_patterns(pattern: str) -> None:
    """B1 must not break globs: a source that is not a Python regex at all (``*.py``)
    is UNKNOWN, not UNBOUNDABLE -- ``compress_modes`` still matches it via fnmatch,
    and ``retrieve_filters`` still reports its own ``invalid regex`` error."""
    assert _validate_pattern(pattern, "include") is None
    assert regex_budget.classify_boundability("*.py") is regex_budget.Boundability.UNKNOWN


def test_glob_filter_still_matches_after_the_ingress_screen() -> None:
    """B1 regression guard: rejecting on 'RE2 cannot compile it' alone would have
    killed every glob filter, since ``*.py`` fails BOTH engines."""
    matcher = _resolve_matcher("*.py")
    assert matcher("foo.py") is True
    assert matcher("foo.txt") is False


def test_ingress_does_not_reject_when_re2_is_absent(without_re2: None) -> None:
    """B1: an install without the ``re2`` extra cannot judge boundability, so it must
    NOT reject everything -- it keeps the documented SIGALRM/residual behavior.

    The load-bearing detail: ``_compile_re2`` returns None for BOTH "RE2 refused
    this pattern" and "RE2 is not installed". Conflating them would make a plain
    ``pip install furl-ctx`` reject EVERY filter pattern, so the absence check has
    to come first.
    """
    regex_budget._compile_re2.cache_clear()
    assert regex_budget.classify_boundability("(a|b|ab)+(?=Z)") is regex_budget.Boundability.UNKNOWN
    assert _reject_pathological_pattern("(a|b|ab)+(?=Z)") is None
    assert _validate_pattern("(a|b|ab)+(?=Z)", "include") is None


@pytest.mark.parametrize("pattern", ["ERROR.*", r"^\d+$", "foo|bar", "*.py"])
def test_normal_patterns_still_accepted_without_re2(without_re2: None, pattern: str) -> None:
    """B1: the no-re2 install must keep working. If ``_compile_re2``'s two None cases
    were conflated, every one of these ordinary filters would be rejected."""
    regex_budget._compile_re2.cache_clear()
    assert regex_budget.classify_boundability(pattern) is regex_budget.Boundability.UNKNOWN
    assert _reject_pathological_pattern(pattern) is None
    assert _validate_pattern(pattern, "include") is None


# ---------------------------------------------------------------------------
# F2 -- a large bounded repetition is refused, with an HONEST diagnosis
# ---------------------------------------------------------------------------


LARGE_BOUNDED_REPS = ["a{0,2000}", "[0-9]{0,1200}", "a{0,1001}"]


@pytest.mark.skipif(not re2_available(), reason="RE2 sets the repetition ceiling it enforces")
@pytest.mark.parametrize("pattern", LARGE_BOUNDED_REPS)
def test_large_bounded_repetition_is_unboundable(pattern: str) -> None:
    """F2: RE2 refuses ``{m,n}`` with n>1000, so classify calls it UNBOUNDABLE. A
    deliberate over-approximation (a lone ``a{0,2000}`` is linear under ``re``), kept
    because proving it linear is exactly RE2's job and ``(a*){0,2000}`` is not."""
    assert regex_budget.classify_boundability(pattern) is regex_budget.Boundability.UNBOUNDABLE


@pytest.mark.skipif(not re2_available(), reason="ingress cannot pre-judge without RE2")
@pytest.mark.parametrize("pattern", LARGE_BOUNDED_REPS)
def test_large_bounded_repetition_reject_names_the_real_cause(pattern: str) -> None:
    """F2: the reject message must not blame "lookaround/backreferences" for a pattern
    whose only sin is a big bounded repetition -- the earlier wording was a factually
    wrong diagnosis. It now names the repetition and the 1000 ceiling on BOTH paths."""
    error = _reject_pathological_pattern(pattern)
    assert error is not None
    assert "repetition" in error.reason and "1000" in error.reason, error.reason
    compress_error = _validate_pattern(pattern, "include")
    assert compress_error is not None
    assert "repetition" in compress_error and "1000" in compress_error, compress_error


@pytest.mark.skipif(not re2_available(), reason="the 1000 ceiling is RE2's")
def test_bounded_repetition_at_the_ceiling_is_still_accepted() -> None:
    """F2: the threshold is exactly 1000 -- ``a{0,1000}`` is fine, so the fix must not
    over-correct into rejecting benign reps at or below the limit."""
    assert regex_budget.classify_boundability("a{0,1000}") is regex_budget.Boundability.BOUNDABLE
    assert _reject_pathological_pattern("a{0,1000}") is None
    assert _validate_pattern("a{0,1000}", "include") is None


# ---------------------------------------------------------------------------
# B5 -- RE2 must not silently drop a caller's regex flags
# ---------------------------------------------------------------------------


def test_constructor_flags_are_honored_under_re2() -> None:
    """B5: ``re.compile(src, re.IGNORECASE)`` carries a flag RE2 never sees (it has
    no flags argument), so the RE2 path must be skipped rather than answer a
    different question case-sensitively."""
    assert matches_within_budget(re.compile("error", re.IGNORECASE), "ERROR") is True
    assert matches_within_budget(re.compile("error"), "ERROR") is False


def test_flag_handling_is_identical_with_and_without_re2(without_re2: None) -> None:
    """B5: the two engines must agree on a flagged pattern -- that is the whole point."""
    regex_budget._compile_re2.cache_clear()
    assert matches_within_budget(re.compile("error", re.IGNORECASE), "ERROR") is True
    assert matches_within_budget(re.compile("error"), "ERROR") is False


@pytest.mark.skipif(not re2_available(), reason="asserts the RE2 path is kept")
def test_inline_flags_stay_on_the_re2_path() -> None:
    """B5 must not reopen B1. ``re.compile("(?i)x").flags`` is INDISTINGUISHABLE from
    ``re.compile("x", re.I).flags`` (both ``UNICODE|IGNORECASE``), so rejecting the
    RE2 path on ``flags & ~re.UNICODE`` would push inline-flag patterns onto the
    unbudgeted path -- making ``(?i)(a|b|ab)+Z`` a freeze again. RE2 parses ``(?i)``
    from the source itself, so it stays bounded AND correct.
    """
    assert re.compile("(?i)error").flags == re.compile("error", re.IGNORECASE).flags
    assert regex_budget._re2_sees_same_flags(re.compile("(?i)error")) is True
    assert regex_budget._re2_sees_same_flags(re.compile("error", re.IGNORECASE)) is False
    # Correct answer AND bounded: it must not fall through to unbudgeted `re`.
    assert matches_within_budget(re.compile("(?i)error"), "ERROR") is True
    start = time.monotonic()
    assert matches_within_budget(re.compile("(?i)(a|b|ab)+Z"), "ab" * 2600) is False
    assert time.monotonic() - start < _CEILING_SECONDS


# ---------------------------------------------------------------------------
# F3 -- the agent filter call sites must compile FLAGLESS, so a future
#       constructor flag cannot silently reopen the unbudgeted residual
# ---------------------------------------------------------------------------


def test_retrieve_filter_compiles_the_pattern_flagless() -> None:
    """F3: ``RetrieveFilters.parse`` compiles the agent pattern with NO constructor
    flags. If a future edit added e.g. ``re.IGNORECASE`` there, ``_re2_sees_same_flags``
    would skip RE2 at match time and a BOUNDABLE pattern would run UNBUDGETED on the
    worker thread -- B1 again. Pin the invariant the residual safety rests on: the
    compiled flags must equal a flagless recompile of the same source."""
    source = "Error[0-9]+"
    parsed = RetrieveFilters.parse({"pattern": source})
    assert isinstance(parsed, RetrieveFilters)
    assert parsed.pattern is not None
    assert parsed.pattern.flags == re.compile(source).flags


def test_compress_filter_compiles_the_pattern_flagless() -> None:
    """F3: ``_resolve_matcher`` (the furl_compress include/exclude path) compiles
    flagless too. The compiled object is captured in a closure, so spy on ``re.compile``
    and assert every call the matcher made passed no flags argument."""
    from unittest import mock

    real_compile = re.compile
    calls: list[tuple[object, int]] = []

    def _spy(pattern, flags=0):
        calls.append((pattern, flags))
        return real_compile(pattern, flags)

    with mock.patch.object(re, "compile", _spy):
        _resolve_matcher("Error[0-9]+")
    assert calls, "the matcher must have compiled the pattern"
    assert all(flags == 0 for _pattern, flags in calls), (
        f"a filter call site added constructor flags, which reopens the residual: {calls}"
    )


# ---------------------------------------------------------------------------
# B7 -- the watchdog must not eat a caller's pending alarm
# ---------------------------------------------------------------------------


def test_pre_armed_itimer_survives_a_budgeted_match(without_re2: None) -> None:
    """B7: an embedder (or pytest-timeout) may already have ITIMER_REAL armed. The
    watchdog used to overwrite it and zero it on the way out, silently cancelling a
    timeout the caller was relying on."""
    regex_budget._compile_re2.cache_clear()
    fired: list[str] = []
    previous = signal.signal(signal.SIGALRM, lambda *_: fired.append("caller"))
    signal.setitimer(signal.ITIMER_REAL, 30.0)
    try:
        assert matches_within_budget(re.compile("ERROR"), "some line") is False
        remaining, _interval = signal.getitimer(signal.ITIMER_REAL)
        assert remaining > 0, "caller's pending alarm was cancelled by the budget"
        assert signal.getsignal(signal.SIGALRM) is not regex_budget._raise_budget_exceeded
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_restored_itimer_does_not_gain_the_match_time(without_re2: None) -> None:
    """B7: the restored deadline must have the match's own time SUBTRACTED.

    Re-arming the value setitimer returned at entry would silently push the
    caller's deadline out by however long the match ran -- a 0.5 s alarm becoming
    0.5 s + match. Uses an adversarial pattern so the match burns the full budget.
    """
    regex_budget._compile_re2.cache_clear()
    pattern, text = ADVERSARIAL[0]
    previous = signal.signal(signal.SIGALRM, lambda *_: None)
    signal.setitimer(signal.ITIMER_REAL, 5.0)
    try:
        assert matches_within_budget(re.compile(pattern), text) is False
        remaining, _interval = signal.getitimer(signal.ITIMER_REAL)
        # The match consumed ~budget_seconds; the caller's 5s must have shrunk by
        # at least that, never grown.
        assert remaining < 5.0, "restored timer gained the match's runtime"
        assert remaining > 4.0, "restored timer lost far more than the budget"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_unarmed_caller_timer_stays_unarmed(without_re2: None) -> None:
    """B7: 0.0 from setitimer means "was not armed" -- it must NOT be re-armed with
    the epsilon, which would hand the caller an alarm they never asked for."""
    regex_budget._compile_re2.cache_clear()
    previous = signal.signal(signal.SIGALRM, lambda *_: None)
    try:
        assert signal.getitimer(signal.ITIMER_REAL)[0] == 0.0
        assert matches_within_budget(re.compile("ERROR"), "some line") is False
        assert signal.getitimer(signal.ITIMER_REAL)[0] == 0.0, "invented a timer"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
