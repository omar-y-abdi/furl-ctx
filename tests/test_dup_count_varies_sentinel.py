"""End-to-end pin for T5 (furl-ctx pre-mortem audit).

The kept REPRESENTATIVE of a collapsed VaryingIdentity family must render its
varying identity columns as the ``<varies>`` sentinel, never row-0's concrete
value beside ``_dup_count:N``.

Rows that differ ONLY in identity columns -- a hex id, an ISO timestamp, a
monotone counter -- while their content is constant collapse under the
stable-projection hash. Before the fix the representative kept row-0's concrete
id, so an agent reading the compressed view -- which the skill tells it to
trust -- read one id as recurring ``N`` times when ``N`` DISTINCT ids each
occurred once. The fix blanks those columns to ``<varies>`` while keeping
``_dup_count`` and leaving every dropped row CCR-recoverable: a display change,
not a data change.
"""

from __future__ import annotations

import json
from typing import Any

import furl_ctx._core as core

_VARIES = "<varies>"
_IDENTITY_COLS = ("req_id", "ts", "seq")


def _crush_kept_rows(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Run the engine's lossy dedup path and return (kept row dicts, strategy).

    ``without_compaction`` bypasses the lossless-table short-circuit so the
    dedup path -- where the ``_dup_count`` stamping lives -- actually runs. The
    trailing ``{"_ccr_dropped": ...}`` sentinel row is not a kept row.
    """
    sc = core.SmartCrusher.without_compaction(core.SmartCrusherConfig())
    res = sc.crush_array_json(json.dumps(items, ensure_ascii=False), "audit trail")
    payload = res["items"]
    rendered = payload if isinstance(payload, str) else json.dumps(payload)
    rows = json.loads(rendered)
    assert isinstance(rows, list), f"kept-rows payload is not an array: {type(rows)}"
    kept = [r for r in rows if isinstance(r, dict) and "_ccr_dropped" not in r]
    return kept, str(res["strategy_info"])


def _heartbeat_audit_trail() -> list[dict[str, Any]]:
    """A realistic audit trail: constant content per host, three distinct
    identity shapes per row. The first 60 rows repeat one heartbeat (three
    hosts -> three families of 20); the last 60 vary so the array crushes.
    """
    items: list[dict[str, Any]] = []
    for i in range(120):
        if i < 60:
            event, detail = "heartbeat", "ok"
        else:
            event, detail = "request", f"served shard {i % 7}"
        items.append(
            {
                "req_id": f"{i:040x}",
                "ts": f"2026-06-12T10:{i // 60:02d}:{i % 60:02d}Z",
                "seq": i,
                "host": f"web-{i % 3}",
                "event": event,
                "detail": detail,
                "status": "ok",
            }
        )
    return items


def test_representative_blanks_varying_identity_columns_end_to_end() -> None:
    kept, strategy = _crush_kept_rows(_heartbeat_audit_trail())

    reps = [r for r in kept if "_dup_count" in r]
    assert reps, (
        f"T5 precondition: the collapse-and-stamp path must fire; strategy={strategy}, kept={kept}"
    )

    for rep in reps:
        assert int(rep["_dup_count"]) > 1, f"a stamped representative records N>1: {rep}"
        for col in _IDENTITY_COLS:
            assert rep.get(col) == _VARIES, (
                f"identity column {col!r} on a stamped representative must be "
                f"{_VARIES!r}, not a concrete value implying it recurred "
                f"{rep['_dup_count']} times: {rep}"
            )
        # Constant content is untouched -- only identity display changes.
        assert rep.get("status") == "ok", f"constant content stays verbatim: {rep}"
