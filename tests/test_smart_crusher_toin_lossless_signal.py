"""Determination test for #8: TOIN learning signal on the lossless path.

Concern (#8): on a lossless csv-schema / compact-JSON crush, the kept-row
count equals the original count, so TOIN's record_compression receives
``compressed_count == original_count``. The plan framed this as a "0% reduction
/ zeroed learning signal."

Determination (by-design): the count axis is CORRECT — a lossless re-encoding
drops zero rows, so equal counts are the truth, and ``compressed_count`` feeds
``total_items_kept`` which must stay accurate. The real savings live on the
TOKEN axis: ``original_tokens``/``compressed_tokens`` are byte-derived from the
actual original/compressed payloads, so a ~89%-smaller lossless output yields a
~89% ``token_reduction`` in TOIN. No consumer drives a compression DECISION off
the count ratio (both axes are telemetry/dashboard aggregates), so the signal
is not zeroed — it is carried by the token axis.

This test LOCKS that determination: on a lossless crush, record_compression
sees equal counts AND a token count that reflects the real byte reduction. If
someone "fixes" #8 by falsifying ``compressed_count`` (lowering it below the
real rows kept), the count-equality assertion fails — guarding the
TOIN-count-integrity invariant.
"""
from __future__ import annotations

import json

import pytest

from headroom.transforms.smart_crusher import SmartCrusher, SmartCrusherConfig


def _lossless_array(n: int = 60) -> str:
    # Uniform-schema dicts: the analyzer compacts to csv-schema (lossless,
    # row-preserving) rather than dropping rows. Distinct values keep it
    # from collapsing to a row-drop strategy.
    items = [
        {"id": i, "status": "ok", "name": f"item_{i:03d}", "score": i * 7}
        for i in range(n)
    ]
    return json.dumps(items)


def test_lossless_crush_token_axis_carries_real_reduction(monkeypatch) -> None:
    captured: dict = {}

    crusher = SmartCrusher(SmartCrusherConfig())

    # Capture exactly what SmartCrusher hands to TOIN.
    def _capture(self_toin, **kwargs):
        captured.update(kwargs)

    # Patch the record path the crusher resolves lazily (get_toin().record_compression).
    import headroom.telemetry.toin as toin_mod

    monkeypatch.setattr(
        toin_mod.ToolIntelligenceNetwork, "record_compression", _capture, raising=True
    )
    # Force a fresh toin handle so the patched method is used.
    crusher._toin = None

    payload = _lossless_array(60)
    result = crusher.crush(payload, query="", bias=1.0)

    if not result.was_modified or not captured:
        pytest.skip("payload did not take the lossless recording path")

    # Count axis: lossless keeps every row -> counts equal. This is correct,
    # not a bug; it must NOT be falsified to manufacture a reduction.
    assert captured["original_count"] == captured["compressed_count"] == 60

    # Token axis: the REAL reduction. byte-derived tokens must show the
    # lossless output is materially smaller than the original.
    assert captured["compressed_tokens"] < captured["original_tokens"]
    token_reduction = 1 - captured["compressed_tokens"] / captured["original_tokens"]
    assert token_reduction > 0.3, (
        f"token-axis learning signal not carrying the reduction: {token_reduction:.2%}"
    )
