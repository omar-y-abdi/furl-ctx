"""API-envelope ingestion: sniff predicate, detection, byte-exact recovery, veto."""

from __future__ import annotations

import json

from furl_ctx.cache.compression_store import (
    get_compression_store,
    reset_compression_store,
)
from furl_ctx.ccr.marker_grammar import BRACKET_RETRIEVE_PATTERN
from furl_ctx.tokenizers import get_tokenizer
from furl_ctx.transforms import envelope_ingest
from furl_ctx.transforms.content_detector import ContentType, detect_content_type
from furl_ctx.transforms.content_router import ContentRouter
from furl_ctx.transforms.csv_ingest import raw_recovery_hash
from furl_ctx.transforms.envelope_ingest import sniff_envelope
from furl_ctx.transforms.router_policy import CompressionStrategy

_COUNT = get_tokenizer("gpt-4o").count_text


def _envelope(n: int = 60) -> str:
    return json.dumps(
        {
            "data": [
                {"id": i, "name": f"item-{i}", "score": i * 2, "tag": "alpha"} for i in range(n)
            ],
            "total": n,
            "next_cursor": "cursor-xyz",
        }
    )


# ── sniff_envelope predicate (shared by detection + dispatch) ────────────────


def test_sniff_hits_single_data_array() -> None:
    view = sniff_envelope(_envelope())
    assert view is not None
    assert view.key == "data"
    assert len(view.inner) == 60
    assert view.other == {"total": 60, "next_cursor": "cursor-xyz"}


def test_sniff_misses_plain_array() -> None:
    assert sniff_envelope('[{"id": 1}]') is None


def test_sniff_misses_no_wrapper_key() -> None:
    assert sniff_envelope('{"status": "ok", "meta": {"total": 5}}') is None


def test_sniff_misses_empty_array() -> None:
    assert sniff_envelope('{"data": []}') is None


def test_sniff_misses_non_dict_items() -> None:
    assert sniff_envelope('{"data": [1, 2, 3]}') is None


def test_sniff_ambiguous_two_arrays_fails_open() -> None:
    assert sniff_envelope('{"data": [{"a": 1}], "results": [{"b": 2}]}') is None


def test_sniff_misses_non_json() -> None:
    assert sniff_envelope("not json") is None


# ── detection routes the envelope through the JSON_ARRAY / SmartCrusher path ──


def test_detection_routes_envelope_as_json_array() -> None:
    result = detect_content_type(_envelope())
    assert result.content_type == ContentType.JSON_ARRAY
    assert result.metadata.get("envelope") is True
    assert result.metadata.get("envelope_key") == "data"


# ── end-to-end through the real router: compresses, recovers byte-exact ───────


def test_recovery_byte_exact_envelope_through_real_router() -> None:
    reset_compression_store()
    try:
        raw = _envelope(200)
        result = ContentRouter().compress(raw, token_counter=_COUNT)
        assert result.strategy_used is CompressionStrategy.SMART_CRUSHER
        assert result.compressed != raw
        assert _COUNT(result.compressed) < _COUNT(raw)  # actually smaller
        assert "next_cursor" in result.compressed  # non-array meta preserved inline

        match = BRACKET_RETRIEVE_PATTERN.search(result.compressed)
        assert match is not None, f"recovery marker missing: {result.compressed!r}"
        assert match.group(3) == raw_recovery_hash(raw)
        entry = get_compression_store().retrieve(match.group(3))
        assert entry is not None, "marker hash must resolve in the store"
        assert entry.original_content == raw  # byte-exact original envelope
    finally:
        reset_compression_store()


def test_compress_envelope_vetoes_on_persist_failure(monkeypatch) -> None:
    # persist failure ⇒ compress_envelope returns None so the caller serves the
    # raw envelope — the envelope marker must never ship dangling. (Tested at the
    # unit level: through the full router a large raw payload is separately picked
    # up by the engine's own backed CCR-offload, which would confound the assert.)
    from types import SimpleNamespace

    monkeypatch.setattr(envelope_ingest, "persist_to_python_ccr", lambda *a, **k: False)
    raw = _envelope(50)
    view = sniff_envelope(raw)
    assert view is not None
    fake_crusher = SimpleNamespace(
        crush=lambda content, *, query="", bias=1.0: SimpleNamespace(compressed="X")
    )
    out = envelope_ingest.compress_envelope(raw, view, fake_crusher, token_counter=len)
    assert out is None  # vetoed → no dangling marker
