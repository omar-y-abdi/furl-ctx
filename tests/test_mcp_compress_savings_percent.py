"""Regression test for #19: savings_percent was inverted.

_compress_content computed
    savings_pct = (1 - result.compression_ratio) * 100   if ratio < 1.0

but CompressResult.compression_ratio is the SAVINGS fraction
(0.957 == 95.7% removed), so a 95.7%-reduction was reported as
savings_percent=4.3 — the complement. The reported savings_percent must
agree with tokens_saved / original_tokens, which sit beside it in the same
response dict and are computed from mcp's own token counts.

Fix: derive savings_percent from the same source as tokens_saved
(``max(0, input - output) / input``). Single-site change; the sibling stats
sites already computed from tokens directly.

Compression-neutral (metric/observability plane only).
"""

from __future__ import annotations

import importlib
import types

from headroom.ccr.mcp_server import HeadroomMCPServer
from headroom.compress import CompressResult

# headroom/__init__.py re-exports the `compress` function, shadowing the
# submodule attribute, so `import headroom.compress` binds the function.
# Resolve the actual module object to monkeypatch the name on it.
compress_mod = importlib.import_module("headroom.compress")


def _stub_server():
    # _get_local_store + _stats are the only collaborators _compress_content
    # touches besides the compress() call (which we monkeypatch).
    captured: dict[str, str] = {}

    def _store(**kwargs):
        captured["hash"] = "deadbeefcafe"
        return "deadbeefcafe"

    return types.SimpleNamespace(
        _get_local_store=lambda: types.SimpleNamespace(store=_store),
        _stats=types.SimpleNamespace(record_compression=lambda *a, **k: None),
    )


def _patch_compress(monkeypatch, *, tokens_before: int, tokens_after: int) -> None:
    """Force compress() to return a known CompressResult.

    compression_ratio is set to the SAVINGS fraction (its real semantics) so
    the test exercises the exact value the buggy formula mis-read.
    """
    saved = tokens_before - tokens_after
    ratio = saved / tokens_before if tokens_before else 0.0
    result = CompressResult(
        messages=[{"role": "tool", "content": "x"}],
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        tokens_saved=saved,
        compression_ratio=ratio,
        transforms_applied=["log_compressor"],
    )
    monkeypatch.setattr(compress_mod, "compress", lambda *a, **k: result)


def test_savings_percent_agrees_with_tokens_saved(monkeypatch) -> None:
    # 1000 -> 43 tokens = 957 saved = 95.7% reduction. The buggy formula
    # (1 - 0.957) * 100 = 4.3 would report the COMPLEMENT; this asserts the
    # true value, so the reverted formula fails this test.
    _patch_compress(monkeypatch, tokens_before=1000, tokens_after=43)
    out = HeadroomMCPServer._compress_content(_stub_server(), "irrelevant payload")

    assert out["original_tokens"] == 1000
    assert out["compressed_tokens"] == 43
    assert out["tokens_saved"] == 957
    # Structural agreement: savings_percent == tokens_saved / original_tokens.
    expected = round(out["tokens_saved"] / out["original_tokens"] * 100, 1)
    assert out["savings_percent"] == expected == 95.7


def test_savings_percent_not_the_complement(monkeypatch) -> None:
    # Mutation guard: an asymmetric case (70% reduction) where the old
    # (1 - ratio) formula yields a distinctly different number (30.0).
    _patch_compress(monkeypatch, tokens_before=100, tokens_after=30)
    out = HeadroomMCPServer._compress_content(_stub_server(), "x")
    assert out["savings_percent"] == 70.0
    assert out["savings_percent"] != 30.0  # the inverted value


def test_zero_savings_reports_zero(monkeypatch) -> None:
    # Passthrough (no reduction): savings_percent == 0, tokens_saved == 0.
    _patch_compress(monkeypatch, tokens_before=50, tokens_after=50)
    out = HeadroomMCPServer._compress_content(_stub_server(), "x")
    assert out["tokens_saved"] == 0
    assert out["savings_percent"] == 0
