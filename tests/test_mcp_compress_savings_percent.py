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

Round 6 adds finding D: a zero-savings response must NAME its reason — the
note carries the engine's raw ``transforms_applied`` strings verbatim, so a
0% is never unexplained.

Compression-neutral (metric/observability plane only).
"""

from __future__ import annotations

import importlib
import types

from furl_ctx.ccr.mcp_server import FurlMCPServer
from furl_ctx.compress import CompressResult

# furl_ctx/__init__.py re-exports the `compress` function, shadowing the
# submodule attribute, so `import furl_ctx.compress` binds the function.
# Resolve the actual module object to monkeypatch the name on it.
compress_mod = importlib.import_module("furl_ctx.compress")


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


def _patch_compress(
    monkeypatch,
    *,
    tokens_before: int,
    tokens_after: int,
    transforms: list[str] | None = None,
) -> None:
    """Force compress() to return a known CompressResult.

    compression_ratio is set to the SAVINGS fraction (its real semantics) so
    the test exercises the exact value the buggy formula mis-read.
    ``transforms`` overrides the stubbed ``transforms_applied`` list (finding
    D tests); ``None`` keeps the original stub value.
    """
    saved = tokens_before - tokens_after
    ratio = saved / tokens_before if tokens_before else 0.0
    result = CompressResult(
        messages=[{"role": "tool", "content": "x"}],
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        tokens_saved=saved,
        compression_ratio=ratio,
        transforms_applied=transforms if transforms is not None else ["log_compressor"],
    )
    monkeypatch.setattr(compress_mod, "compress", lambda *a, **k: result)


def test_savings_percent_agrees_with_tokens_saved(monkeypatch) -> None:
    # 1000 -> 43 tokens = 957 saved = 95.7% reduction. The buggy formula
    # (1 - 0.957) * 100 = 4.3 would report the COMPLEMENT; this asserts the
    # true value, so the reverted formula fails this test.
    _patch_compress(monkeypatch, tokens_before=1000, tokens_after=43)
    out = FurlMCPServer._compress_content(_stub_server(), "irrelevant payload")

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
    out = FurlMCPServer._compress_content(_stub_server(), "x")
    assert out["savings_percent"] == 70.0
    assert out["savings_percent"] != 30.0  # the inverted value


def test_zero_savings_reports_zero(monkeypatch) -> None:
    # Passthrough (no reduction): savings_percent == 0, tokens_saved == 0.
    _patch_compress(monkeypatch, tokens_before=50, tokens_after=50)
    out = FurlMCPServer._compress_content(_stub_server(), "x")
    assert out["tokens_saved"] == 0
    assert out["savings_percent"] == 0


# ─── round-6 finding D: zero-savings responses name their reason ─────────────


def test_zero_savings_note_names_the_raw_transforms(monkeypatch) -> None:
    """A bare 0% reads as a malfunction. The response note must carry the
    engine's own transforms_applied strings VERBATIM — no parsing or
    rewriting (the router owns their taxonomy; a parallel branch is enriching
    'router:noop' into 'router:noop:<reason>' and this surface must render
    whatever arrives)."""
    _patch_compress(
        monkeypatch,
        tokens_before=50,
        tokens_after=50,
        transforms=["router:noop:high-entropy"],
    )
    out = FurlMCPServer._compress_content(_stub_server(), "x")
    assert out["savings_percent"] == 0
    # The structured field carries the raw strings...
    assert out["transforms"] == ["router:noop:high-entropy"]
    # ...and the model-facing note names them too, verbatim.
    assert "router:noop:high-entropy" in out["note"]


def test_zero_savings_with_empty_transforms_reports_passthrough(monkeypatch) -> None:
    # Totality: an empty transforms list still yields an explanation.
    _patch_compress(monkeypatch, tokens_before=50, tokens_after=50, transforms=[])
    out = FurlMCPServer._compress_content(_stub_server(), "x")
    assert out["savings_percent"] == 0
    assert out["transforms"] == []
    assert "passthrough" in out["note"]


def test_nonzero_savings_note_stays_clean(monkeypatch) -> None:
    # The zero-savings explainer must not spam successful compressions.
    _patch_compress(monkeypatch, tokens_before=100, tokens_after=30)
    out = FurlMCPServer._compress_content(_stub_server(), "x")
    assert out["savings_percent"] == 70.0
    assert "No token savings" not in out["note"]


def test_tiny_real_savings_not_mislabeled_as_zero(monkeypatch) -> None:
    """F5 boundary: 1 token saved on a huge input rounds to a DISPLAYED
    savings_percent of 0.0, but the note must not claim "No token savings" —
    that statement would be false (the gate is tokens_saved == 0, not the
    rounded percentage). The transforms field is still rendered."""
    _patch_compress(monkeypatch, tokens_before=10000, tokens_after=9999)
    out = FurlMCPServer._compress_content(_stub_server(), "x")
    assert out["tokens_saved"] == 1
    assert out["savings_percent"] == 0.0  # rounded display value
    assert "No token savings" not in out["note"]
    assert out["transforms"] == ["log_compressor"]  # still present in the response


def test_note_explains_distinct_embedded_marker_hash_review_f6() -> None:
    """Review F6: a structured compression (here a JSON array tabled by the
    smart crusher) embeds a granular ``<<ccr:…>>`` marker whose hash DIFFERS
    from the whole-content ``hash``. Two different-looking hashes for one
    compression read as a bug unless the note names the relationship. Uses the
    REAL compress + store path (not the stub) so the embedded marker is real."""
    import json

    from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store
    from furl_ctx.ccr.marker_grammar import hashes_in_text

    reset_compression_store()
    try:
        rows = [
            {
                "event_id": f"e{i}",
                "amount": round(10 + i * 1.5, 2),
                "event_type": ["purchase", "refund", "login", "payout_request"][i % 4],
            }
            for i in range(200)
        ]
        server = types.SimpleNamespace(
            _get_local_store=lambda: get_compression_store(),
            _stats=types.SimpleNamespace(record_compression=lambda *a, **k: None),
        )
        out = FurlMCPServer._compress_content(server, json.dumps(rows))

        # Durable-success path — the path the finding is about.
        assert out.get("hash") is not None
        compressed = out["compressed"]
        text = compressed if isinstance(compressed, str) else json.dumps(compressed)
        embedded = [h for h in hashes_in_text(text) if h != out["hash"]]
        assert embedded, "expected a granular marker whose hash differs from the top-level hash"

        note = out["note"]
        assert "<<ccr:" in note
        assert "whole-content hash=" in note
        assert embedded[0] in note  # the actual differing hash is named

        # review F6 (round 2): the granular-marker note is plain user-facing
        # copy. Fragment markers stored under their own keys resolve against the
        # same underlying source document (not one shared "stored original"), and
        # the copy carries no em-dashes, en-dashes, or parentheses.
        assert "underlying source document" in note
        assert "same stored original" not in note
        granular = note[note.index("The compressed view also embeds") :]
        assert "—" not in granular and "–" not in granular, "no em/en dashes in the note"
        assert "(" not in granular and ")" not in granular, "no parentheses in the note"
    finally:
        reset_compression_store()
