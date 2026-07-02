"""Text-block already-compressed pinning covers ``<<ccr:`` sentinels (COR-31).

The text-block pin keyed on the human phrases (``Retrieve more: hash=`` /
``Retrieve original: hash=``) only — but the smart-crusher path emits
``<<ccr:HASH>>`` sentinels WITHOUT either phrase at default configuration, so
crushed output re-entering as a text block was re-compressed after
result-cache expiry, and sentinel-row survival through a second crush is not
contractual. The flat string path and the tool_result path already pin via
the strict marker grammar (``_looks_like_ccr_output``); this file pins the
text-block path's parity.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import EstimatingTokenCounter
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig


def _make_tokenizer() -> Tokenizer:
    return Tokenizer(EstimatingTokenCounter())


def _make_router_with_mock_compress(monkeypatch: pytest.MonkeyPatch) -> ContentRouter:
    router = ContentRouter(ContentRouterConfig())

    def fake_compress(content, context: str = "", bias: float = 1.0, **kwargs):
        return SimpleNamespace(
            compressed=content[: len(content) // 2] + "[compressed]",
            compression_ratio=0.5,
            strategy_used=SimpleNamespace(value="text"),
        )

    monkeypatch.setattr(router, "compress", fake_compress)
    return router


def _filler() -> str:
    return " ".join(f"kept row {i} with plain descriptive text" for i in range(30))


def _tool_text_message(text: str) -> dict:
    # tool-role text blocks compress freely — the only gate left is pinning.
    return {"role": "tool", "content": [{"type": "text", "text": text}]}


def test_bare_ccr_sentinel_pins_text_block(monkeypatch):
    """A real engine-emitted 24-hex ``<<ccr:HASH>>`` sentinel with NO human
    phrase (the default smart-crusher shape) must pin the block."""
    router = _make_router_with_mock_compress(monkeypatch)
    pinned = f'{{"_ccr_dropped": "<<ccr:{"ab" * 12}>>"}} ' + _filler()
    result = router.apply([_tool_text_message(pinned)], _make_tokenizer())

    assert result.messages[0]["content"][0]["text"] == pinned
    assert "[compressed]" not in result.messages[0]["content"][0]["text"]


def test_twelve_hex_row_marker_pins_text_block(monkeypatch):
    """The 12-hex crusher row-marker shape pins too."""
    router = _make_router_with_mock_compress(monkeypatch)
    pinned = f"<<ccr:{'0' * 12} 53_chunks>> " + _filler()
    result = router.apply([_tool_text_message(pinned)], _make_tokenizer())

    assert result.messages[0]["content"][0]["text"] == pinned


def test_invalid_marker_mention_still_compresses(monkeypatch):
    """Content that merely MENTIONS the grammar (docs, this repo's own source
    read back) uses placeholders and must stay compressible."""
    router = _make_router_with_mock_compress(monkeypatch)
    mention = "the marker format is <<ccr:HASH>> as documented " + _filler()
    result = router.apply([_tool_text_message(mention)], _make_tokenizer())

    assert "[compressed]" in result.messages[0]["content"][0]["text"]


def test_legacy_phrase_pin_still_honored(monkeypatch):
    """Back-compat: the loose phrase substrings keep pinning (pre-existing
    behavior, relied on by older marker producers)."""
    router = _make_router_with_mock_compress(monkeypatch)
    pinned = "Retrieve more: hash=abc " + _filler()
    result = router.apply([_tool_text_message(pinned)], _make_tokenizer())

    assert result.messages[0]["content"][0]["text"] == pinned
