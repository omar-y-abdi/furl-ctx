"""Signal-aware CCR-offload preview: the two JSON-array-of-dicts offload paths
(top-level array and dominant-inner-array object) emit a compact ``_ccr_summary``
that answers AGGREGATE (per-field value histograms) and ANOMALY (one concrete
example row per notable value, with its fields) questions INLINE — without
retrieving the full data.

Contract under test (``_build_offload_preview`` / ``_summarize_rows``):
  * value_counts carries the per-field categorical histogram (the aggregate
    answer);
  * examples carries one concrete row per top value of the primary categorical
    ("type") field — so every notable value (incl. an infrequent one) is
    surfaced with its own fields (the anomaly answer);
  * the summary is bounded (a few KB) regardless of row count;
  * byte-exact recovery of the ORIGINAL is unchanged (compress -> retrieve);
  * the computation is fail-open: a pathological input still yields a preview.
"""

from __future__ import annotations

import json

import pytest

from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig
from furl_ctx.transforms.router_engine import _SUMMARY_TOP_VALUES
from furl_ctx.transforms.router_policy import CompressionStrategy


@pytest.fixture(autouse=True)
def _fresh_store():
    reset_compression_store()
    yield
    reset_compression_store()


def _offload_router() -> ContentRouter:
    """SmartCrusher disabled so the strategy chain deterministically declines
    and the offload fallback (the unit under test) fires."""
    return ContentRouter(ContentRouterConfig(enable_smart_crusher=False))


def _recover(compressed_marker_text: str) -> str | None:
    ccr_hash = compressed_marker_text.split("<<ccr:", 1)[1].split(">>", 1)[0]
    entry = get_compression_store().retrieve(ccr_hash)
    return entry.original_content if entry else None


def _summary_of(compressed: str) -> dict:
    """The ``_ccr_summary`` object is the FIRST preview row; the sentinel is
    the last. Structured (JSON array) offloads only."""
    rows = json.loads(compressed)
    assert isinstance(rows, list)
    return rows[0]["_ccr_summary"]


# ---------------------------------------------------------------------------
# Deterministic synthetic corpora
# ---------------------------------------------------------------------------


def _events_object(n_common: int = 900, n_rare: int = 7) -> tuple[str, list[int]]:
    """A ``{"metadata": {...}, "events": [...]}`` object (Chrome-trace shape)
    whose dominant inner array holds a COMMON ``name`` ("RunTask") and a RARE
    one ("DroppedFrame", well under the rare fraction). Returns the JSON string
    and the list of the rare rows' ``ts`` values (the anomaly ground truth)."""
    metadata = {"source": "synthetic", "version": 3}
    ts = 1000
    common_queue: list[dict] = []
    for _ in range(n_common):
        common_queue.append({"name": "RunTask", "cat": "timeline", "ph": "X", "ts": ts, "pid": 1})
        ts += 1
    rare_queue: list[dict] = []
    rare_ts: list[int] = []
    for _ in range(n_rare):
        rare_queue.append(
            {"name": "DroppedFrame", "cat": "timeline", "ph": "I", "ts": ts, "pid": 1}
        )
        rare_ts.append(ts)
        ts += 1
    # Spread the rare rows deterministically through the array so a head/tail
    # sample would miss them — only a full O(n) scan surfaces every one.
    ordered: list[dict] = []
    stride = max(1, len(common_queue) // (len(rare_queue) + 1))
    ci = 0
    for rare_event in rare_queue:
        ordered.extend(common_queue[ci : ci + stride])
        ci += stride
        ordered.append(rare_event)
    ordered.extend(common_queue[ci:])
    payload = {"metadata": metadata, "events": ordered}
    return json.dumps(payload, ensure_ascii=False), rare_ts


def _top_level_events(n_common: int = 900, n_rare: int = 6) -> tuple[str, list[int]]:
    """A TOP-LEVEL JSON array (not an object) of the same event rows."""
    rows: list[dict] = []
    ts = 5000
    for _ in range(n_common):
        rows.append({"name": "Paint", "phase": "X", "ts": ts})
        ts += 1
    rare_ts: list[int] = []
    for _ in range(n_rare):
        rows.append({"name": "LongTask", "phase": "I", "ts": ts})
        rare_ts.append(ts)
        ts += 1
    return json.dumps(rows, ensure_ascii=False), rare_ts


# ---------------------------------------------------------------------------
# (1) Dominant-array object: histogram + rare rows present, output bounded
# ---------------------------------------------------------------------------


def test_dominant_array_summary_answers_aggregate_and_anomaly():
    content, rare_ts = _events_object()
    result = _offload_router().compress(content)

    assert result.strategy_used == CompressionStrategy.CCR_OFFLOAD
    summary = _summary_of(result.compressed)

    # Names the dominant array + keeps the object's OTHER fields WITH values
    # (review F2), not just their names.
    assert summary["array"] == "events"
    assert summary["other_fields"] == {"metadata": {"source": "synthetic", "version": 3}}
    assert summary["count"] == 907

    # AGGREGATE: the per-name histogram is inline and directly answers the
    # "event distribution" question.
    name_counts = summary["value_counts"]["name"]
    assert name_counts["RunTask"] == 900
    assert name_counts["DroppedFrame"] == 7
    assert name_counts["_distinct"] == 2

    # ranges: numeric ts range present; bool/str fields excluded.
    assert "ts" in summary["ranges"]
    assert summary["ranges"]["ts"]["min"] < summary["ranges"]["ts"]["max"]
    assert "name" not in summary["ranges"]

    # ANOMALY: examples surface one concrete row per event name WITH its ts —
    # so the infrequent DroppedFrame is directly readable from the blob (not
    # crowded out, because examples are keyed by event type, not by rarity).
    assert summary["examples"]["field"] == "name"
    by_value = summary["examples"]["by_value"]
    assert "RunTask" in by_value and "DroppedFrame" in by_value
    dropped_example = by_value["DroppedFrame"]
    assert dropped_example["name"] == "DroppedFrame"
    assert dropped_example["ts"] in set(rare_ts)  # a real DroppedFrame timestamp

    # Bounded output regardless of row count.
    assert len(result.compressed) < 8192


def test_dominant_array_sibling_scalar_values_survive_review_f2():
    """Review F2: a crash-report-shaped object — one dominant array (the stack
    ``frames``) plus small scalar/nested sibling fields (``exception``,
    ``termination``, ``faultingThread``) — keeps the sibling VALUES in the
    preview, not just the key names. The exception type/signal is the single
    most important fact in a crash report; pre-fix it was reduced to a bare key
    name under ``other_keys`` and was invisible in the compressed view."""
    crash = {
        "exception": {
            "type": "EXC_BAD_ACCESS",
            "signal": "SIGSEGV",
            "codes": "KERN_INVALID_ADDRESS at 0x18",
        },
        "termination": {"namespace": "SIGNAL", "indicator": "Segmentation fault: 11"},
        "faultingThread": 9,
        "frames": [
            {"imageIndex": i % 5, "symbol": f"frame_{i}", "imageOffset": 4096 + i}
            for i in range(300)
        ],
    }
    result = _offload_router().compress(json.dumps(crash, ensure_ascii=False))

    assert result.strategy_used == CompressionStrategy.CCR_OFFLOAD
    summary = _summary_of(result.compressed)
    assert summary["array"] == "frames"

    other = summary["other_fields"]
    assert other["exception"] == {
        "type": "EXC_BAD_ACCESS",
        "signal": "SIGSEGV",
        "codes": "KERN_INVALID_ADDRESS at 0x18",
    }
    assert other["termination"]["indicator"] == "Segmentation fault: 11"
    assert other["faultingThread"] == 9
    # The crash reason is literally present in the compressed bytes now.
    assert "EXC_BAD_ACCESS" in result.compressed
    assert "SIGSEGV" in result.compressed


def test_large_sibling_array_is_elided_not_inlined_review_f2():
    """Review F2 bound: a LARGE sibling value (a 500-element array) is exactly
    the kind of structure that SHOULD stay offloaded — it is elided to a bounded
    ``[… in CCR]`` note while the tiny scalar sibling is kept, so the preview
    never re-inlines a big structure and stays a few KB."""
    payload = {
        "run": "run-42",  # scalar sibling: kept verbatim
        "big_side_array": list(range(500)),  # large sibling (non-dict list): elided
        "events": [{"kind": "tick", "n": i} for i in range(400)],  # dominant array
    }
    result = _offload_router().compress(json.dumps(payload, ensure_ascii=False))
    summary = _summary_of(result.compressed)

    assert summary["array"] == "events"
    other = summary["other_fields"]
    assert other["run"] == "run-42"
    assert isinstance(other["big_side_array"], str)
    assert "in CCR" in other["big_side_array"]
    assert len(result.compressed) < 8192


def test_sibling_fields_preview_bounded_by_construction_review_rf4():
    """Review RF4: the sibling preview is hard-bounded, not soft-checked. 200
    sizeable scalar siblings serialized to ~22 KB under the old scalar-only
    fallback (scalars passed through verbatim), and the top-level key count was
    never capped. Both are now bounded by construction — the serialized blob
    stays within _SIBLING_FIELDS_MAX_CHARS and the kept-key count within
    _SIBLING_MAX_KEYS — with an explicit _more_keys note for the elided rest."""
    from furl_ctx.transforms.router_engine import (
        _SIBLING_FIELDS_MAX_CHARS,
        _SIBLING_MAX_KEYS,
        ContentCompressionEngine,
    )

    # Sizeable scalars: the accumulated-length budget bounds the blob.
    big = {f"k{i}": ("x" * 100) for i in range(200)}
    out_big = ContentCompressionEngine._compact_sibling_fields(big)
    assert len(json.dumps(out_big, ensure_ascii=False, default=str)) <= _SIBLING_FIELDS_MAX_CHARS, (
        "sibling preview exceeded the char cap"
    )
    assert "_more_keys" in out_big

    # Many tiny scalars: the top-level key cap bounds the key count even when the
    # bytes alone would have fit.
    tiny = {f"k{i}": f"v{i}" for i in range(200)}
    out_tiny = ContentCompressionEngine._compact_sibling_fields(tiny)
    assert len([k for k in out_tiny if k != "_more_keys"]) <= _SIBLING_MAX_KEYS
    assert "_more_keys" in out_tiny


def test_examples_surface_notable_values_and_cap_at_top_values():
    """The improvement over rarest-first selection: an infrequent-but-notable
    value (a mid-frequency event type) is surfaced by name, NOT crowded out by a
    long tail of singleton noise on other/the-same field. Examples are keyed by
    the primary categorical field's TOP values (by count) and capped."""
    events: list[dict] = []
    ts = 0
    for _ in range(800):
        events.append({"name": "Common", "noise": "shared", "ts": ts})
        ts += 1
    for _ in range(20):
        events.append({"name": "Mid", "noise": "shared", "ts": ts})
        ts += 1
    # 40 singleton event types + a unique `noise` per row: a long rare tail that
    # a "rarest-first" selector would let crowd out the Mid rows entirely.
    for i in range(40):
        events.append({"name": f"OneOff{i}", "noise": f"uniq{i}", "ts": ts})
        ts += 1
    content = json.dumps({"events": events}, ensure_ascii=False)

    result = _offload_router().compress(content)
    summary = _summary_of(result.compressed)

    # `name` (42 distinct) is the primary field over `noise` (41 distinct).
    assert summary["examples"]["field"] == "name"
    by_value = summary["examples"]["by_value"]
    # Capped at the top-values budget.
    assert len(by_value) <= _SUMMARY_TOP_VALUES
    # The notable Mid type IS surfaced with a concrete row (the regression the
    # examples design fixes) — and so is Common.
    assert "Common" in by_value and "Mid" in by_value
    assert by_value["Mid"]["name"] == "Mid"
    assert isinstance(by_value["Mid"]["ts"], int)


# ---------------------------------------------------------------------------
# (2) Byte-exact recovery is UNCHANGED
# ---------------------------------------------------------------------------


def test_offload_summary_recovers_original_byte_exact():
    content, _ = _events_object()
    result = _offload_router().compress(content)

    rows = json.loads(result.compressed)
    assert set(rows[-1]) == {"_ccr_dropped"}
    assert "Retrieve more: hash=" in rows[-1]["_ccr_dropped"]
    # The stored original round-trips exactly — the summary is display-only.
    assert _recover(rows[-1]["_ccr_dropped"]) == content


# ---------------------------------------------------------------------------
# (3) Fail-open: pathological rows still yield a preview, never raise
# ---------------------------------------------------------------------------


def test_summary_fail_open_on_pathological_rows():
    # Heterogeneous rows: missing keys, unhashable field values (list/dict),
    # a field numeric in some rows + str in others, None/empty fields. The
    # summary computation must not raise; a preview is always returned.
    rows = []
    for i in range(300):
        rows.append(
            {
                "kind": ["A", "B"] if i % 2 else "A",  # unhashable in half the rows
                "code": i if i % 3 else str(i),  # int vs str
                "meta": {"nested": i} if i % 5 else None,  # dict vs None
                "flag": bool(i % 2),
            }
        )
    content = json.dumps(rows, ensure_ascii=False)

    result = _offload_router().compress(content)
    # A preview is emitted and stays byte-exact recoverable, whichever path
    # (summary or head/tail fallback) produced it.
    assert result.strategy_used == CompressionStrategy.CCR_OFFLOAD
    recovered = _recover(json.loads(result.compressed)[-1]["_ccr_dropped"])
    assert recovered == content


def test_summarize_rows_never_raises_on_direct_pathological_input():
    """``_summarize_rows`` is the fail-open core; call it directly with inputs
    designed to trip a naive implementation and assert it returns, never
    raises. (compress() wraps it in try/except; this pins the core itself.)"""
    engine = _offload_router()._engine
    # A categorical field ("kind") that is a common string in most rows, an
    # infrequent string in a few, and an UNHASHABLE list in others: the example
    # membership test (`value in wanted`) must not choke on the list value.
    mixed_hashability = (
        [{"kind": "RARE", "ts": i} for i in range(5)]
        + [{"kind": ["x", "y"], "ts": 100 + i} for i in range(10)]
        + [{"kind": "COMMON", "ts": 200 + i} for i in range(285)]
    )
    pathological: list[list[dict]] = [
        [],  # empty (guarded by caller, but must not crash)
        [{"a": {"deep": [1, 2]}}, {"a": object()}],  # unhashable values
        [{"x": 1}, {"y": 2}, {"z": 3}],  # fully disjoint keys, all-unique
        [{"n": float("nan")}, {"n": float("inf")}],  # non-finite numerics
        mixed_hashability,  # scalar values + unhashable value in one field
    ]
    for rows in pathological:
        out, n = engine._summarize_rows(rows, key=None, other_fields={})
        assert isinstance(out, list) and len(out) == 1
        assert "_ccr_summary" in out[0]
        assert n == len(rows)

    # For the mixed-hashability case, the scalar values are surfaced as examples
    # and no unhashable-valued row leaks in.
    summary = engine._summarize_rows(mixed_hashability, key=None, other_fields={})[0][0][
        "_ccr_summary"
    ]
    example_kinds = [r.get("kind") for r in summary["examples"]["by_value"].values()]
    assert "RARE" in example_kinds and "COMMON" in example_kinds
    assert all(isinstance(k, str) for k in example_kinds)


# ---------------------------------------------------------------------------
# (4) Top-level array (not a dominant-object) is also summarized
# ---------------------------------------------------------------------------


def test_top_level_array_is_summarized():
    content, rare_ts = _top_level_events()
    result = _offload_router().compress(content)

    assert result.strategy_used == CompressionStrategy.CCR_OFFLOAD
    summary = _summary_of(result.compressed)

    # Top-level array => no wrapping key, no sibling fields.
    assert summary["array"] is None
    assert summary["other_fields"] == {}
    assert summary["count"] == 906

    # Aggregate + anomaly answerable inline.
    assert summary["value_counts"]["name"]["Paint"] == 900
    assert summary["value_counts"]["name"]["LongTask"] == 6
    assert summary["examples"]["field"] == "name"
    assert summary["examples"]["by_value"]["LongTask"]["ts"] in set(rare_ts)

    # Recovery unchanged.
    assert _recover(json.loads(result.compressed)[-1]["_ccr_dropped"]) == content
    assert len(result.compressed) < 8192


def test_all_unique_field_is_not_treated_as_categorical():
    """A field whose values are all distinct (like a monotonic ts or a uid) has
    too high a cardinality to be a categorical histogram — it must NOT produce a
    value_counts entry (which would be n distinct pairs), and must NOT be chosen
    as the example ("type") field (every row would be its own example)."""
    rows = [{"name": "Same", "uid": f"u{i}", "ts": i} for i in range(500)]
    content = json.dumps(rows, ensure_ascii=False)

    result = _offload_router().compress(content)
    summary = _summary_of(result.compressed)

    # name is categorical (1 distinct); uid is all-unique => not categorical.
    assert "name" in summary["value_counts"]
    assert "uid" not in summary["value_counts"]
    # The only categorical field ("name", 1 value) is the example field; the
    # all-unique `uid` is NOT chosen (which would emit 500 example rows).
    assert summary["examples"]["field"] == "name"
    assert list(summary["examples"]["by_value"]) == ["Same"]
    # ts is numeric => range, not a histogram.
    assert "ts" in summary["ranges"]
    assert "ts" not in summary["value_counts"]


# ── Bug-4 / Med-10: error-line preservation in the plain-text head/tail preview ──


def _plain_text_log_with_buried_errors() -> str:
    """A high-entropy plain-text log (not a JSON array) long enough to offload,
    with an ERROR and a Traceback buried in the omitted MIDDLE (not head/tail)."""
    lines = [f"paragraph {i}: unique routine narrative content token-{i} tail" for i in range(120)]
    lines[55] = "paragraph 55: ERROR upstream returned 503 after 3 retries id=req-55"
    lines[70] = "paragraph 70: Traceback (most recent call last): boom in handler"
    return "\n".join(lines)


def test_plain_text_offload_surfaces_buried_error_lines_bug4():
    """A buried ERROR/Traceback is visible in the compressed view, not just in CCR."""
    content = _plain_text_log_with_buried_errors()
    result = _offload_router().compress(content)
    view = result.compressed
    assert "lines omitted, in CCR" in view, "precondition: took the head/tail offload path"
    assert "error/severity line(s) surfaced" in view
    assert "ERROR upstream returned 503" in view, "the buried ERROR must be visible inline"
    assert "Traceback (most recent call last)" in view, "the buried Traceback must be visible"


def test_plain_text_offload_still_recovers_byte_exact_bug4():
    """Surfacing error lines does not change byte-exact recovery of the original."""
    content = _plain_text_log_with_buried_errors()
    result = _offload_router().compress(content)
    assert _recover(result.compressed) == content


def test_plain_text_offload_no_false_positive_when_benign():
    """An all-benign long text takes the plain head/tail path with NO surfaced block."""
    content = "\n".join(f"paragraph {i}: benign unique content token-{i}" for i in range(120))
    view = _offload_router().compress(content).compressed
    assert "lines omitted, in CCR" in view
    assert "surfaced" not in view


def test_error_line_surfacing_is_bounded():
    """An error-dense middle surfaces at most _OFFLOAD_ERROR_LINES_MAX lines."""
    from furl_ctx.transforms.router_engine import (
        _OFFLOAD_ERROR_LINES_MAX,
        ContentCompressionEngine,
    )

    eng = ContentCompressionEngine.__new__(ContentCompressionEngine)
    omitted = [f"ERROR failure number {i}" for i in range(500)]
    surfaced = eng._extract_error_lines(omitted)
    assert len(surfaced) == _OFFLOAD_ERROR_LINES_MAX
