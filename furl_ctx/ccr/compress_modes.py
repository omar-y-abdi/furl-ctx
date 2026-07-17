"""Compression mode + section filtering for ``furl_compress`` (NR2-2 feature c).

The MCP ``furl_compress`` tool gains two optional dimensions. Both default to
today's exact behavior, so a default call is byte-identical:

* ``mode`` — one of ``normal`` (default), ``lossless_only``, ``aggressive``.
  These are a THIN selector over machinery that already exists in
  ``ContentRouterConfig``; no new compression algorithm is introduced.
    - ``normal``   → the process default pipeline (unchanged).
    - ``lossless_only`` → ``ContentRouterConfig(lossless_only=True)`` — only
      proven-lossless transforms run; lossy compressor routes and the
      CCR-offload fallback resolve to passthrough (existing mode).
    - ``aggressive`` → existing aggressiveness knobs turned up:
      ``smart_crusher_max_items_after_crush`` caps kept items low (fewer items
      survive a crush) and ``min_ratio_relaxed``/``min_ratio_aggressive`` are
      raised so the router ACCEPTS marginal compressions it would otherwise
      reject. Both are pre-existing ``ContentRouterConfig`` fields.

* ``include_patterns`` / ``exclude_patterns`` — glob-or-regex filters over the
  content, applied LINE-WISE (and, when present, over the message ``tool_name``
  as a whole-name match — see ``tool_name_is_excluded``). Contract:
    - A pattern is tried as a Python regex ``search`` first; if it does not
      COMPILE as a regex (e.g. a bare ``*.py`` glob), it falls back to
      ``fnmatch`` glob matching. This makes both ``ERROR.*`` (regex) and
      ``*.py`` (glob) work without a mode flag.
    - ``exclude_patterns``: any content line matching ANY exclude pattern is
      PROTECTED — passed through verbatim, never compressed.
    - ``include_patterns``: when non-empty, ONLY lines matching at least one
      include pattern are eligible for compression; every other line is
      protected. (Absent/empty ``include_patterns`` means "all lines eligible"
      subject to the exclude filter.)
    - The content is split into maximal runs of eligible vs protected lines in
      ORIGINAL order; each eligible run is compressed independently and each
      protected run ships verbatim, then all runs are rejoined in order. Order
      and the protected bytes are preserved exactly.

Effects: this module is a pure planner. It builds a pipeline object and
partitions text; it performs NO compression or I/O itself. The caller runs the
returned pipeline through ``compress()`` (which owns fail-open, token counting,
and the store writes).
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .regex_budget import Boundability, classify_boundability, matches_within_budget

# ── ReDoS guards (SEC-1, A12, RG1) ──────────────────────────────────────────
# The include/exclude patterns are caller-supplied and run over caller-supplied
# content, line by line. A catastrophically-backtracking pattern (e.g.
# ``(a+)+$``) over a long line can wedge the MCP process. Three layers defend it:
#
# * ``_MAX_PATTERN_CHARS`` / the nested-quantifier heuristic reject pathological
#   *patterns* at parse time (see ``_validate_pattern``);
# * ``_MAX_REGEX_LINE_CHARS`` bounds the *input* any single match ever sees;
# * the per-match wall-clock budget in ``regex_budget`` bounds the match itself.
#
# The budget is what actually makes this safe. The first two layers are
# SYNTACTIC, and syntax cannot bound backtracking: ``(a|b|ab)+Z`` has no nested
# quantifier, no optional chain, and is 10 characters long, yet it backtracks for
# minutes against an 80-character line -- well inside the input cap. An earlier
# revision of this comment claimed the input cap made "worst-case backtracking
# finite and small"; that was wrong, and RG1 replaced the claim with the budget.
#
# All three bounds sit far past realistic filter usage, so normal patterns and
# normal content are matched byte-identically to before.
_MAX_REGEX_LINE_CHARS = 10_000
_MAX_PATTERN_CHARS = 200

# Detects nested unbounded quantifiers — a quantified group whose body itself
# ends in an unbounded quantifier — e.g. ``(a+)+``, ``(a*)*``, ``(a+)*``,
# ``(.*)+``. This is the classic exponential-backtracking shape. Heuristic, not
# a proof: it catches the common single-nesting forms, not every pathological
# construction. The input cap above is the real backstop; this just fails the
# obvious cases loudly at parse time instead of at match time.
_NESTED_QUANTIFIER_RE = re.compile(r"\([^)]*[+*][^)]*\)[+*]")

# Bound on the number of variable-length quantifiers a single pattern may apply
# (review A12, widened to ``+`` by RG1). An "optional chain" such as ``.?``
# repeated dozens of times carries NO nested quantifier and stays under
# ``_MAX_PATTERN_CHARS``, yet backtracks exponentially on any line WITHIN the
# input cap — the cap bounds input length, not backtracking width, so a short
# adversarial line (``.?`` x22 measured ~7 s, worse beyond) still wedges the
# match. The nested-quantifier screen above misses this shape entirely. Real
# retrieve/compress filters carry at most a handful of quantifiers (the shipped
# test corpus tops out at 4), so a bound of 12 rejects the exponential chains
# with wide margin while passing every realistic pattern untouched.
#
# This screen is DEFENSE-IN-DEPTH, not the guarantee: it is syntactic, and no
# syntactic screen can bound backtracking (``(a|b|ab)+Z`` scores 1 and still
# hangs). The per-match budget in ``regex_budget`` is the actual bound (RG1).
_MAX_VARIABLE_QUANTIFIERS = 12


def count_variable_quantifiers(pattern: str) -> int:
    """Count the variable-length quantifiers a pattern applies.

    Counts ``?``, ``*``, ``+`` and any braced range (``{0,3}``, ``{1,}``, ``{,5}``
    — a ``{`` body containing a comma), skipping escaped metacharacters, character
    classes, and the ``?`` that opens a group extension (``(?:``, ``(?=``,
    ``(?<name>``). A high count is the optional-chain exponential-backtracking
    shape the nested-quantifier screen misses (A12); a fixed ``{3}`` repeat is
    NOT ambiguous and is not counted. Pure, linear in the pattern length.

    ``+`` counts as of RG1: a flat ``a+`` chain (``"a+" * 30``) backtracks
    exponentially with no ``?``/``*`` anywhere, so omitting ``+`` let that shape
    score 0 and sail through. This is defense-in-depth only — the screen still
    cannot catch ``(a|b|ab)+Z`` (one quantifier, still exponential), which is why
    the real bound is the per-match budget in :mod:`furl_ctx.ccr.regex_budget`.
    """
    count = 0
    i = 0
    n = len(pattern)
    in_class = False
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            i += 2  # an escaped char is a literal, never a quantifier
            continue
        if in_class:
            if ch == "]":
                in_class = False
            i += 1
            continue
        if ch == "[":
            in_class = True
            i += 1
            continue
        if ch in "?*+":
            # A ``?`` right after ``(`` opens a group extension ((?:, (?=,
            # (?<name>…) — group syntax, not a quantifier over an atom.
            if ch == "?" and i > 0 and pattern[i - 1] == "(":
                i += 1
                continue
            count += 1
            i += 1
            continue
        if ch == "{":
            close = pattern.find("}", i)
            if close != -1 and "," in pattern[i + 1 : close]:
                # A braced RANGE ({m,n}/{m,}/{,n}) is variable-length and can
                # backtrack; a fixed {m} count cannot, so only ranges count.
                count += 1
                i = close + 1
                continue
        i += 1
    return count


class CompressionMode(Enum):
    """Aggressiveness selector for ``furl_compress``.

    ``NORMAL`` is the default and maps to the unchanged process pipeline;
    the other two select existing ``ContentRouterConfig`` machinery.
    """

    NORMAL = "normal"
    LOSSLESS_ONLY = "lossless_only"
    AGGRESSIVE = "aggressive"

    @classmethod
    def parse(cls, value: Any) -> CompressionMode | str:
        """Parse a loose ``mode`` argument.

        Returns the ``CompressionMode`` on success or an error-reason string
        for an unknown/mistyped value (the handler renders it as a structured
        error envelope). ``None`` maps to ``NORMAL`` so an absent argument is
        the default.
        """
        if value is None:
            return cls.NORMAL
        if not isinstance(value, str):
            return f"mode must be a string, got {type(value).__name__}"
        normalized = value.strip().lower()
        for member in cls:
            if member.value == normalized:
                return member
        valid = ", ".join(member.value for member in cls)
        return f"unknown mode {value!r}; valid modes: {valid}"


# Aggressive-mode knob values. All are EXISTING ``ContentRouterConfig`` fields;
# these are just turned-up settings, not new behavior.
#
# * cap kept items low so a crush keeps fewer rows (verified to shrink a
#   JSON-array tool output below the ``normal`` result on a concrete payload);
# * raise both acceptance thresholds toward 1.0 so the router accepts marginal
#   compressions it rejects at the default 0.85/0.95 (a compression is accepted
#   when ``ratio < min_ratio``; higher accepts more).
_AGGRESSIVE_MAX_ITEMS_AFTER_CRUSH = 5
_AGGRESSIVE_MIN_RATIO_RELAXED = 0.98
_AGGRESSIVE_MIN_RATIO_AGGRESSIVE = 0.99


def build_mode_pipeline(mode: CompressionMode) -> Any | None:
    """Build the ``TransformPipeline`` for a non-default mode, or ``None``.

    ``None`` for ``NORMAL`` — the caller then uses the process default
    singleton, keeping a default call byte-identical. For the other modes,
    construct a pipeline whose ``ContentRouter`` carries the mode's
    ``ContentRouterConfig``, mirroring the default pipeline's transform order
    (``CrossMessageDeduper`` → ``ContentRouter``) so ONLY the router config
    differs from default.

    Imported locally to keep this module import-light and avoid a cycle with
    the transforms package.
    """
    if mode is CompressionMode.NORMAL:
        return None

    from ..transforms import TransformPipeline
    from ..transforms.content_router import ContentRouter, ContentRouterConfig
    from ..transforms.cross_message_dedup import CrossMessageDeduper

    if mode is CompressionMode.LOSSLESS_ONLY:
        router_config = ContentRouterConfig(lossless_only=True)
    else:  # AGGRESSIVE — exhaustive (three-member enum, NORMAL handled above)
        router_config = ContentRouterConfig(
            smart_crusher_max_items_after_crush=_AGGRESSIVE_MAX_ITEMS_AFTER_CRUSH,
            min_ratio_relaxed=_AGGRESSIVE_MIN_RATIO_RELAXED,
            min_ratio_aggressive=_AGGRESSIVE_MIN_RATIO_AGGRESSIVE,
        )

    return TransformPipeline(
        transforms=[CrossMessageDeduper(), ContentRouter(config=router_config)]
    )


@dataclass(frozen=True)
class SectionPatterns:
    """Validated include/exclude pattern sets for content section filtering.

    Construct via :meth:`parse`. ``is_empty`` is True when neither list was
    provided — the handler uses that to skip section filtering entirely and
    keep the whole-content path unchanged.
    """

    include: tuple[str, ...]
    exclude: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.include and not self.exclude

    @classmethod
    def parse(cls, arguments: dict[str, Any]) -> SectionPatterns | str:
        """Validate ``include_patterns`` / ``exclude_patterns`` arguments.

        Each, when present, must be a list of strings. Returns a
        ``SectionPatterns`` or an error-reason string.
        """
        include, include_err = _parse_pattern_list(
            arguments.get("include_patterns"), "include_patterns"
        )
        if include_err is not None:
            return include_err
        exclude, exclude_err = _parse_pattern_list(
            arguments.get("exclude_patterns"), "exclude_patterns"
        )
        if exclude_err is not None:
            return exclude_err
        return cls(include=include, exclude=exclude)

    def tool_name_is_excluded(self, tool_name: str | None) -> bool:
        """True when a whole ``tool_name`` matches an exclude pattern.

        Lets ``exclude_patterns`` target a tool by name (the contract mentions
        tool names). The MCP compress path currently wraps content with no tool
        name, so this returns False there — but the check is honest for callers
        that do set one.
        """
        if tool_name is None:
            return False
        return any(_pattern_matches(pattern, tool_name) for pattern in self.exclude)

    def _resolve_matchers(self) -> _ResolvedMatchers:
        """Compile every pattern to a matcher ONCE (SEC-1).

        ``partition_content`` resolves these a single time per call and reuses
        them across all lines, so a long input is not re-resolved per line.
        """
        return _ResolvedMatchers(
            include=tuple(_resolve_matcher(p) for p in self.include),
            exclude=tuple(_resolve_matcher(p) for p in self.exclude),
        )

    def line_is_eligible(self, line: str, matchers: _ResolvedMatchers | None = None) -> bool:
        """Whether a content line is eligible for compression.

        Protected (ineligible) when it matches any exclude pattern, or when
        ``include`` is non-empty and it matches none of the include patterns.
        Pass pre-resolved ``matchers`` in a hot loop to avoid re-resolving the
        patterns per line; when omitted they are resolved from ``self`` so a
        direct one-off call still works.

        ReDoS input bound (SEC-1): a line longer than ``_MAX_REGEX_LINE_CHARS``
        is never matched (the pattern would backtrack over unbounded input), so
        it is treated as PROTECTED — the conservative choice that ships it
        verbatim rather than silently compressing an unfilterable line or
        dropping a user's intended exclude-protection. Within the cap the match
        runs under a wall-clock budget (RG1, see ``_resolve_matcher``); a line
        that exhausts the budget reads as "no match" for both the exclude and
        include tests, so an include-filtered run treats it as ineligible
        (protected) rather than hanging. Realistic content lines are far under
        the cap, so neither bound fires in normal use.
        """
        if len(line) > _MAX_REGEX_LINE_CHARS:
            return False
        resolved = matchers if matchers is not None else self._resolve_matchers()
        if any(match(line) for match in resolved.exclude):
            return False
        if resolved.include:
            return any(match(line) for match in resolved.include)
        return True


def _parse_pattern_list(raw: Any, name: str) -> tuple[tuple[str, ...], str | None]:
    """Validate one optional pattern-list argument into a tuple of strings.

    Beyond the type check, each pattern is screened for the ReDoS-pathological
    shapes (over-long / nested unbounded quantifier); a rejected pattern yields
    the same error-reason string channel as a wrong type, so the handler renders
    it as a structured parameter error instead of the process wedging later.
    """
    if raw is None:
        return (), None
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        return (), f"{name} must be a list of strings"
    for pattern in raw:
        reason = _validate_pattern(pattern, name)
        if reason is not None:
            return (), reason
    return tuple(raw), None


def _validate_pattern(pattern: str, name: str) -> str | None:
    """Reject a pathological pattern at parse time; ``None`` when acceptable.

    Three cheap, dependency-free screens (SEC-1, A12): an over-long pattern, the
    nested-unbounded-quantifier shape, and a long optional-chain (many
    variable-length quantifiers) — all three drive exponential backtracking. This
    is a heuristic — it does not catch every catastrophic construction — so it is
    NOT the safety bound; it only turns the obvious wedges into a clear typed
    error at parse time. What bounds the rest is the per-match wall-clock budget
    applied by ``_resolve_matcher`` (RG1): the per-line input cap does NOT bound
    backtracking, only input length. Normal filters (``ERROR.*``, ``*.py``,
    ``^\\d+$``) have none of these shapes and pass untouched.

    The fourth screen IS a bound (B1): a pattern RE2 refuses
    (lookaround/backreferences) cannot be time-bounded off the main thread, and a
    wedged match on the MCP worker thread freezes the whole event loop, so it is
    rejected here instead of run unbounded. Only a pattern that is a valid Python
    regex RE2 declines is rejected — a glob like ``*.py`` is not a regex at all
    and is classified ``UNKNOWN``, so it still reaches the ``fnmatch`` path in
    ``_resolve_matcher``. With RE2 absent nothing is rejected on this basis.
    """
    if len(pattern) > _MAX_PATTERN_CHARS:
        return f"{name} pattern too long (>{_MAX_PATTERN_CHARS} chars): {pattern[:40]!r}…"
    if classify_boundability(pattern) is Boundability.UNBOUNDABLE:
        return (
            f"{name} pattern rejected: lookaround/backreferences cannot be "
            f"time-bounded off the main thread; anchor or rewrite the pattern "
            f"without lookaround: {pattern!r}"
        )
    if _NESTED_QUANTIFIER_RE.search(pattern):
        return (
            f"{name} pattern rejected: nested unbounded quantifier "
            f"(catastrophic-backtracking risk): {pattern!r}"
        )
    if count_variable_quantifiers(pattern) > _MAX_VARIABLE_QUANTIFIERS:
        return (
            f"{name} pattern rejected: too many variable-length quantifiers "
            f"(>{_MAX_VARIABLE_QUANTIFIERS}); an optional-chain like '.?' repeated "
            f"many times backtracks exponentially: {pattern!r}"
        )
    return None


def _resolve_matcher(pattern: str) -> Callable[[str], bool]:
    """Resolve ``pattern`` ONCE to a line matcher (regex-first, glob-fallback).

    A pattern that compiles as a regex is applied with ``re.search`` (partial
    match, the natural "does this line contain X" semantics). A pattern that
    does NOT compile as a regex (e.g. ``*.py``, whose leading ``*`` is invalid
    regex) is matched with ``fnmatch`` glob semantics. Deterministic: the same
    pattern always resolves to the same matcher.

    ReDoS bounds: a line longer than ``_MAX_REGEX_LINE_CHARS`` is NOT fed to the
    matcher at all — it resolves to ``False`` (no match). The cap is applied
    before BOTH branches because ``fnmatch`` also translates to a regex under the
    hood. The cap bounds the line LENGTH but NOT backtracking WIDTH, so within
    the cap the match additionally runs under a wall-clock budget (RG1) — a
    pattern like ``(a|b|ab)+Z`` passes every parse-time screen and still
    backtracks exponentially on an 80-character line. A match that exceeds the
    budget resolves to ``False``; see :mod:`furl_ctx.ccr.regex_budget`. This is a
    raw match verdict only; how an over-long line is *treated* for eligibility
    (protected, not compressed) is decided by ``line_is_eligible``, which caps the
    line independently. Realistic filter content is far shorter, so neither bound
    fires in normal use.
    """
    try:
        compiled = re.compile(pattern)
    except re.error:
        return lambda text: len(text) <= _MAX_REGEX_LINE_CHARS and fnmatch.fnmatch(text, pattern)
    return lambda text: len(text) <= _MAX_REGEX_LINE_CHARS and matches_within_budget(compiled, text)


def _pattern_matches(pattern: str, text: str) -> bool:
    """Match ``text`` against ``pattern`` (regex-first, glob-fallback).

    Thin wrapper over :func:`_resolve_matcher` so there is a single matching
    code path; kept for the per-pattern call sites and the unit tests. Hot loops
    resolve the matcher once via :func:`_resolve_matcher` instead of re-resolving
    per line.
    """
    return _resolve_matcher(pattern)(text)


@dataclass(frozen=True)
class _ResolvedMatchers:
    """Include/exclude patterns compiled to line matchers once (SEC-1).

    Built by :meth:`SectionPatterns._resolve_matchers` and threaded through the
    per-line loop so the compilation cost (and the input-cap wrapper) is paid a
    single time per ``partition_content`` call, not once per line.
    """

    include: tuple[Callable[[str], bool], ...]
    exclude: tuple[Callable[[str], bool], ...]


@dataclass(frozen=True)
class _LineRun:
    """A maximal contiguous run of lines that are all eligible or all protected."""

    eligible: bool
    text: str


def partition_content(content: str, patterns: SectionPatterns) -> list[_LineRun]:
    """Split ``content`` into ordered maximal eligible/protected line runs.

    Splitting on ``\\n`` and rejoining the runs with ``\\n`` reproduces the
    original string exactly when the runs are concatenated (the boundaries fall
    between lines), so protected bytes and overall order are preserved.
    """
    lines = content.split("\n")
    matchers = patterns._resolve_matchers()  # compile once, reuse per line (SEC-1)
    runs: list[_LineRun] = []
    current_eligible: bool | None = None
    current: list[str] = []

    for line in lines:
        eligible = patterns.line_is_eligible(line, matchers)
        if current_eligible is None:
            current_eligible = eligible
        if eligible != current_eligible:
            runs.append(_LineRun(eligible=current_eligible, text="\n".join(current)))
            current = []
            current_eligible = eligible
        current.append(line)

    if current_eligible is not None:
        runs.append(_LineRun(eligible=current_eligible, text="\n".join(current)))
    return runs
