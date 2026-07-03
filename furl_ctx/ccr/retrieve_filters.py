"""Filters for per-hash ``furl_retrieve`` (NR2-2 feature b).

A retrieved original can be large. These filters let a caller narrow what
comes back WITHOUT a second round-trip, while keeping the unfiltered path
byte-identical to a plain retrieve (an empty ``RetrieveFilters`` is a no-op the
handler never even calls into here).

Design (type-driven):

* ``RetrieveFilters`` is the parsed, validated request. Its smart constructor
  ``RetrieveFilters.parse`` is the ONLY boundary that turns the loose MCP
  ``arguments`` dict into a filter spec — every invalid input (bad regex, a
  non-int line bound, an inverted range, a wrong-typed field list) becomes a
  ``FilterError`` there, so the apply step is total and never raises on user
  input.
* Two representations, matched to two filter families (the honest contract —
  they operate on genuinely different shapes of the SAME original):
    - ``pattern`` (regex, line-wise, with context lines) and ``line_range``
      operate on the original as TEXT LINES.
    - ``fields`` projects named keys out of a JSON ARRAY of objects.
  Composition: ``line_range`` narrows the line window first, then ``pattern``
  matches within it (both are line operations, so they compose left-to-right).
  ``fields`` is a structural projection over the parsed array and does not
  compose with the line filters — requesting ``fields`` together with a line
  filter is a ``FilterError`` (they describe incompatible views), and
  ``fields`` on an original that is not a JSON array is likewise a
  ``FilterError`` (never a silent empty result).

Effects: pure. No I/O, no store access, no logging. The store/handler layer
owns retrieval and its side effects; this module only transforms an
already-retrieved string.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# Bound the regex context window so a caller cannot request an unboundedly
# large context expansion. 0..50 is far past any real "show me around this
# match" need while keeping the projected output bounded.
_MAX_CONTEXT_LINES = 50


@dataclass(frozen=True)
class FilterError:
    """A user-facing reason a filter request was rejected.

    Carried as data (not raised) so the MCP handler renders it as a structured
    error envelope exactly like its other parameter-error paths — invalid
    input is the caller's to fix, never an internal server failure.
    """

    reason: str


@dataclass(frozen=True)
class RetrieveFilters:
    """A validated per-hash retrieve filter spec.

    Construct via :meth:`parse`; the raw constructor is not part of the public
    contract. ``is_empty`` is True when no filter was requested — the handler
    uses that to preserve the byte-identical unfiltered path.
    """

    pattern: re.Pattern[str] | None
    context_lines: int
    line_start: int | None
    line_end: int | None
    fields: tuple[str, ...] | None

    @property
    def is_empty(self) -> bool:
        """True when no filter dimension was requested (pure passthrough)."""
        return (
            self.pattern is None
            and self.line_start is None
            and self.line_end is None
            and self.fields is None
        )

    @property
    def has_line_filter(self) -> bool:
        return self.pattern is not None or self.line_start is not None or self.line_end is not None

    @classmethod
    def parse(cls, arguments: dict[str, Any]) -> RetrieveFilters | FilterError:
        """Validate the loose MCP ``arguments`` into a filter spec.

        Returns a ``FilterError`` (never raises) for any malformed input:
        non-string ``pattern``, uncompilable regex, non-int / negative
        ``context_lines``, non-int line bounds, an inverted or non-positive
        ``line_range``, a ``fields`` value that is not a list of strings, or
        the incompatible ``fields`` + line-filter combination.
        """
        pattern_raw = arguments.get("pattern")
        pattern: re.Pattern[str] | None = None
        if pattern_raw is not None:
            if not isinstance(pattern_raw, str):
                return FilterError(f"pattern must be a string, got {type(pattern_raw).__name__}")
            try:
                pattern = re.compile(pattern_raw)
            except re.error as exc:
                return FilterError(f"invalid regex in pattern: {exc}")

        context_raw = arguments.get("context_lines", 0)
        if isinstance(context_raw, bool) or not isinstance(context_raw, int):
            return FilterError(
                f"context_lines must be an integer, got {type(context_raw).__name__}"
            )
        if context_raw < 0:
            return FilterError(f"context_lines must be >= 0, got {context_raw}")
        if context_raw > _MAX_CONTEXT_LINES:
            return FilterError(f"context_lines must be <= {_MAX_CONTEXT_LINES}, got {context_raw}")

        line_range_raw = arguments.get("line_range")
        line_start, line_end, range_err = _parse_line_range(line_range_raw)
        if range_err is not None:
            return range_err

        fields_raw = arguments.get("fields")
        fields: tuple[str, ...] | None = None
        if fields_raw is not None:
            if not isinstance(fields_raw, list) or not all(isinstance(f, str) for f in fields_raw):
                return FilterError("fields must be a list of strings")
            if not fields_raw:
                return FilterError("fields must not be empty when provided")
            fields = tuple(fields_raw)

        has_line_filter = pattern is not None or line_start is not None or line_end is not None
        if fields is not None and has_line_filter:
            return FilterError(
                "fields cannot be combined with pattern/line_range: fields projects "
                "a JSON array while pattern/line_range operate on text lines. "
                "Use one representation per call."
            )

        return cls(
            pattern=pattern,
            context_lines=context_raw,
            line_start=line_start,
            line_end=line_end,
            fields=fields,
        )


def _parse_line_range(
    raw: Any,
) -> tuple[int | None, int | None, FilterError | None]:
    """Validate an optional ``line_range`` argument.

    Accepts a ``[start, end]`` two-element list of 1-based inclusive line
    numbers (either bound may be ``null`` for an open end). Returns
    ``(start, end, None)`` on success or ``(None, None, FilterError)`` on any
    malformed shape.
    """
    if raw is None:
        return None, None, None
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return (
            None,
            None,
            FilterError("line_range must be a [start, end] pair (1-based, inclusive)"),
        )

    def _bound(value: Any, name: str) -> tuple[int | None, FilterError | None]:
        if value is None:
            return None, None
        if isinstance(value, bool) or not isinstance(value, int):
            return None, FilterError(
                f"line_range {name} must be an integer or null, got {type(value).__name__}"
            )
        if value < 1:
            return None, FilterError(f"line_range {name} must be >= 1, got {value}")
        return value, None

    start, start_err = _bound(raw[0], "start")
    if start_err is not None:
        return None, None, start_err
    end, end_err = _bound(raw[1], "end")
    if end_err is not None:
        return None, None, end_err
    if start is not None and end is not None and end < start:
        return (
            None,
            None,
            FilterError(f"line_range end ({end}) must be >= start ({start})"),
        )
    return start, end, None


@dataclass(frozen=True)
class FilteredContent:
    """Successful filter result: the projected text plus what it represents."""

    content: str
    kind: str  # "lines" | "fields"
    matched_count: int
    total_count: int


def apply_filters(original_content: str, filters: RetrieveFilters) -> FilteredContent | FilterError:
    """Apply a validated filter spec to a retrieved original. Total.

    Returns ``FilteredContent`` on success or a ``FilterError`` when the
    original's shape is incompatible with the requested filter (``fields`` on a
    non-array). Never raises on the already-validated spec.
    """
    if filters.fields is not None:
        return _project_fields(original_content, filters.fields)
    return _filter_lines(original_content, filters)


def _filter_lines(original_content: str, filters: RetrieveFilters) -> FilteredContent:
    """Apply ``line_range`` then ``pattern`` (with context) over text lines.

    Line numbering is 1-based and inclusive, matching the ``furl_read`` tool's
    numbered output so a caller can round-trip a range it saw there. Splitting
    on ``\\n`` (without a trailing-empty trim) keeps line indices exact.
    """
    lines = original_content.split("\n")
    total = len(lines)

    # 1-based inclusive window; open bounds default to the full extent.
    start = filters.line_start if filters.line_start is not None else 1
    end = filters.line_end if filters.line_end is not None else total
    # Clamp to the available lines (an in-range-but-past-EOF end is not an
    # error — it simply yields whatever exists; the smart constructor already
    # rejected inverted/<1 ranges).
    start = max(1, start)
    end = min(total, end)
    if start > total:
        # The requested window begins past the last line — no lines to show.
        windowed: list[tuple[int, str]] = []
    else:
        windowed = [(i + 1, lines[i]) for i in range(start - 1, end)]

    if filters.pattern is None:
        selected = windowed
    else:
        selected = _select_matching_with_context(windowed, filters.pattern, filters.context_lines)

    rendered = "\n".join(f"{num}:{text}" for num, text in selected)
    return FilteredContent(
        content=rendered,
        kind="lines",
        matched_count=len(selected),
        total_count=total,
    )


def _select_matching_with_context(
    windowed: list[tuple[int, str]],
    pattern: re.Pattern[str],
    context_lines: int,
) -> list[tuple[int, str]]:
    """Return windowed lines matching ``pattern`` plus ``context_lines`` around
    each match, de-duplicated and in original order.

    Context is computed against the WINDOW (the post-``line_range`` slice), so a
    range filter is a hard boundary the context cannot leak past — the two
    filters compose without one silently overriding the other.
    """
    match_indices = [idx for idx, (_num, text) in enumerate(windowed) if pattern.search(text)]
    if not match_indices:
        return []
    keep: set[int] = set()
    last = len(windowed) - 1
    for idx in match_indices:
        low = max(0, idx - context_lines)
        high = min(last, idx + context_lines)
        keep.update(range(low, high + 1))
    return [windowed[i] for i in sorted(keep)]


def _project_fields(
    original_content: str, fields: tuple[str, ...]
) -> FilteredContent | FilterError:
    """Project ``fields`` out of a JSON array of objects.

    ``fields`` is meaningful only for a JSON array; anything else is a
    ``FilterError`` (never a silent empty result). Each element that is an
    object is reduced to the requested keys (absent keys are simply omitted
    from that element — a projection, not a lookup that must hit); non-object
    elements are dropped from the projection with the count reflecting how many
    of the array's elements were projectable.
    """
    try:
        parsed = json.loads(original_content)
    except json.JSONDecodeError:
        return FilterError(
            "fields filter requires a JSON-array original, but the stored content is not valid JSON"
        )
    if not isinstance(parsed, list):
        return FilterError(
            "fields filter requires a JSON-array original, but the stored content "
            f"is a JSON {_json_kind(parsed)}"
        )

    projected: list[dict[str, Any]] = []
    for element in parsed:
        if not isinstance(element, dict):
            continue
        projected.append({key: element[key] for key in fields if key in element})

    rendered = json.dumps(projected, ensure_ascii=False, indent=2)
    return FilteredContent(
        content=rendered,
        kind="fields",
        matched_count=len(projected),
        total_count=len(parsed),
    )


def _json_kind(value: Any) -> str:
    """Human-readable JSON kind name for an error message."""
    if isinstance(value, dict):
        return "object"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return type(value).__name__
