"""Wall-clock-bounded matching for AGENT-SUPPLIED regexes (review RG1).

Why this module exists
----------------------
``furl_retrieve``'s ``pattern`` and ``furl_compress``'s include/exclude filters
are supplied by the calling agent and run against stored content. Before RG1 the
only defenses were SYNTACTIC: an input-length cap plus screens for the
nested-quantifier (``(a+)+``) and long-optional-chain (``.?`` x N) shapes.

Syntactic screening cannot bound backtracking. The counter-example that motivated
this module is ``(a|b|ab)+Z``: it carries NO nested quantifier, NO ``?``/``*``
chain, and is 10 characters long, so every screen passes it -- yet against an
80-character ambiguous line (``"ab" * 40``) CPython's ``re`` backtracks for
minutes. Ambiguous alternation under a single ``+`` is exponential, and no
cheap syntactic test separates it from the benign ``(foo|bar)+``. Only running
the match under a real time budget bounds it.

Engine strategy (in order)
--------------------------
1. **RE2** (``google-re2``, optional). RE2 matches with an automaton, not a
   backtracker, so the pathological class simply does not exist for it -- the
   counter-example above resolves in ~0.08 ms. It is the ONLY option that holds
   in a worker thread, which is where the MCP server actually runs these matches
   (``mcp_server`` dispatches ``_retrieve_content_sync`` via ``asyncio.to_thread``
   and ``_compress_content`` via ``run_in_executor``).
2. **SIGALRM watchdog** (stdlib, main thread only). CPython's ``sre`` engine calls
   ``PyErr_CheckSignals()`` periodically, so an itimer CAN interrupt a wedged
   match -- but ONLY on the main thread, since Python-level signal handlers never
   run elsewhere. This covers the CLI, and keeps EXACT ``re`` semantics.
3. **Unbudgeted ``re``** -- the residual. Reached when BOTH are true: the match is
   off the main thread (so no watchdog can fire) AND RE2 cannot handle the
   pattern (not installed, or the pattern uses lookaround/backreferences, which
   RE2 refuses because it cannot match them in linear time). The syntactic
   screens and the input cap still apply, but they do not bound backtracking.

Why the residual is closed at INGRESS (review B1)
-------------------------------------------------
The residual is not merely "a slow match", which is how an earlier revision of
this docstring framed it. Because CPython's ``sre`` holds the GIL for the whole
match, a wedged worker thread starves the ENTIRE asyncio event loop: measured on
the MCP server, 1 event-loop tick during a 1.48 s match where a healthy loop
ticks ~30. Every session served by the process freezes until the match ends, and
an agent picks the duration -- the input cap is 10 000 characters and the cost of
``(a|b|ab)+(?=Z)`` grows ~4x per 2 extra characters (measured 0.14 s / 0.56 s /
2.3 s at 18 / 20 / 22 ``ab`` pairs). That is a process-wide denial of service
reachable from one ordinary tool call, not a slow filter.

So a pattern RE2 cannot compile is REJECTED at ingress by both validators
(``retrieve_filters._reject_pathological_pattern`` and
``compress_modes._validate_pattern``) via :func:`classify_boundability`, rather
than accepted and run unbounded. This removes lookaround/backreferences from
agent-supplied filters; no production caller and no test used them. When RE2 is
absent the boundability of a pattern cannot be pre-judged, so ingress does NOT
reject and the SIGALRM/residual path above still applies -- an install without
the ``re2`` extra (the ``mcp`` extra pulls it in) keeps the old behavior rather
than rejecting every pattern it cannot classify.

Why NOT a thread-based watchdog
-------------------------------
Running the match in a ``ThreadPoolExecutor`` and waiting with
``future.result(timeout=...)`` looks portable but does NOT work: CPython's ``sre``
engine holds the GIL for the entire match, so the waiting thread never gets the
GIL back to observe its own timeout. Measured on CPython 3.13: a main thread that
asked for a 2 s sleep while a worker backtracked did not resume for >58 s. The
timeout is unobservable, so that approach only adds a thread while still hanging.

Engine-divergence note (disclosed)
----------------------------------
RE2's perl classes are ASCII-only where Python's are Unicode-aware, so
``^\\d+$`` matches the Arabic-Indic ``"٣٤٥"`` under ``re`` but not
under RE2. This is the one measured divergence over the realistic pattern set
(see ``tests/test_regex_budget.py``); it only ever surfaces for a Unicode-class
pattern applied to non-ASCII content. Bounding an agent-reachable hang is worth
that narrow, pinned difference. Everything here is a BOOLEAN "does a match
exist" verdict, so leftmost-first vs leftmost-longest -- the usual RE2/``re``
difference -- cannot matter: it changes which span wins, never whether one exists.

Totality: every entry point returns a verdict and NEVER raises to the caller.

Known, deliberately deferred (tracked, not dropped)
---------------------------------------------------
* #7 -- the budget is PER LINE, not per operation, so a filter over very many
  lines can still take a long total wall-clock. CLI-only in practice and
  Ctrl-C-interruptible; the MCP path is bounded by RE2's linear-time match.
* #10 -- ``MatchVerdict.BUDGET_EXCEEDED`` is distinguishable but no production
  caller reads it: both call sites use :func:`matches_within_budget`, which folds
  it into "no match". Kept distinct so an overrun CAN be reported.
* #12 -- ``$`` means ``\\Z`` in RE2 but ``\\z``-with-trailing-newline in ``re``.
  Both filter callers split content on ``"\\n"`` first, so no line carries the
  trailing newline that would expose the difference.
"""

from __future__ import annotations

import re
import signal
import threading
from enum import Enum
from functools import lru_cache
from types import FrameType
from typing import Any, Final

# The per-line wall-clock budget. Generous next to a realistic filter (an
# ``ERROR.*`` search over a 5 000-character line measured 0.01 ms, ~4 orders of
# magnitude under) and small enough that a wedge is a blip rather than a hang.
DEFAULT_MATCH_BUDGET_SECONDS: Final = 0.1


class MatchVerdict(Enum):
    """Outcome of one budgeted match.

    ``BUDGET_EXCEEDED`` is distinct from ``NO_MATCH`` on purpose: callers treat
    it as "no match on this line" for filtering, but it is a LOUD event worth
    reporting, not a silent negative.
    """

    MATCH = "match"
    NO_MATCH = "no_match"
    BUDGET_EXCEEDED = "budget_exceeded"

    @property
    def is_match(self) -> bool:
        """Whether this verdict should be treated as a match by a filter."""
        return self is MatchVerdict.MATCH


def _load_re2() -> Any | None:
    """Import ``re2`` once, or ``None`` when the optional extra is absent."""
    try:
        import re2
    except Exception:  # noqa: BLE001 - absent/broken extra is a normal fallback
        return None
    return re2


_RE2: Final = _load_re2()


def re2_available() -> bool:
    """Whether the RE2 engine is importable (the linear-time match path)."""
    return _RE2 is not None


@lru_cache(maxsize=512)
def _compile_re2(source: str) -> Any | None:
    """Compile ``source`` with RE2, or ``None`` when RE2 can't handle it.

    RE2 refuses constructs it cannot match in linear time (lookaround,
    backreferences); those fall back to the budgeted ``re`` path. Cached so a
    hot per-line loop compiles once per pattern, and so RE2's parse-error log
    noise is emitted at most once per pattern.
    """
    if _RE2 is None:
        return None
    try:
        return _RE2.compile(source)
    except Exception:  # noqa: BLE001 - unsupported syntax is a normal fallback
        return None


class Boundability(Enum):
    """Whether a pattern SOURCE can be proven time-bounded before it is run.

    Three states, not a boolean, because "RE2 refuses it" and "RE2 cannot judge
    it" demand opposite responses at ingress: the first is the B1 freeze and must
    be rejected; the second must be let through (rejecting it would break every
    glob filter and every install without the ``re2`` extra).
    """

    BOUNDABLE = "boundable"
    """RE2 compiles it: the match is linear-time on any thread."""

    UNBOUNDABLE = "unboundable"
    """A valid Python regex that RE2 refuses (lookaround/backreferences).

    Off the main thread nothing can bound it -- the B1 process-wide freeze.
    """

    UNKNOWN = "unknown"
    """Not judgeable here: RE2 is absent, or the source is not a Python regex.

    A non-regex source is NOT a filter defect -- ``compress_modes`` matches such a
    pattern (e.g. ``*.py``) with ``fnmatch`` glob semantics, and
    ``retrieve_filters`` reports its own ``invalid regex`` error. Both would
    break if this were folded into ``UNBOUNDABLE``.
    """


def classify_boundability(pattern: str) -> Boundability:
    """Classify whether ``pattern``'s source can be time-bounded. Never raises.

    Ingress validators reject ONLY :attr:`Boundability.UNBOUNDABLE`; see the
    module docstring for why ``UNKNOWN`` must pass through.
    """
    if _RE2 is None:
        return Boundability.UNKNOWN
    try:
        re.compile(pattern)
    except re.error:
        # Not a Python regex at all -- the caller's own glob/invalid-regex
        # handling owns this case, so refuse to judge it.
        return Boundability.UNKNOWN
    if _compile_re2(pattern) is not None:
        return Boundability.BOUNDABLE
    return Boundability.UNBOUNDABLE


def _re2_sees_same_flags(compiled: re.Pattern[str]) -> bool:
    """Whether RE2 compiling ``compiled.pattern`` honors every flag it carries.

    RE2 has no flags argument (there is no ``re2.IGNORECASE``), so it only ever
    sees flags written INTO the source, like ``(?i)``. Those it honors natively.
    Flags passed to ``re.compile(source, flags)`` are invisible to it, and using
    RE2 there would silently answer a DIFFERENT question than the caller asked
    (review B5: ``re.compile("error", re.IGNORECASE)`` matching case-sensitively).

    ``compiled.flags`` CANNOT distinguish the two: ``re.compile("error", re.I)``
    and ``re.compile("(?i)error")`` both report exactly ``re.UNICODE|re.IGNORECASE``.
    Recompiling the bare source and comparing does distinguish them -- if the
    source alone reproduces the flags, every flag is inline and RE2 sees it.
    Rejecting all flags instead (the obvious fix) would push inline-``(?i)``
    patterns onto the unbudgeted path and reopen B1 for ``(?i)(a|b|ab)+Z``.
    """
    try:
        return re.compile(compiled.pattern).flags == compiled.flags
    except re.error:
        return False


class _BudgetExceeded(Exception):
    """Internal: raised from the SIGALRM handler to unwind a wedged match."""


def _raise_budget_exceeded(signum: int, frame: FrameType | None) -> None:
    raise _BudgetExceeded()


def _can_use_sigalrm() -> bool:
    """Whether a SIGALRM watchdog can be armed on this thread.

    Python-level signal handlers only run on the main thread of the main
    interpreter; ``signal.signal`` raises ``ValueError`` anywhere else. Guard by
    asking rather than by catching, so the common worker-thread case costs no
    exception.
    """
    return threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGALRM")


def _search_with_watchdog(
    compiled: re.Pattern[str], text: str, budget_seconds: float
) -> MatchVerdict:
    """Run ``compiled.search(text)`` under a SIGALRM budget (main thread only).

    Restores BOTH the previous handler and the caller's previous ``ITIMER_REAL``
    unconditionally, so a budgeted match never leaks timer state into the caller
    and never cancels a timer the caller had already armed (review B7: an
    embedder's or ``pytest-timeout``'s pending alarm used to be silently dropped,
    the exact opposite of what this docstring promised).

    The restore is COARSE: the remaining interval is re-armed as it read at entry,
    without subtracting the time this match spent. That over-grants at most
    ``budget_seconds`` (0.1 s by default), which is why it is preferred over
    leaving the caller with no timer at all.
    """
    previous_handler = signal.signal(signal.SIGALRM, _raise_budget_exceeded)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, budget_seconds)
    try:
        return MatchVerdict.MATCH if compiled.search(text) else MatchVerdict.NO_MATCH
    except _BudgetExceeded:
        return MatchVerdict.BUDGET_EXCEEDED
    except re.error:
        # A match-time regex error (rare; e.g. recursion limits) is a non-match,
        # never an exception escaping into a filter loop.
        return MatchVerdict.NO_MATCH
    finally:
        # Disarm ours first, then restore the caller's handler, then re-arm what
        # the caller had pending (0.0 from setitimer means "was not armed", and
        # setitimer(0.0) is itself the disarm, so the same call covers both).
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        remaining, interval = previous_timer
        if remaining:
            signal.setitimer(signal.ITIMER_REAL, remaining, interval)


def search_within_budget(
    compiled: re.Pattern[str],
    text: str,
    *,
    budget_seconds: float = DEFAULT_MATCH_BUDGET_SECONDS,
) -> MatchVerdict:
    """Whether ``compiled`` matches ``text``, under a bounded time budget.

    Total: returns a verdict for every input and never raises. See the module
    docstring for the engine order and the disclosed RE2/``re`` divergence.
    """
    engine = _compile_re2(compiled.pattern) if _re2_sees_same_flags(compiled) else None
    if engine is not None:
        try:
            return MatchVerdict.MATCH if engine.search(text) else MatchVerdict.NO_MATCH
        except Exception:  # noqa: BLE001 - fall back rather than fail the filter
            pass
    if _can_use_sigalrm():
        return _search_with_watchdog(compiled, text, budget_seconds)
    # THE RESIDUAL (see module docstring): off the main thread with no RE2 form of
    # this pattern. No stdlib mechanism can interrupt a backtracking match here --
    # the GIL makes any in-process watchdog unobservable -- so this runs
    # unbudgeted. The syntactic screens and the input cap still apply, but they do
    # not bound backtracking. Reached in practice only for lookaround/backref
    # patterns on the MCP worker path, or on an install without the re2 extra.
    try:
        return MatchVerdict.MATCH if compiled.search(text) else MatchVerdict.NO_MATCH
    except re.error:
        return MatchVerdict.NO_MATCH


def matches_within_budget(
    compiled: re.Pattern[str],
    text: str,
    *,
    budget_seconds: float = DEFAULT_MATCH_BUDGET_SECONDS,
) -> bool:
    """Boolean convenience wrapper: a budget overrun counts as NO match.

    Use :func:`search_within_budget` where the overrun needs to be reported.
    """
    return search_within_budget(compiled, text, budget_seconds=budget_seconds).is_match
