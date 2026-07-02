"""Real-tokenizer units in the compression plane (COR-17).

Every "token" inside the compression plane used to be ``len(x.split())``
(whitespace words), while the acceptance gate ``compression_ratio <
min_ratio`` compared that word-ratio against a threshold derived from
TOKENIZER-measured context pressure, and eligibility used the real tokenizer.
Compaction outputs (CSV, comma-joined) have few spaces, so word-ratios
systematically overstated savings.

The fix threads an optional ``token_counter`` through ``compress()`` →
``_compress_mixed`` / ``_compress_pure`` → the dispatcher, defaulting to the
historical word count. ``apply()`` ACTIVATES it with the request's
``tokenizer.count_text`` at all three compress sites (inline pass, parallel
workers, content-block path), so the gate compares like units.

Pinned here:
* the dispatcher measures original + compressed tokens with the injected
  counter (and the word-count default stays byte-identical to before);
* ``compress()`` plumbs the counter into its routing log;
* ``apply()`` activates the real tokenizer on both the string path and the
  content-block path.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from headroom.tokenizer import Tokenizer
from headroom.tokenizers import EstimatingTokenCounter
from headroom.transforms.content_router import (
    ContentRouter,
    ContentRouterConfig,
)
from headroom.transforms.router_dispatch import StrategyDispatcher
from headroom.transforms.router_policy import CompressionStrategy

_CHAR_COUNTER = len  # a deliberately different unit than words


def _make_tokenizer() -> Tokenizer:
    return Tokenizer(EstimatingTokenCounter())


def _dispatcher() -> StrategyDispatcher:
    return StrategyDispatcher(
        ContentRouterConfig(),
        logger=logging.getLogger("test.router_dispatch"),
        log_router_debug=lambda *a, **k: None,
        json_shape=lambda content: {},
    )


def _apply_dispatch(dispatcher, content, strategy, *, crusher=None, token_counter=None, toin=None):
    return dispatcher.apply(
        content,
        strategy,
        "",
        get_smart_crusher=lambda: crusher,
        get_search_compressor=lambda: None,
        get_log_compressor=lambda: None,
        get_diff_compressor=lambda: None,
        get_html_extractor=lambda: None,
        record_to_toin=(toin if toin is not None else (lambda **kwargs: None)),
        token_counter=token_counter,
    )


class TestDispatcherUnits:
    CONTENT = "alpha beta gamma delta epsilon zeta eta theta iota kappa " * 8

    def test_passthrough_counts_with_injected_counter(self):
        compressed, tokens, chain = _apply_dispatch(
            _dispatcher(),
            self.CONTENT,
            CompressionStrategy.PASSTHROUGH,
            token_counter=_CHAR_COUNTER,
        )
        assert compressed == self.CONTENT
        assert tokens == len(self.CONTENT)  # chars, not words

    def test_passthrough_default_stays_word_count(self):
        """No counter → byte-identical legacy behavior (whitespace words)."""
        compressed, tokens, chain = _apply_dispatch(
            _dispatcher(), self.CONTENT, CompressionStrategy.PASSTHROUGH
        )
        assert tokens == len(self.CONTENT.split())

    def test_smart_crusher_output_counted_in_gate_units(self):
        """The COR-17 core case: comma-joined compaction output is ONE word
        but many real tokens — the injected counter must measure it, and the
        same units must reach TOIN."""
        compacted = "id,path,status,duration\n" + ",".join(str(i) for i in range(40))
        crusher = SimpleNamespace(
            crush=lambda content, query="", bias=1.0: SimpleNamespace(compressed=compacted)
        )
        recorded: list[dict] = []

        compressed, tokens, chain = _apply_dispatch(
            _dispatcher(),
            self.CONTENT,
            CompressionStrategy.SMART_CRUSHER,
            crusher=crusher,
            token_counter=_CHAR_COUNTER,
            toin=lambda **kwargs: recorded.append(kwargs),
        )

        assert compressed == compacted
        assert tokens == len(compacted)  # char units — words would be 2
        assert recorded[0]["original_tokens"] == len(self.CONTENT)
        assert recorded[0]["compressed_tokens"] == len(compacted)


class TestCompressPlumbing:
    def test_routing_log_uses_injected_counter(self, monkeypatch):
        router = ContentRouter(ContentRouterConfig())
        monkeypatch.setattr(
            router, "_determine_strategy", lambda content: CompressionStrategy.PASSTHROUGH
        )
        content = "one two three four five six seven eight nine ten " * 6

        result = router.compress(content, token_counter=_CHAR_COUNTER)

        assert result.routing_log[0].original_tokens == len(content)
        assert result.routing_log[0].compressed_tokens == len(content)

    def test_routing_log_defaults_to_word_count(self, monkeypatch):
        router = ContentRouter(ContentRouterConfig())
        monkeypatch.setattr(
            router, "_determine_strategy", lambda content: CompressionStrategy.PASSTHROUGH
        )
        content = "one two three four five six seven eight nine ten " * 6

        result = router.compress(content)

        assert result.routing_log[0].original_tokens == len(content.split())


class TestApplyActivation:
    """``apply()`` must thread the request tokenizer's ``count_text`` into
    every compress() call — string path and content-block path."""

    def _capturing_router(self, monkeypatch: pytest.MonkeyPatch):
        router = ContentRouter(ContentRouterConfig())
        captured: list[dict] = []

        def fake_compress(content, context="", bias=1.0, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                compressed=content[: len(content) // 2] + "[compressed]",
                compression_ratio=0.5,
                strategy_used=SimpleNamespace(value="text"),
            )

        monkeypatch.setattr(router, "compress", fake_compress)
        return router, captured

    def test_string_path_passes_request_tokenizer(self, monkeypatch):
        router, captured = self._capturing_router(monkeypatch)
        tokenizer = _make_tokenizer()
        content = " ".join(f"line {i} of a large enough tool output body" for i in range(40))
        msg = {"role": "tool", "content": content, "tool_call_id": "call_units"}

        router.apply([msg], tokenizer)

        assert captured, "compress() was never reached"
        assert captured[0].get("token_counter") == tokenizer.count_text

    def test_block_path_passes_request_tokenizer(self, monkeypatch):
        router, captured = self._capturing_router(monkeypatch)
        tokenizer = _make_tokenizer()
        text = " ".join(f"record {i} in a large enough tool_result payload" for i in range(40))
        msg = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_units", "content": text}],
        }

        router.apply([msg], tokenizer)

        assert captured, "compress() was never reached"
        assert captured[0].get("token_counter") == tokenizer.count_text
