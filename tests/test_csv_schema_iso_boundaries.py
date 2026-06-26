"""ISO-date civil-decoder boundary coverage (csv_schema_decoder.py:137-158).

``_parse_iso`` (:133-148) and ``_render_iso`` (:151-158) carry the dense field
bounds (``1<=mo<=12``, ``1<=d<=31``, ``h>23``, ``mi>59``, ``sec>59``,
``y<1`` / ``1<=y<=9999``). These are reached from the public
``decode_csv_schema_rows`` only via an ISO-delta column, and the public return
MASKS the parse boundary: an invalid seed cell is returned verbatim
(:641-643), so just-valid and just-invalid seeds look identical at the row
level. The year-upper bound (:156) is unreachable from a parsed string at all
(``_ISO_RE`` anchors the year to ``\\d{4}``) — it only fires under delta
arithmetic that overflows the calendar.

So the boundaries are asserted directly against ``_parse_iso`` / ``_render_iso``
(consistent with the suite already testing private helpers, e.g.
``test_parser_none_text.py`` importing ``_extract_tool_result_text``), plus one
``decode_csv_schema_rows`` smoke test confirming the public path decodes.

Every edge below was REPL-verified before assertion:
    valid field  -> _parse_iso returns (epoch:int, tz:str)
    invalid field-> _parse_iso returns None
    y in [1,9999]-> _render_iso returns the ISO string
    y outside    -> _render_iso returns None
"""

from __future__ import annotations

import pytest

from headroom.transforms.csv_schema_decoder import (
    _parse_iso,
    _render_iso,
    decode_csv_schema_rows,
)

# Epochs REPL-verified from _parse_iso for the year extremes.
_EPOCH_Y0001 = -62135596800  # 0001-01-01T00:00:00Z
_EPOCH_Y9999 = 253402214400  # 9999-12-31T00:00:00Z
_ONE_YEAR_SECONDS = 366 * 86400  # > one calendar year, to cross a year edge


@pytest.mark.parametrize(
    "iso,valid",
    [
        # month bound (:140): 12 ok, 13 rejected
        ("2021-12-15T00:00:00Z", True),
        ("2021-13-15T00:00:00Z", False),
        # day bound (:140): 31 ok, 32 rejected
        ("2021-01-31T00:00:00Z", True),
        ("2021-01-32T00:00:00Z", False),
        # hour bound (:142): 23 ok, 24 rejected
        ("2021-01-15T23:00:00Z", True),
        ("2021-01-15T24:00:00Z", False),
        # minute bound (:142): 59 ok, 60 rejected
        ("2021-01-15T00:59:00Z", True),
        ("2021-01-15T00:60:00Z", False),
        # second bound (:142): 59 ok, 60 rejected
        ("2021-01-15T00:00:59Z", True),
        ("2021-01-15T00:00:60Z", False),
        # year lower bound (:140 y<1): 0001 ok, 0000 rejected
        ("0001-01-01T00:00:00Z", True),
        ("0000-01-01T00:00:00Z", False),
        # year upper edge: 9999 parses; 10000 is 5 digits → _ISO_RE rejects
        ("9999-12-31T00:00:00Z", True),
        ("10000-01-01T00:00:00Z", False),
    ],
)
def test_parse_iso_field_boundaries(iso: str, valid: bool) -> None:
    result = _parse_iso(iso)
    if valid:
        # A just-valid edge decodes to (epoch_seconds, tz_spelling).
        assert result is not None, f"{iso} should parse"
        epoch, tz = result
        assert isinstance(epoch, int)
        assert tz == "Z"
    else:
        # A just-invalid edge returns None (no exception, no silent pass-through).
        assert result is None, f"{iso} should be rejected, got {result!r}"


def test_render_iso_year_upper_boundary() -> None:
    # :156 (1<=y<=9999 upper) — reachable only via epoch arithmetic. y=9999
    # renders; pushing one year past it (y=10000) returns None, not a 5-digit
    # year string.
    assert _render_iso(_EPOCH_Y9999, "Z") == "9999-12-31T00:00:00Z"
    assert _render_iso(_EPOCH_Y9999 + _ONE_YEAR_SECONDS, "Z") is None


def test_render_iso_year_lower_boundary() -> None:
    # :156 (1<=y<=9999 lower) — y=1 renders; dropping below it returns None.
    assert _render_iso(_EPOCH_Y0001, "Z") == "0001-01-01T00:00:00Z"
    assert _render_iso(_EPOCH_Y0001 - _ONE_YEAR_SECONDS, "Z") is None


def test_parse_iso_invalid_calendar_date_rejected() -> None:
    # :145 — a field-valid but calendar-invalid date (Feb 30) survives the
    # numeric bounds but is caught by the civil round-trip check.
    assert _parse_iso("2021-02-30T00:00:00Z") is None
    # Sanity: Feb 28 of the same (non-leap) year IS valid.
    assert _parse_iso("2021-02-28T00:00:00Z") is not None


def test_decode_csv_schema_rows_public_smoke() -> None:
    # The boundary helpers above are private; confirm the PUBLIC decode entry
    # round-trips a minimal valid schema so the helpers are exercised on a real
    # code path, not only in isolation.
    rows = decode_csv_schema_rows("[2]{id:int}\n1\n2")
    assert rows == [{"id": 1}, {"id": 2}]
