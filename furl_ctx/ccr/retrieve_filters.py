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
* Three representations, matched to three filter families (the honest contract —
  they operate on genuinely different shapes of the SAME original):
    - ``pattern`` (regex, line-wise, with context lines) and ``line_range``
      operate on the original as TEXT LINES.
    - ``fields`` projects named keys out of a JSON ARRAY of objects.
    - the SELECT family (``select_field`` + ``select_equals`` OR
      ``select_min``/``select_max``, with an optional ``limit``) keeps the ROWS
      of a JSON array of objects whose ``select_field`` equals a value or falls
      in a numeric range. It reads both a top-level array AND a JSON object with
      exactly one dominant inner array (the Chrome-trace
      ``{"metadata":{}, "traceEvents":[…]}`` shape), so an offloaded object with
      a dominant array is sliceable without a second round-trip.
  Composition: ``line_range`` narrows the line window first, then ``pattern``
  matches within it (both are line operations, so they compose left-to-right).
  ``fields`` is a structural projection over the parsed array and does not
  compose with the line filters — requesting ``fields`` together with a line
  filter is a ``FilterError`` (they describe incompatible views), and
  ``fields`` on an original that is not a JSON array is likewise a
  ``FilterError`` (never a silent empty result). SELECT is likewise structural:
  it composes with ``fields`` (project columns of the selected rows) but NOT
  with ``pattern``/``line_range`` (a ``FilterError``, mirroring the ``fields``
  rule), and on an original with no usable array it is a ``FilterError`` (never
  a silent empty result).

Effects: pure. No I/O, no store access, no logging. The store/handler layer
owns retrieval and its side effects; this module only transforms an
already-retrieved string.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import chain
from typing import Any, TypeGuard

from furl_ctx.ccr.compress_modes import (
    _MAX_PATTERN_CHARS,
    _MAX_REGEX_LINE_CHARS,
    _MAX_VARIABLE_QUANTIFIERS,
    _NESTED_QUANTIFIER_RE,
    count_variable_quantifiers,
)
from furl_ctx.ccr.regex_budget import Boundability, classify_boundability, matches_within_budget
from furl_ctx.ccr.retrieve_spans import array_element_spans

# Bound the regex context window so a caller cannot request an unboundedly
# large context expansion. 0..50 is far past any real "show me around this
# match" need while keeping the projected output bounded.
_MAX_CONTEXT_LINES = 50

# ReDoS guards (SEC-2): the bounds + nested-quantifier heuristic are shared with
# ``compress_modes`` (SEC-1) — see the rationale there; validators stay separate.

# Bound a row-select ("give me the matching rows") so a slice can never dump the
# whole array back — the very failure sliceable retrieve exists to avoid. 1000
# rows is far past any "show me the anomalies" need while keeping the projected
# JSON a few hundred KB at most; a caller wanting more raises ``limit``
# explicitly. Applied only when a select is requested without its own limit.
_DEFAULT_SELECT_LIMIT = 1000

# Bounds on the F7 absent-field hint. Field names are attacker-influenced STORED
# data, and echoing them unbounded lets a wide array or a single multi-KB key make
# the response LARGER than the original the tool exists to shrink. The names surface
# only in the structured ``known_fields`` array, never the ``note`` prose, and three
# caps are enforced together on that array, so
# the hint stays small no matter the input: at most ``_MAX_KNOWN_FIELDS`` names,
# each truncated to ``_MAX_KNOWN_FIELD_NAME_CHARS`` chars, and the shown set bounded
# to ``_MAX_KNOWN_FIELDS_TOTAL_CHARS`` chars cumulatively. 50 covers a realistic
# schema's full key set; 64 chars identifies a long hash/base64 key by its prefix;
# 512 cumulative chars keeps the whole signal on the order of a kilobyte, far below
# the size the tool keeps outputs under. Names are sorted before capping, so the
# shown subset is deterministic and stable, and the remainder counts into ``elided``.
_MAX_KNOWN_FIELDS = 50
_MAX_KNOWN_FIELD_NAME_CHARS = 64
_MAX_KNOWN_FIELDS_TOTAL_CHARS = 512

# Distinguishes an ABSENT key from a key PRESENT with a null value in a row-select
# (F7 correctness). ``dict.get(field)`` returns ``None`` for both, which made
# ``select_equals=None`` match rows that merely lack the key AND made a criterion-
# less ``select_field`` on an absent key match every row while the warning claimed
# the result was empty. Passing ``_MISSING`` for an absent key — and treating it as
# equal to nothing, not even null — separates the two: ``select_equals=null`` now
# means "present and null", never "key absent". Never leaves this module.
_MISSING: Any = object()


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
    select_field: str | None
    select_equals: Any | None
    select_min: float | None
    select_max: float | None
    limit: int | None
    # F10: return each matched row's byte-exact source span instead of a
    # re-serialization. A render modifier on a row-select, not a filter dimension
    # (``is_empty`` ignores it): the smart constructor rejects ``raw`` without a
    # ``select_field``, so it never appears on an otherwise-empty spec. Defaults
    # off, keeping the re-serialized output byte-identical to before.
    raw: bool = False

    @property
    def is_empty(self) -> bool:
        """True when no filter dimension was requested (pure passthrough).

        ``raw`` is intentionally NOT part of this test: it is a rendering choice
        over a row-select, and ``parse`` rejects it without ``select_field``, so
        a ``raw`` spec always carries a real filter and is never "empty".
        """
        return (
            self.pattern is None
            and self.line_start is None
            and self.line_end is None
            and self.fields is None
            and not self.has_select
        )

    @property
    def has_select(self) -> bool:
        """True when a row-select was requested. ``select_field`` is the anchor:
        the smart constructor rejects any ``select_*`` value without it, so its
        presence alone is the honest test for the whole SELECT family."""
        return self.select_field is not None

    @classmethod
    def parse(cls, arguments: dict[str, Any]) -> RetrieveFilters | FilterError:
        """Validate the loose MCP ``arguments`` into a filter spec.

        Returns a ``FilterError`` (never raises) for any malformed input:
        non-string ``pattern``, uncompilable regex, non-int / negative
        ``context_lines``, non-int line bounds, an inverted or non-positive
        ``line_range``, a ``fields`` value that is not a list of strings, the
        incompatible ``fields`` + line-filter combination, or an invalid SELECT
        request (``select_*`` without ``select_field``, ``select_equals``
        together with a range, a container ``select_equals``, a non-numeric or
        inverted range, a non-positive ``limit``, or SELECT combined with a line
        filter).
        """
        pattern_raw = arguments.get("pattern")
        pattern: re.Pattern[str] | None = None
        if pattern_raw is not None:
            if not isinstance(pattern_raw, str):
                return FilterError(f"pattern must be a string, got {type(pattern_raw).__name__}")
            reject = _reject_pathological_pattern(pattern_raw)
            if reject is not None:
                return reject
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

        select = _parse_select(arguments, fields_present=fields is not None)
        if isinstance(select, FilterError):
            return select
        select_field, select_equals, select_min, select_max, limit = select
        if select_field is not None and has_line_filter:
            return FilterError(
                "select_field cannot be combined with pattern/line_range: select "
                "keeps rows of a JSON array while pattern/line_range operate on "
                "text lines. Use one representation per call."
            )

        raw = _parse_raw(arguments, select_field=select_field, fields_present=fields is not None)
        if isinstance(raw, FilterError):
            return raw

        return cls(
            pattern=pattern,
            context_lines=context_raw,
            line_start=line_start,
            line_end=line_end,
            fields=fields,
            select_field=select_field,
            select_equals=select_equals,
            select_min=select_min,
            select_max=select_max,
            limit=limit,
            raw=raw,
        )


def _reject_pathological_pattern(pattern: str) -> FilterError | None:
    """Reject a ReDoS-pathological pattern at parse time; ``None`` if fine.

    Three cheap, dependency-free screens (SEC-2, A12): an over-long pattern, the
    nested-unbounded-quantifier shape, and a long optional-chain (many
    variable-length quantifiers). Heuristic — it does not catch every
    catastrophic construction — but it turns the obvious wedges into a
    ``FilterError`` (the caller's to fix, never a hang). The optional-chain screen
    closes the short-line gap the long-line literal-only path (F3) does not cover:
    an ``.?`` repeated dozens of times is under the length cap and carries no
    nested quantifier, yet backtracks exponentially on a line WITHIN
    ``_MAX_REGEX_LINE_CHARS`` (the input cap bounds length, not backtracking
    width). Normal patterns have none of these shapes and pass untouched.

    These screens are DEFENSE-IN-DEPTH, not the bound (RG1): they are syntactic,
    and ``(a|b|ab)+Z`` passes all three while still backtracking exponentially.
    The actual bound is the per-match budget applied in ``_line_matches``.

    The FOURTH screen is the bound for the one case the budget cannot cover (B1):
    a pattern RE2 refuses (lookaround, a backreference, or a bounded repetition
    above 1000) cannot be time-bounded on a worker thread, where the MCP server
    runs this match, and a wedged match there freezes the whole event loop for
    every session on the process. Such a pattern is rejected here rather than
    accepted and run unbounded. When RE2 is absent boundability is not knowable,
    so nothing is rejected on that basis; see
    :func:`furl_ctx.ccr.regex_budget.classify_boundability`.
    """
    if len(pattern) > _MAX_PATTERN_CHARS:
        return FilterError(
            f"pattern too long (>{_MAX_PATTERN_CHARS} chars); "
            "narrow the pattern to avoid catastrophic backtracking"
        )
    if classify_boundability(pattern) is Boundability.UNBOUNDABLE:
        return FilterError(
            "pattern rejected: lookaround, a backreference, or a bounded "
            "repetition larger than RE2 allows (max 1000, e.g. a{0,2000}) cannot "
            "be time-bounded off the main thread; remove the "
            "lookaround/backreference or lower the repetition to 1000 or less"
        )
    if _NESTED_QUANTIFIER_RE.search(pattern):
        return FilterError(
            "pattern rejected: nested unbounded quantifier "
            "(catastrophic-backtracking risk); rewrite without a nested +/*"
        )
    if count_variable_quantifiers(pattern) > _MAX_VARIABLE_QUANTIFIERS:
        return FilterError(
            f"pattern rejected: too many variable-length quantifiers "
            f"(>{_MAX_VARIABLE_QUANTIFIERS}); an optional-chain like '.?' repeated "
            "many times backtracks exponentially; narrow or anchor the pattern"
        )
    return None


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


# The parsed SELECT tuple: (select_field, select_equals, select_min, select_max,
# limit). ``select_equals`` is ``Any`` because a caller may match any JSON scalar
# (str/int/float/bool/None); a container is rejected at parse.
_SelectSpec = tuple[str | None, Any | None, float | None, float | None, int | None]


def _parse_select(arguments: dict[str, Any], *, fields_present: bool) -> _SelectSpec | FilterError:
    """Validate the SELECT family from the loose ``arguments``. Fail-closed.

    ``select_field`` is the row-select anchor: ``select_equals`` /
    ``select_min`` / ``select_max`` without it is a ``FilterError`` (a
    range/equals with no field is meaningless). A ``select_field`` without any
    equality or range criterion is likewise rejected instead of implicitly
    selecting null-valued rows. ``select_equals`` and a range
    (``select_min``/``select_max``) are mutually exclusive — equality OR range,
    never both. ``select_equals`` must be a JSON scalar; a container (list/dict)
    is rejected because comparing a cell to a container is not a row-select.
    Range bounds must be real numbers (``bool`` excluded — ``True`` is not the
    number 1 here) and ordered.

    ``limit`` (F-alpha3) is a general positive-int row bound (``bool`` excluded).
    It bounds EITHER a ``select_field`` row-select OR a ``fields`` projection
    (``fields_present``): a select without an explicit ``limit`` defaults to
    :data:`_DEFAULT_SELECT_LIMIT`, while a ``fields`` projection stays unbounded
    unless a ``limit`` is given. A ``limit`` with neither a ``select_field`` nor
    ``fields`` (nothing to bound) is a truthful ``FilterError``.
    """
    field_raw = arguments.get("select_field")
    equals_present = "select_equals" in arguments
    equals_raw = arguments.get("select_equals")
    min_raw = arguments.get("select_min")
    max_raw = arguments.get("select_max")
    limit_raw = arguments.get("limit")

    has_range = min_raw is not None or max_raw is not None
    any_select_anchor = field_raw is not None or equals_present or has_range
    if not any_select_anchor and limit_raw is None:
        return (None, None, None, None, None)

    limit, limit_err = _parse_limit(limit_raw)
    if limit_err is not None:
        return limit_err

    if field_raw is None:
        if equals_present or has_range:
            return FilterError(
                "select_field is required for select_equals / select_min / "
                "select_max; it names the field to match on"
            )
        # Only a bare ``limit`` reached here. It bounds a fields projection or a
        # row-select; with neither there are no rows for it to bound.
        if not fields_present:
            return FilterError(
                "limit needs a row-producing filter: pass 'fields' to bound a "
                "projection or 'select_field' to bound a row-select; a line "
                "window is bounded by line_range instead"
            )
        return (None, None, None, None, limit)

    if not isinstance(field_raw, str):
        return FilterError(f"select_field must be a string, got {type(field_raw).__name__}")

    if equals_present and has_range:
        return FilterError(
            "select_equals and select_min/select_max are mutually exclusive: "
            "match a field by equality OR by numeric range, not both"
        )

    if not equals_present and not has_range:
        return FilterError(
            "select_field requires either select_equals (equality) or "
            "select_min/select_max (numeric range) to specify matching criteria"
        )

    select_equals: Any | None = None
    if equals_present:
        if isinstance(equals_raw, (list, dict)):
            return FilterError(
                "select_equals must be a JSON scalar (string, number, boolean, "
                f"or null), got {type(equals_raw).__name__}"
            )
        select_equals = equals_raw

    select_min, min_err = _parse_bound(min_raw, "select_min")
    if min_err is not None:
        return min_err
    select_max, max_err = _parse_bound(max_raw, "select_max")
    if max_err is not None:
        return max_err
    if select_min is not None and select_max is not None and select_max < select_min:
        return FilterError(f"select_max ({select_max}) must be >= select_min ({select_min})")

    if limit is None:
        limit = _DEFAULT_SELECT_LIMIT

    return (field_raw, select_equals, select_min, select_max, limit)


def _parse_bound(raw: Any, name: str) -> tuple[float | None, FilterError | None]:
    """A numeric SELECT range bound: a real number (``bool`` excluded), or None
    for an open end. ``True``/``False`` are not numbers here — a boolean field is
    an equals-match category, never a range endpoint."""
    if raw is None:
        return None, None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, FilterError(f"{name} must be a number or null, got {type(raw).__name__}")
    return float(raw), None


def _parse_limit(raw: Any) -> tuple[int | None, FilterError | None]:
    """A SELECT ``limit``: a positive int (``bool`` excluded), or None to take
    the bounded default. Zero/negative is rejected — a slice with no rows is a
    narrower filter's job, not a limit of 0."""
    if raw is None:
        return None, None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, FilterError(f"limit must be an integer, got {type(raw).__name__}")
    if raw < 1:
        return None, FilterError(f"limit must be >= 1, got {raw}")
    return raw, None


def _parse_raw(
    arguments: dict[str, Any], *, select_field: str | None, fields_present: bool
) -> bool | FilterError:
    """Validate the F10 ``raw`` flag against what it can honestly render.

    ``raw`` returns each matched ROW's byte-exact source span, so it is coherent
    ONLY over a whole-row select: it REQUIRES ``select_field`` (nothing else
    produces source-sliceable rows) and REJECTS ``fields`` (a projection returns
    a subset of a row's keys, which never had a contiguous source span). A
    non-boolean value is a typed error, not a truthiness guess. Absent/false ⇒
    ``False`` ⇒ the re-serialized default, byte-identical to before.
    """
    raw_value = arguments.get("raw")
    if raw_value is None:
        return False
    if not isinstance(raw_value, bool):
        return FilterError(f"raw must be a boolean, got {type(raw_value).__name__}")
    if not raw_value:
        return False
    if select_field is None:
        return FilterError(
            "raw requires select_field: it returns the byte-exact source span of "
            "each matched row, which only a row-select produces. Add select_field, "
            "or drop raw to get the re-serialized projection."
        )
    if fields_present:
        return FilterError(
            "raw cannot be combined with fields: a projection returns a subset of "
            "each row's keys, which has no byte-exact source span. Use raw for the "
            "whole matched rows, or fields for a projected view, not both."
        )
    return True


@dataclass(frozen=True)
class AbsentSelectField:
    """F7 honesty signal: the ``select_field`` of a row-select named a key that
    is present in NO row of the array, so the empty result is a misnamed field,
    not a value that matched nothing.

    ``known_fields`` is the deterministically-sorted union of the keys that ARE
    present, made safe to echo because field names are attacker-influenced stored
    data. It is bounded on THREE axes at once (:func:`_cap_known_fields`): at most
    :data:`_MAX_KNOWN_FIELDS` (50) names, each sanitized and truncated to
    :data:`_MAX_KNOWN_FIELD_NAME_CHARS` (64) chars, the shown set capped to
    :data:`_MAX_KNOWN_FIELDS_TOTAL_CHARS` (512) chars cumulatively. So the whole
    signal — ``known_fields`` plus the generic ``note`` plus the echoed ``field``
    — adds on the order of a kilobyte to the response regardless of how wide or
    long-keyed the array is; it can never approach the size of the data it
    describes. ``elided`` counts the names the caps dropped, so a truncated list
    never reads as complete. ``field`` is a sanitized, length-bounded copy of the
    requested select_field. The discovered names surface ONLY here in
    ``known_fields`` (sanitized), never woven into the ``note`` prose, so a hostile
    key is read as data in a structured list, not as guidance in a sentence.
    """

    field: str
    known_fields: tuple[str, ...]
    elided: int


@dataclass(frozen=True)
class FilteredContent:
    """Successful filter result: the projected text plus what it represents.

    ``lines_skipped_over_cap`` and ``note`` are the F-alpha1 honesty signal for
    the "lines" kind: a non-literal ``pattern`` is never run on a line longer
    than :data:`_MAX_REGEX_LINE_CHARS` (the ReDoS input cap), so a match on such
    a line cannot be reported. When that cap skipped one or more lines,
    ``lines_skipped_over_cap`` counts them and ``note`` explains the miss, so an
    agent sees a signalled gap instead of a bare ``matched_count`` of 0. Both
    default to the no-skip values, so every other path stays byte-identical.

    ``select_field_absent`` (F7) is the SAME honesty pattern for the "rows" kind:
    set only when a row-select's field is absent from every row, it carries the
    known field names so the caller can tell a typo from a genuine empty result.
    When set, ``note`` also carries the prose explanation, exactly as the
    over-cap signal does — one signalled gap, never a bare ``matched_count`` of
    0. It defaults to ``None``, so every non-absent result stays byte-identical.
    """

    content: str
    kind: str  # "lines" | "fields" | "rows"
    matched_count: int
    total_count: int
    lines_skipped_over_cap: int = 0
    note: str | None = None
    select_field_absent: AbsentSelectField | None = None


def apply_filters(original_content: str, filters: RetrieveFilters) -> FilteredContent | FilterError:
    """Apply a validated filter spec to a retrieved original. Total.

    Returns ``FilteredContent`` on success or a ``FilterError`` when the
    original's shape is incompatible with the requested filter (``fields`` or a
    row-select on an original with no usable JSON array). Never raises on the
    already-validated spec.
    """
    if filters.has_select:
        return _select_rows(original_content, filters)
    if filters.fields is not None:
        return _project_fields(original_content, filters.fields, filters.limit)
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

    skipped_over_cap = 0
    if filters.pattern is None:
        selected = windowed
    else:
        selected = _select_matching_with_context(windowed, filters.pattern, filters.context_lines)
        # F-alpha1 honesty signal: a NON-literal pattern is never run on a line
        # longer than the cap (``_line_matches`` returns no-match there without
        # searching), so a real match on such a line is unreportable. Count those
        # lines so the miss is signalled, not a bare zero. A pure literal DOES
        # search an over-cap line by substring, so it skips nothing.
        if _pattern_literal_text(filters.pattern) is None:
            skipped_over_cap = sum(
                1 for _num, text in windowed if len(text) > _MAX_REGEX_LINE_CHARS
            )

    rendered = "\n".join(f"{num}:{text}" for num, text in selected)
    return FilteredContent(
        content=rendered,
        kind="lines",
        matched_count=len(selected),
        total_count=total,
        lines_skipped_over_cap=skipped_over_cap,
        note=_over_cap_note(skipped_over_cap) if skipped_over_cap else None,
    )


def _over_cap_note(skipped_count: int) -> str:
    """The machine-readable warning attached when the regex line cap skipped
    lines from a non-literal pattern search. AI-TELL-FREE prose, no round
    brackets or dashes."""
    return (
        f"{skipped_count} lines longer than the {_MAX_REGEX_LINE_CHARS}-character "
        "regex line cap were not searched by this non-literal pattern, so a match "
        "on them cannot be reported. Search the long line with a pure literal "
        "substring, or narrow the view with line_range."
    )


# The regex metacharacters. A pattern containing NONE of them is a pure
# LITERAL: it matches by plain substring containment — linear in the input
# length, with no backtracking — so it is safe to search a line of any length.
# Every other pattern hands control to the regex engine, whose backtracking CAN
# be superlinear, so it is confined to lines within the per-line cap. A
# syntactic quantifier scan is NOT a sound substitute for this literal test
# (review RF1): a ``?``-chain such as ``a?a?…a?aaa…b`` carries no ``* + {`` yet
# backtracks exponentially, and a ``(?<!\\)`` look-behind misparses backslash
# parity so ``\*`` (a real quantifier) reads as "bounded".
_REGEX_METACHARACTERS = frozenset(".*+?{}[]()|^$\\")


def _pattern_literal_text(pattern: re.Pattern[str]) -> str | None:
    """The pattern's source string when it is a pure LITERAL (contains no regex
    metacharacter), else ``None``.

    A pure literal is matched by plain substring search — linear in the line
    length, no regex engine, no backtracking — so it is the ONLY pattern shape
    allowed to search a line longer than :data:`_MAX_REGEX_LINE_CHARS`. That is
    what lets a literal needle be found inside a single giant single-line blob (a
    minified JSON crash report stored as one line) instead of the caller being
    told ``matched_count=0`` for a substring that is unambiguously present
    (review F3). ANY metacharacter — a lone ``?``, an escaped ``\\*``, a class
    ``[…]`` — makes the pattern a regex that keeps the conservative per-line cap,
    so no adversarial construction can backtrack over an unbounded line (review
    RF1)."""
    source = pattern.pattern
    if any(ch in _REGEX_METACHARACTERS for ch in source):
        return None
    return source


def _line_matches(pattern: re.Pattern[str], literal: str | None, text: str) -> bool:
    """Whether ``text`` matches, honoring the RF1 long-line bound.

    Within the cap the full regex runs UNDER A WALL-CLOCK BUDGET (RG1): the input
    cap bounds the line LENGTH but not backtracking WIDTH, and the parse-time
    screens cannot bound it either -- ``(a|b|ab)+Z`` passes every screen and still
    backtracks exponentially on an 80-character line. A line whose match exceeds
    the budget is reported as NO match on that line rather than hanging; see
    :mod:`furl_ctx.ccr.regex_budget`. Beyond the cap a pure literal is matched by
    plain substring containment (``literal is not None``); any other pattern does
    NOT search the over-long line (the regex engine is never handed an unbounded
    input), so it reports no match there — the conservative pre-F3 per-line cap."""
    if len(text) <= _MAX_REGEX_LINE_CHARS:
        return matches_within_budget(pattern, text)
    if literal is not None:
        return literal in text
    return False


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
    # SEC-2 / RF1 input bound. A line within the cap is matched by the regex
    # engine under the RG1 wall-clock budget (the input cap bounds input LENGTH,
    # NOT backtracking width -- an earlier revision claimed "bounded input →
    # bounded backtracking", which ``(a|b|ab)+Z`` disproves on an 80-char line).
    # A line LONGER than the cap
    # is searched ONLY when the pattern is a pure literal, and then by plain
    # substring containment — linear in the line length, no regex engine, so no
    # backtracking is possible however long the line is. A non-literal (regex)
    # pattern keeps the conservative per-line cap on a long line, so an
    # adversarial pattern can never backtrack over an unbounded line. This still
    # finds a literal needle inside a single giant single-line blob — a
    # minified-JSON crash report stored as ONE line — which is the case review F3
    # fixed. Realistic multi-line content is unaffected: its lines are far
    # shorter than the cap and always searched.
    literal = _pattern_literal_text(pattern)
    match_indices = [
        idx for idx, (_num, text) in enumerate(windowed) if _line_matches(pattern, literal, text)
    ]
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
    original_content: str, fields: tuple[str, ...], limit: int | None = None
) -> FilteredContent | FilterError:
    """Project ``fields`` out of a JSON array of objects.

    ``fields`` is meaningful only for a JSON array; anything else is a
    ``FilterError`` (never a silent empty result). Each element that is an
    object is reduced to the requested keys (absent keys are simply omitted
    from that element — a projection, not a lookup that must hit); non-object
    elements are dropped from the projection with the count reflecting how many
    of the array's elements were projectable.

    ``limit`` (F-alpha3) bounds a ``fields``-only projection exactly as it bounds
    a row-select: without it the projection is unbounded (byte-identical to
    before), and with it only the first ``limit`` projected rows ship plus one
    explicit truncation-marker row so a truncated slice is never mistaken for the
    complete projection. ``matched_count`` stays the full projectable count.
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

    projected: list[dict[str, Any]] = [
        _project_row(element, fields) for element in parsed if isinstance(element, dict)
    ]

    matched_count = len(projected)
    if limit is not None and matched_count > limit:
        shown: list[dict[str, Any]] = list(projected[:limit])
        shown.append(
            {
                _TRUNCATION_KEY: (
                    f"showing {limit} of {matched_count} projected rows; raise 'limit' "
                    "or narrow the filter"
                )
            }
        )
    else:
        shown = projected

    rendered = json.dumps(shown, ensure_ascii=False, indent=2)
    return FilteredContent(
        content=rendered,
        kind="fields",
        matched_count=matched_count,
        total_count=len(parsed),
    )


def _project_row(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """A single row reduced to ``fields`` — a projection, not a lookup that must
    hit: an absent key is simply omitted from the returned copy. Never mutates
    ``row`` (builds a fresh dict), preserving the requested field order."""
    return {key: row[key] for key in fields if key in row}


# The explicit marker object appended when a row-select matches more rows than
# ``limit`` allows — so a truncated slice is never mistaken for the complete set.
# NON-RAW keeps the historic ``_truncated`` key (its output is byte-identical to
# before F10). A row genuinely named ``_truncated`` would be indistinguishable from
# the marker by key, but non-raw output is re-serialized JSON a caller reads, not
# hashes, so position suffices there. RAW is different: its whole purpose is a
# byte-exact slice a caller may HASH, where a synthetic element must be
# identifiable by key — so raw uses a namespaced ``__ccr_truncated__`` a real
# source key is far less likely to carry, and the raw schema documents stripping it.
_TRUNCATION_KEY = "_truncated"
_RAW_TRUNCATION_KEY = "__ccr_truncated__"


def _truncation_marker(limit: int, matched_count: int) -> dict[str, str]:
    """The explicit trailing marker object for a capped NON-RAW row-select (the
    caller sees the true match count and how to widen it). Byte-identical to the
    pre-F10 marker."""
    return {
        _TRUNCATION_KEY: (
            f"showing {limit} of {matched_count} matched rows; raise 'limit' or narrow the filter"
        )
    }


def _raw_truncation_marker(limit: int, matched_count: int) -> dict[str, str]:
    """The trailing marker for a TRUNCATED raw slice. Its namespaced
    ``__ccr_truncated__`` key makes it identifiable by KEY, not just position, and
    its message states it is synthetic — so a caller hashing the slice for a
    signature check knows this one element is not a source row and strips it."""
    return {
        _RAW_TRUNCATION_KEY: (
            f"synthetic marker, not a source row: showing {limit} of {matched_count} "
            "matched rows; raise 'limit' for the rest"
        )
    }


def _render_rows(matched: list[dict[str, Any]], limit: int, matched_count: int) -> str:
    """The default re-serialized row output. Byte-identical to the pre-F10
    behavior: the kept rows (capped, with a trailing truncation marker when the
    match count exceeds ``limit``) as pretty-printed JSON."""
    if matched_count > limit:
        shown: list[dict[str, Any]] = list(matched[:limit])
        shown.append(_truncation_marker(limit, matched_count))
    else:
        shown = matched
    return json.dumps(shown, ensure_ascii=False, indent=2)


def _render_raw(spans: list[str], limit: int, matched_count: int) -> str:
    """The F10 raw output: each matched row's byte-exact source span rejoined with
    FRESH ``[ , ]`` punctuation. Reading (i) — every ROW is byte-identical to its
    source, the surrounding structure is not. When the match count exceeds
    ``limit`` a single synthetic ``__ccr_truncated__`` marker object is appended: the
    ONE non-source element (namespaced so a caller hashing the slice can strip it by
    key), kept so a capped raw slice is never mistaken for the complete set."""
    shown = list(spans[:limit])
    if matched_count > limit:
        shown.append(json.dumps(_raw_truncation_marker(limit, matched_count), ensure_ascii=False))
    return "[" + ",".join(shown) + "]"


def _safe_field_name(name: str) -> str:
    """A field name made safe to echo into a model-facing hint. Field names are
    attacker-influenced stored content, so every non-printable character (newlines
    and control characters included) is replaced with a space — an injected line
    cannot break out of its quoting into the surrounding prose — and the result is
    truncated to :data:`_MAX_KNOWN_FIELD_NAME_CHARS` so one multi-KB key cannot
    bloat the hint. Sanitized names surface only in the structured ``known_fields``
    array, and the echoed ``field`` is JSON-quoted in the note, so every name reads
    as data, never as an instruction."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in name)
    return cleaned[:_MAX_KNOWN_FIELD_NAME_CHARS]


def _cap_known_fields(ordered: list[str]) -> tuple[tuple[str, ...], int]:
    """Bound the known-field hint by all three caps at once (see the constants): at
    most :data:`_MAX_KNOWN_FIELDS` names, each sanitized and truncated to
    :data:`_MAX_KNOWN_FIELD_NAME_CHARS`, and the shown set capped to
    :data:`_MAX_KNOWN_FIELDS_TOTAL_CHARS` chars cumulatively. Returns the shown
    tuple (sorted order preserved) and the count of names the caps dropped."""
    shown: list[str] = []
    total = 0
    for name in ordered:
        if len(shown) >= _MAX_KNOWN_FIELDS:
            break
        safe = _safe_field_name(name)
        if shown and total + len(safe) > _MAX_KNOWN_FIELDS_TOTAL_CHARS:
            break
        shown.append(safe)
        total += len(safe)
    return tuple(shown), len(ordered) - len(shown)


def _absent_select_field(field: str, rows: list[dict[str, Any]]) -> AbsentSelectField | None:
    """F7: the absent-field signal, or ``None`` when ``field`` is a real key of at
    least one row.

    Fires ONLY when ``field`` appears in the keys of NO row — the case a typo
    produces, byte-indistinguishable from a legitimate empty result without a
    signal. A field present in even one row stays silent: a value that matched
    nothing on a PRESENT field is a genuine empty answer, and warning there would
    train the caller to ignore the signal. The known names are the
    deterministically-sorted union of the rows' keys, sanitized and bounded by
    :func:`_cap_known_fields` so the hint stays small and safe to render; the
    requested ``field`` is sanitized the same way before it is echoed back.
    """
    # Iterating a dict yields its keys, so this is the union of the rows' keys.
    known: set[str] = set(chain.from_iterable(rows))
    if field in known:
        return None
    shown, elided = _cap_known_fields(sorted(known))
    return AbsentSelectField(field=_safe_field_name(field), known_fields=shown, elided=elided)


def _absent_field_note(absent: AbsentSelectField) -> str:
    """The prose companion to :class:`AbsentSelectField`, mirroring
    :func:`_over_cap_note`: AI-TELL-FREE, no round brackets or dashes. Deliberately
    GENERIC: it names NOTHING from the stored data. The discovered key names live
    ONLY in the structured ``known_fields`` array, never woven into this sentence.
    Names sitting in a list read as data; the same names inside an advisory
    sentence read as guidance, and that sentence exists to be obeyed, so weaving
    them in IS the injection surface. Truncation bounds a name's LENGTH not its
    CONTENT, so a 64-char cap still fits a shortened injection; relocation, not
    truncation, is what removes the surface. The requested ``select_field`` is
    echoed, sanitized and JSON-quoted so it too reads as data, only to make the
    message self-contained; it is also carried structurally as ``select_field_absent``."""
    return (
        f"select_field {json.dumps(absent.field, ensure_ascii=False)} is absent from every "
        "row, so this empty result means the field name matched no row rather than a value "
        "that matched nothing. The key names present in the data are listed in known_fields."
    )


def _row_spans(original_content: str) -> list[tuple[dict[str, Any], str]] | FilterError:
    """The extracted rows paired with each row's byte-exact source span (F10 raw).

    Validation and the user-facing messages are reused verbatim from
    :func:`_extract_rows` (so a raw select on a non-array original fails exactly
    as a plain select does); the spans then come from the pure
    :func:`~furl_ctx.ccr.retrieve_spans.array_element_spans` scanner over the SAME
    already-accepted bytes. The scanner and ``_extract_rows`` walk the same array
    in order, so ``spans[i]`` is ``rows[i]``'s source. A count mismatch or an
    unrecognized shape is unreachable for validated input; it maps to a loud
    ``FilterError`` rather than silently wrong bytes, keeping the step total.
    """
    rows = _extract_rows(original_content)
    if isinstance(rows, FilterError):
        return rows
    spans = array_element_spans(original_content)
    if spans is None or len(spans) != len(rows):
        return FilterError("raw slice could not map rows to their source spans; retry without raw")
    return [(row, span) for row, (_value, span) in zip(rows, spans)]


def _select_rows(original_content: str, filters: RetrieveFilters) -> FilteredContent | FilterError:
    """Keep the rows of a JSON array whose ``select_field`` matches. Total, pure.

    The array is either the top-level JSON array of objects OR the single
    dominant inner array of a JSON object (see :func:`_extract_rows`); an
    original with no usable array is a ``FilterError`` (never a silent empty
    result). A row is kept when its ``select_field`` is PRESENT and equals
    ``select_equals`` (bool-safe: ``True`` never matches the int ``1``; an ABSENT
    key matches nothing, not even ``select_equals=None`` — that requests rows whose
    field is present and null) or, in range mode, when the field is present,
    numeric, and within ``[select_min, select_max]`` (open bounds default to ±inf;
    a missing or non-numeric field is skipped, never an error). Kept rows are
    optionally projected to ``fields`` and capped at ``limit``; when more matched
    than the cap, only the first ``limit`` ship plus an explicit truncation marker.

    Two honesty signals ride the result. F7: because an absent key matches nothing,
    a ``select_field`` absent from EVERY row yields an EMPTY result, and only then
    does it carry an :class:`AbsentSelectField` + a ``note`` (a misnamed field, not
    a value that matched nothing). F10: with ``filters.raw`` each kept row is
    returned as its byte-exact source span instead of a re-serialization (raw
    forbids ``fields``, so a kept row is always a whole source row).
    """
    field = filters.select_field
    assert field is not None  # has_select guaranteed it upstream

    if filters.raw:
        paired = _row_spans(original_content)
        if isinstance(paired, FilterError):
            return paired
    else:
        rows = _extract_rows(original_content)
        if isinstance(rows, FilterError):
            return rows
        # Non-raw carries no span; the "" placeholder is never read off this path
        # (``_render_raw`` runs only when ``filters.raw``).
        paired = [(row, "") for row in rows]

    row_dicts = [row for row, _span in paired]
    equals_mode = filters.select_min is None and filters.select_max is None
    lo: float = filters.select_min if filters.select_min is not None else float("-inf")
    hi: float = filters.select_max if filters.select_max is not None else float("inf")

    matched: list[dict[str, Any]] = []
    matched_spans: list[str] = []
    for row, span in paired:
        # ``_MISSING`` for an absent key, kept distinct from a present ``None``:
        # an absent key must match neither ``select_equals`` (not even null) nor a
        # range, so a row lacking the field is never kept.
        value = row.get(field, _MISSING)
        if equals_mode:
            keep = _equals(value, filters.select_equals)
        else:
            # ``_is_number`` narrows ``value`` to a real int/float before the range
            # comparison, so ``lo <= value <= hi`` is total (never a None/str/
            # _MISSING operand); a missing or non-numeric field is skipped.
            keep = _is_number(value) and lo <= value <= hi
        if not keep:
            continue
        matched.append(_project_row(row, filters.fields) if filters.fields is not None else row)
        matched_spans.append(span)

    matched_count = len(matched)
    limit = filters.limit if filters.limit is not None else _DEFAULT_SELECT_LIMIT
    content = (
        _render_raw(matched_spans, limit, matched_count)
        if filters.raw
        else _render_rows(matched, limit, matched_count)
    )

    # F7 rides ONLY a genuinely empty result. With ``_MISSING`` an absent field
    # matches no row, so an absent-from-every-row select always lands here at
    # ``matched_count == 0``; the guard also makes the "empty result" wording in
    # the note true by construction, never attached to a non-empty one.
    absent = _absent_select_field(field, row_dicts) if matched_count == 0 else None
    return FilteredContent(
        content=content,
        kind="rows",
        matched_count=matched_count,
        total_count=len(row_dicts),
        note=_absent_field_note(absent) if absent is not None else None,
        select_field_absent=absent,
    )


def _extract_rows(original_content: str) -> list[dict[str, Any]] | FilterError:
    """The array of object-rows a row-select operates on, or a ``FilterError``.

    Accepts a top-level JSON array of objects, OR a JSON object with EXACTLY one
    dominant inner array — a non-empty list whose elements are all dicts (the
    Chrome-trace ``{"metadata":{}, "traceEvents":[…]}`` shape). This mirrors the
    router engine's ``_dominant_array`` "exactly one, else None" rule, kept as a
    small LOCAL reimplementation so this pure module never imports the engine.
    Anything else (invalid JSON, a scalar, an empty array, an object with zero or
    several dominant arrays) is a ``FilterError`` — a select needs an array, and
    an ambiguous/absent one is reported, never silently emptied.
    """
    try:
        parsed = json.loads(original_content)
    except json.JSONDecodeError:
        return FilterError(
            "select requires a JSON array of objects (or an object with one "
            "dominant array), but the stored content is not valid JSON"
        )

    if isinstance(parsed, list):
        if parsed and all(isinstance(item, dict) for item in parsed):
            return parsed
        return FilterError(
            "select requires a JSON array of OBJECTS, but the stored array is "
            "empty or contains non-object elements"
        )

    if isinstance(parsed, dict):
        matches = [
            k
            for k, v in parsed.items()
            if isinstance(v, list) and v and all(isinstance(item, dict) for item in v)
        ]
        if len(matches) == 1:
            inner: list[dict[str, Any]] = parsed[matches[0]]
            return inner
        return FilterError(
            "select requires a JSON object with EXACTLY one dominant array (a "
            f"non-empty list of objects); found {len(matches)} such keys"
        )

    return FilterError(
        "select requires a JSON array of objects (or an object with one dominant "
        f"array), but the stored content is a JSON {_json_kind(parsed)}"
    )


def _equals(cell: Any, target: Any) -> bool:
    """Bool-safe equality for a row-select, with an absent key distinguished from a
    present null value.

    An ABSENT key is passed as :data:`_MISSING` and equals NOTHING here, not even
    ``select_equals=None``: a caller matching null wants rows where the field is
    PRESENT and null, never rows that simply lack it. Then the bool guard — Python
    treats ``True == 1`` / ``False == 0`` as equal, which would silently conflate a
    boolean field with a 0/1 numeric, so when exactly one operand is a ``bool`` they
    are never equal. Otherwise plain ``==`` (total for any JSON scalar; never
    hashes, so an unhashable value is unreachable — containers were rejected at
    parse anyway)."""
    if cell is _MISSING:
        return False
    if isinstance(cell, bool) != isinstance(target, bool):
        return False
    return bool(cell == target)


def _is_number(value: Any) -> TypeGuard[float]:
    """True for a real numeric cell (int/float). ``bool`` is excluded — ``True``
    is not the number 1 in a range comparison, matching the engine's ``_type_name``
    (bool before int) and keeping booleans out of numeric ranges.

    A ``TypeGuard[float]`` so the caller's ``lo <= value <= hi`` narrows to a real
    number (``int`` widens to ``float`` for the comparison) — the range check is
    total, never a ``None``/``str`` operand."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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
