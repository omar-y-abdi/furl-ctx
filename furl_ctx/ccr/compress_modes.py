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
from dataclasses import dataclass
from enum import Enum
from typing import Any


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

    def line_is_eligible(self, line: str) -> bool:
        """Whether a content line is eligible for compression.

        Protected (ineligible) when it matches any exclude pattern, or when
        ``include`` is non-empty and it matches none of the include patterns.
        """
        if any(_pattern_matches(pattern, line) for pattern in self.exclude):
            return False
        if self.include:
            return any(_pattern_matches(pattern, line) for pattern in self.include)
        return True


def _parse_pattern_list(raw: Any, name: str) -> tuple[tuple[str, ...], str | None]:
    """Validate one optional pattern-list argument into a tuple of strings."""
    if raw is None:
        return (), None
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        return (), f"{name} must be a list of strings"
    return tuple(raw), None


def _pattern_matches(pattern: str, text: str) -> bool:
    """Match ``text`` against ``pattern`` as regex-first, glob-fallback.

    A pattern that compiles as a regex is applied with ``re.search`` (partial
    match, the natural "does this line contain X" semantics). A pattern that
    does NOT compile as a regex (e.g. ``*.py``, whose leading ``*`` is invalid
    regex) is matched with ``fnmatch`` glob semantics. Deterministic: the same
    pattern always resolves to the same matcher.
    """
    try:
        compiled = re.compile(pattern)
    except re.error:
        return fnmatch.fnmatch(text, pattern)
    return compiled.search(text) is not None


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
    runs: list[_LineRun] = []
    current_eligible: bool | None = None
    current: list[str] = []

    for line in lines:
        eligible = patterns.line_is_eligible(line)
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
