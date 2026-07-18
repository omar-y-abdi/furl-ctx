"""MATRIX · retrieve() slice filters over freshly-offloaded content.

``test_retrieve_slice.py`` / ``test_ccr_filter_units.py`` already cover the pure
filter domain and a select_equals end-to-end offload. This register extends past
that in two non-duplicate directions:

* the TEXT line filters (``pattern`` / ``line_range`` / ``context_lines``) applied
  to a whole-offloaded LOG — the drill-in path for the log/code families the rest
  of the matrix offloads, which the existing end-to-end coverage does not touch;
* the numeric-range and projection filters (``select_min`` / ``select_max`` /
  ``limit`` / ``fields``) end-to-end through a real array offload — the existing
  end-to-end test exercises only ``select_equals``.

Each filtered retrieve returns EXACTLY the correct subset — no over/under-selection.
"""

from __future__ import annotations

import json

from tests.matrix import _matrix as m


def _error_info_log() -> tuple[str, int]:
    """400 lines; every 25th is an ERROR (16 total). Returns (text, n_errors)."""
    lines = [
        (f"ERROR svc-{i} failed batch {i}" if i % 25 == 0 else f"INFO svc-{i} ok batch {i}")
        for i in range(400)
    ]
    return "\n".join(lines) + "\n", 16


# ─── TEXT line filters over a whole-offloaded log ────────────────────────────


def test_pattern_filter_returns_exactly_the_matching_lines(salt) -> None:
    log, n_err = _error_info_log()
    doc = m.salted(log, salt)
    result = m.run(doc)
    assert result.ccr_hashes, "log is expected to offload"
    h = result.ccr_hashes[0]

    matched = m.retrieve(h, pattern="ERROR")
    assert matched is not None
    # exactly the 16 ERROR lines, 1-based line-numbered, nothing else.
    assert matched.count("failed batch") == n_err
    assert "INFO" not in matched
    assert "1:ERROR svc-0 failed batch 0" in matched  # first ERROR at line 1, numbered


def test_line_range_filter_returns_exactly_that_window(salt) -> None:
    log, _ = _error_info_log()
    doc = m.salted(log, salt)
    result = m.run(doc)
    assert result.ccr_hashes
    h = result.ccr_hashes[0]

    window = m.retrieve(h, line_range=[1, 3])
    assert window is not None
    assert window.count("\n") == 2 and not window.endswith("\n")  # exactly 3 numbered lines
    assert "1:ERROR svc-0 failed batch 0" in window
    assert "2:INFO svc-1 ok batch 1" in window
    assert "3:INFO svc-2 ok batch 2" in window
    assert "svc-3" not in window  # window is inclusive [1,3] only


def test_context_lines_includes_neighbours_of_the_match(salt) -> None:
    log, _ = _error_info_log()
    doc = m.salted(log, salt)
    result = m.run(doc)
    assert result.ccr_hashes
    h = result.ccr_hashes[0]

    around = m.retrieve(h, pattern="ERROR svc-25 ", context_lines=1)
    assert around is not None
    assert "ERROR svc-25 failed batch 25" in around  # the match
    assert "INFO svc-24 ok batch 24" in around  # one line of leading context
    assert "INFO svc-26 ok batch 26" in around  # one line of trailing context
    assert "svc-0 " not in around  # unrelated matches are NOT pulled in


# ─── numeric-range / projection / limit filters over an array offload ────────


def _offload_rows() -> str:
    rows = [{"name": "Paint" if i % 2 else "Layout", "ts": i, "dur": i % 7} for i in range(400)]
    result = m.run(json.dumps(rows))
    assert result.ccr_hashes, "row array is expected to offload"
    return result.ccr_hashes[0]


def test_select_min_returns_exactly_the_upper_window() -> None:
    rows = json.loads(m.retrieve(_offload_rows(), select_field="ts", select_min=395))
    assert [r["ts"] for r in rows] == [395, 396, 397, 398, 399]


def test_select_max_returns_exactly_the_lower_window() -> None:
    rows = json.loads(m.retrieve(_offload_rows(), select_field="ts", select_max=3))
    assert [r["ts"] for r in rows] == [0, 1, 2, 3]


def test_fields_projection_returns_all_rows_with_only_requested_keys() -> None:
    rows = json.loads(m.retrieve(_offload_rows(), fields=["name", "ts"]))
    assert len(rows) == 400
    assert all(set(r.keys()) == {"name", "ts"} for r in rows)


def test_limit_truncates_and_reports_true_match_count() -> None:
    rows = json.loads(
        m.retrieve(_offload_rows(), select_field="name", select_equals="Layout", limit=2)
    )
    kept = [r for r in rows if "_truncated" not in r]
    marker = [r for r in rows if "_truncated" in r]
    assert len(kept) == 2  # bounded output
    assert marker and "200" in marker[0]["_truncated"]  # true total (200 Layout rows) reported
    assert all(r["name"] == "Layout" for r in kept)
