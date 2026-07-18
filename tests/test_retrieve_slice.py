"""Sliceable CCR retrieve (c7/ccr-slice-retrieve).

The row-select filter family turns an all-or-nothing ``retrieve(hash)`` into a
cheap drill-in: keep the ROWS of a JSON array of objects — or of a JSON object
with one dominant inner array (the Chrome-trace shape) — whose ``select_field``
equals a value or falls in a numeric range, optionally projected to ``fields``
and always bounded by ``limit``.

Two layers are exercised:

* the PURE domain (``RetrieveFilters.parse`` fail-closed + the total
  ``apply_filters``/``_select_rows``), directly, no store — matching the repo's
  "domain logic testable without mocks" rule; and
* the library ``retrieve()`` end-to-end: a no-filter retrieve is byte-identical
  to the stored original, a slice returns just the matching rows, and a bad
  filter combination raises ``ValueError`` (a caller bug, surfaced loudly).
"""

from __future__ import annotations

import json

import pytest

from furl_ctx import compress, retrieve
from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store
from furl_ctx.ccr.retrieve_filters import (
    _DEFAULT_SELECT_LIMIT,
    FilteredContent,
    FilterError,
    RetrieveFilters,
    apply_filters,
)


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("FURL_CCR_BACKEND", "memory")
    reset_compression_store()
    yield
    reset_compression_store()


def _spec(**kwargs) -> RetrieveFilters:
    spec = RetrieveFilters.parse(kwargs)
    assert isinstance(spec, RetrieveFilters), spec
    return spec


# A top-level array of object-rows and the dominant-inner-array object form of
# the SAME rows — both must slice identically.
_ROWS = [
    {"name": "Paint", "ts": 10, "dur": 3},
    {"name": "DroppedFrame", "ts": 20, "dur": 1},
    {"name": "Paint", "ts": 30, "dur": 4},
    {"name": "DroppedFrame", "ts": 40, "dur": 2},
    {"name": "Layout", "ts": 50, "dur": 9},
]
_TOP_LEVEL_ARRAY = json.dumps(_ROWS)
_DOMINANT_OBJECT = json.dumps({"metadata": {"src": "devtools"}, "traceEvents": _ROWS})


# ─── SELECT equals: top-level array AND dominant-array object ────────────────


def test_select_equals_on_top_level_array() -> None:
    out = apply_filters(_TOP_LEVEL_ARRAY, _spec(select_field="name", select_equals="DroppedFrame"))
    assert isinstance(out, FilteredContent)
    assert out.kind == "rows"
    assert json.loads(out.content) == [
        {"name": "DroppedFrame", "ts": 20, "dur": 1},
        {"name": "DroppedFrame", "ts": 40, "dur": 2},
    ]
    assert out.matched_count == 2
    assert out.total_count == 5


def test_select_equals_on_dominant_array_object() -> None:
    # The object is NOT a top-level array; the inner ``traceEvents`` is the one
    # dominant list-of-dicts, so select reaches into it — no second round-trip.
    out = apply_filters(_DOMINANT_OBJECT, _spec(select_field="name", select_equals="DroppedFrame"))
    assert isinstance(out, FilteredContent)
    assert [r["ts"] for r in json.loads(out.content)] == [20, 40]
    assert out.matched_count == 2
    assert out.total_count == 5  # rows of the dominant array, not the object's keys


def test_select_equals_no_match_is_empty_not_error() -> None:
    # A valid array with zero matches is an empty result (count 0), never a
    # FilterError — only a missing/ambiguous ARRAY is an error.
    out = apply_filters(_TOP_LEVEL_ARRAY, _spec(select_field="name", select_equals="Nonexistent"))
    assert isinstance(out, FilteredContent)
    assert json.loads(out.content) == []
    assert out.matched_count == 0
    assert out.total_count == 5


# ─── SELECT numeric range ───────────────────────────────────────────────────


def test_select_numeric_range_window() -> None:
    out = apply_filters(_DOMINANT_OBJECT, _spec(select_field="ts", select_min=20, select_max=40))
    assert isinstance(out, FilteredContent)
    assert [r["ts"] for r in json.loads(out.content)] == [20, 30, 40]  # inclusive bounds
    assert out.matched_count == 3


def test_select_open_ended_range() -> None:
    # Only select_min → an open upper bound (>= 40).
    out = apply_filters(_TOP_LEVEL_ARRAY, _spec(select_field="ts", select_min=40))
    assert isinstance(out, FilteredContent)
    assert [r["ts"] for r in json.loads(out.content)] == [40, 50]


def test_select_range_skips_missing_and_non_numeric_and_bool() -> None:
    rows = json.dumps(
        [
            {"ts": 5},
            {"ts": "not-a-number"},  # non-numeric → skipped
            {"other": 1},  # field missing → skipped
            {"ts": True},  # bool is not a number → skipped
            {"ts": 7},
        ]
    )
    out = apply_filters(rows, _spec(select_field="ts", select_min=0, select_max=100))
    assert isinstance(out, FilteredContent)
    assert json.loads(out.content) == [{"ts": 5}, {"ts": 7}]
    assert out.total_count == 5  # every array element counted in the total


# ─── SELECT composes with fields ────────────────────────────────────────────


def test_select_projects_fields_over_selected_rows() -> None:
    out = apply_filters(
        _TOP_LEVEL_ARRAY,
        _spec(select_field="name", select_equals="Paint", fields=["name", "dur"]),
    )
    assert isinstance(out, FilteredContent)
    assert json.loads(out.content) == [
        {"name": "Paint", "dur": 3},
        {"name": "Paint", "dur": 4},
    ]  # ts dropped by the projection; only selected rows present


def test_select_fields_projection_omits_absent_keys() -> None:
    # Projection, not a lookup that must hit: an absent requested key is simply
    # omitted from that row's copy (never a KeyError, never a null fill).
    out = apply_filters(
        _TOP_LEVEL_ARRAY,
        _spec(select_field="name", select_equals="Layout", fields=["name", "gone"]),
    )
    assert isinstance(out, FilteredContent)
    assert json.loads(out.content) == [{"name": "Layout"}]


# ─── SELECT limit truncation (bounded output) ───────────────────────────────


def test_select_limit_truncates_with_marker_row() -> None:
    rows = json.dumps([{"k": 1, "i": i} for i in range(10)])
    out = apply_filters(rows, _spec(select_field="k", select_equals=1, limit=3))
    assert isinstance(out, FilteredContent)
    data = json.loads(out.content)
    assert len(data) == 4  # 3 rows + 1 explicit truncation marker
    assert data[:3] == [{"k": 1, "i": 0}, {"k": 1, "i": 1}, {"k": 1, "i": 2}]
    assert "_truncated" in data[-1]
    assert "10" in data[-1]["_truncated"]  # reports the true match count
    assert out.matched_count == 10  # matched_count is the full total, not the cap


def test_select_default_limit_is_bounded() -> None:
    spec = RetrieveFilters.parse({"select_field": "k", "select_equals": 1})
    assert isinstance(spec, RetrieveFilters)
    assert spec.limit == _DEFAULT_SELECT_LIMIT  # a select is always bounded


def test_select_exactly_at_limit_has_no_marker() -> None:
    rows = json.dumps([{"k": 1} for _ in range(3)])
    out = apply_filters(rows, _spec(select_field="k", select_equals=1, limit=3))
    assert isinstance(out, FilteredContent)
    data = json.loads(out.content)
    assert len(data) == 3  # equal-to-limit is not truncated
    assert all("_truncated" not in r for r in data)


# ─── bool-vs-int equality (no silent conflation) ────────────────────────────


def test_select_equals_bool_does_not_match_int() -> None:
    rows = json.dumps([{"v": True}, {"v": 1}, {"v": False}, {"v": 0}])
    out_true = apply_filters(rows, _spec(select_field="v", select_equals=True))
    assert isinstance(out_true, FilteredContent)
    assert json.loads(out_true.content) == [{"v": True}]  # not the int 1
    out_one = apply_filters(rows, _spec(select_field="v", select_equals=1))
    assert isinstance(out_one, FilteredContent)
    assert json.loads(out_one.content) == [{"v": 1}]  # not the bool True


# ─── SELECT on a non-array original → FilterError (never a silent empty) ─────


@pytest.mark.parametrize(
    "content",
    [
        "just some text, not JSON",
        json.dumps({"a": 1, "b": 2}),  # object, no dominant array
        json.dumps([1, 2, 3]),  # array of scalars, not objects
        json.dumps([]),  # empty array
        json.dumps({"x": [{"a": 1}], "y": [{"b": 2}]}),  # two dominant arrays → ambiguous
        json.dumps(42),  # scalar
    ],
)
def test_select_on_non_array_is_error_not_silent_empty(content: str) -> None:
    out = apply_filters(content, _spec(select_field="name", select_equals="x"))
    assert isinstance(out, FilterError)
    assert "select requires" in out.reason


# ─── parse fail-closed: SELECT validation ───────────────────────────────────


@pytest.mark.parametrize(
    "arguments",
    [
        {"select_equals": "x"},  # no select_field anchor
        {"select_min": 1},  # range without a field
        {"limit": 5},  # limit without a field
        {"select_field": 7, "select_equals": "x"},  # non-string field
        {"select_field": "k", "select_equals": 1, "select_min": 0},  # equals + range
        {"select_field": "k", "select_equals": [1, 2]},  # container equals
        {"select_field": "k", "select_equals": {"a": 1}},  # container equals (dict)
        {"select_field": "k", "select_min": 9, "select_max": 2},  # inverted range
        {"select_field": "k", "select_min": True},  # bool bound
        {"select_field": "k", "select_min": "5"},  # non-numeric bound
        {"select_field": "k", "select_equals": 1, "limit": 0},  # non-positive limit
        {"select_field": "k", "select_equals": 1, "limit": True},  # bool limit
        {"select_field": "k", "select_equals": 1, "pattern": "x"},  # select + line filter
        {"select_field": "k", "select_equals": 1, "line_range": [1, 2]},  # select + line filter
    ],
)
def test_parse_rejects_bad_select(arguments: dict) -> None:
    out = RetrieveFilters.parse(arguments)
    assert isinstance(out, FilterError), (arguments, out)


def test_select_equals_null_is_a_valid_request() -> None:
    # select_equals=null is a real "field is null" match, distinct from "no
    # select_equals given" (which, with a field, is also a null match).
    spec = RetrieveFilters.parse({"select_field": "k", "select_equals": None})
    assert isinstance(spec, RetrieveFilters)
    assert spec.has_select
    rows = json.dumps([{"k": None}, {"k": 1}, {"other": 2}])
    out = apply_filters(rows, spec)
    assert isinstance(out, FilteredContent)
    # Both the explicit null and the absent field read as None on .get.
    assert json.loads(out.content) == [{"k": None}, {"other": 2}]


def test_parse_empty_is_still_empty_spec() -> None:
    spec = RetrieveFilters.parse({})
    assert isinstance(spec, RetrieveFilters)
    assert spec.is_empty
    assert not spec.has_select


# ─── purity / immutability of _select_rows ──────────────────────────────────


def test_select_does_not_mutate_input_string_semantics() -> None:
    # apply_filters takes a str, so "mutation" would mean a differing re-parse.
    # Run the same select twice and confirm identical output (pure/idempotent).
    a = apply_filters(_DOMINANT_OBJECT, _spec(select_field="name", select_equals="Paint"))
    b = apply_filters(_DOMINANT_OBJECT, _spec(select_field="name", select_equals="Paint"))
    assert isinstance(a, FilteredContent) and isinstance(b, FilteredContent)
    assert a.content == b.content
    # And the original constant is unchanged (still parses to the same rows).
    assert json.loads(_DOMINANT_OBJECT)["traceEvents"] == _ROWS


# ─── library retrieve(): no-filter byte-identity + slice + ValueError ────────


def _store_original(content: str) -> str:
    """Store *content* in the CCR store and return its hash (no compression
    heuristics in the loop — a direct, byte-exact store round-trip)."""
    store = get_compression_store()
    return store.store(
        original=content,
        compressed=content,
        original_tokens=len(content.split()),
        compressed_tokens=len(content.split()),
    )


def test_retrieve_no_filter_is_byte_identical_to_stored_original() -> None:
    h = _store_original(_DOMINANT_OBJECT)
    assert retrieve(h) == _DOMINANT_OBJECT  # full path untouched, byte-exact


def test_retrieve_slice_returns_only_matching_rows() -> None:
    h = _store_original(_DOMINANT_OBJECT)
    sliced = retrieve(h, select_field="name", select_equals="DroppedFrame")
    assert sliced is not None
    assert [r["ts"] for r in json.loads(sliced)] == [20, 40]
    # The slice is far smaller than the full original.
    assert len(sliced) < len(_DOMINANT_OBJECT)


def test_retrieve_slice_range_over_library() -> None:
    h = _store_original(_TOP_LEVEL_ARRAY)
    sliced = retrieve(h, select_field="ts", select_min=20, select_max=40)
    assert sliced is not None
    assert [r["ts"] for r in json.loads(sliced)] == [20, 30, 40]


def test_retrieve_missing_hash_is_none_even_with_filter() -> None:
    # A store miss is None on both the full and the sliced path — a loud,
    # explicit miss, never a silent empty slice.
    assert retrieve("0" * 24, select_field="name", select_equals="x") is None


def test_retrieve_bad_filter_raises_valueerror() -> None:
    h = _store_original(_TOP_LEVEL_ARRAY)
    with pytest.raises(ValueError, match="mutually exclusive"):
        retrieve(h, select_field="name", select_equals="x", select_min=1)


def test_retrieve_select_on_non_array_raises_valueerror() -> None:
    h = _store_original("plain text, not a JSON array")
    with pytest.raises(ValueError, match="select requires"):
        retrieve(h, select_field="name", select_equals="x")


def test_retrieve_query_plus_filter_raises_valueerror() -> None:
    h = _store_original(_TOP_LEVEL_ARRAY)
    with pytest.raises(ValueError, match="query cannot be combined"):
        retrieve(h, query="paint", select_field="name", select_equals="Paint")


def test_retrieve_through_real_compress_offload_slices() -> None:
    # End-to-end through the real offload: compress a dominant-array object big
    # enough to offload, grab the shipped hash, and slice it back.
    rows = [{"name": "DroppedFrame" if i % 3 == 0 else "Paint", "ts": i} for i in range(500)]
    env = json.dumps({"metadata": {"src": "x"}, "traceEvents": rows})
    result = compress([{"role": "tool", "content": env}], model="gpt-4o")
    assert result.ccr_hashes, "expected an offload hash"
    h = result.ccr_hashes[0]
    assert retrieve(h) == env  # full recovery byte-exact
    sliced = retrieve(h, select_field="name", select_equals="DroppedFrame")
    assert sliced is not None
    dropped = json.loads(sliced)
    assert all(r["name"] == "DroppedFrame" for r in dropped)
    assert len(dropped) == len([r for r in rows if r["name"] == "DroppedFrame"])
