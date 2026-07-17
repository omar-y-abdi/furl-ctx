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

   KNOWN LIMITATION, measured, not theoretical: ``(a|b|ab)+(?=Z)`` passes every
   screen, is refused by RE2 for the lookahead, and backtracks for minutes under
   ``re``. On the MCP server -- which matches in worker threads -- that pattern is
   still unbounded. Closing it needs either a bounded engine that supports
   lookaround (none available here) or rejecting lookaround/backreferences in
   agent-supplied filters outright, which would remove a working capability and
   is a product decision, not a bug fix. Installing the ``re2`` extra (the ``mcp``
   extra pulls it in) bounds every pattern RE2 can compile, which is everything
   except lookaround/backreferences.

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

    Restores the previous handler and disarms the itimer unconditionally, so a
    budgeted match never leaks timer state into the caller.
    """
    previous = signal.signal(signal.SIGALRM, _raise_budget_exceeded)
    signal.setitimer(signal.ITIMER_REAL, budget_seconds)
    try:
        return MatchVerdict.MATCH if compiled.search(text) else MatchVerdict.NO_MATCH
    except _BudgetExceeded:
        return MatchVerdict.BUDGET_EXCEEDED
    except re.error:
        # A match-time regex error (rare; e.g. recursion limits) is a non-match,
        # never an exception escaping into a filter loop.
        return MatchVerdict.NO_MATCH
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


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
    engine = _compile_re2(compiled.pattern)
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
