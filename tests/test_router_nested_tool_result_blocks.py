"""Nested tool_result parts-list compression across all three transforms (COR-47).

The canonical Anthropic/MCP tool_result shape —
``content: [{"type": "text", "text": …}]`` — was never compressed by ANY of
the three transforms (all gated on ``isinstance(content, str)``) and the
router booked it as ``route_counts["small"]``, so the flagship MCP deployment
could silently sit at 0% while stats mislabeled megabyte payloads as "small".

Pinned here:
* the router routes each inner ``type=="text"`` part through
  ``_compress_content_block`` (shared two-tier cache), leaves non-text parts
  untouched, honors ``is_error`` / error-indicator / pinning / min_chars
  protections per part, and books the message under the new
  ``nested_blocks`` counter instead of ``small``;
* ``SmartCrusher.apply`` crushes inner text parts;
* ``CrossMessageDeduper`` dedups the concatenated text of all-text parts
  lists — including cross-shape against flat string occurrences — and never
  touches a parts list containing a non-text part.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from furl_ctx.cache.compression_store import get_compression_store
from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import EstimatingTokenCounter
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig
from furl_ctx.transforms.cross_message_dedup import MIN_DEDUP_CHARS, CrossMessageDeduper
from furl_ctx.transforms.smart_crusher import SmartCrusher

_DEDUP_SENTINEL_RE = re.compile(r"<<ccr:([0-9a-f]{24}) (\d+)_bytes_duplicate>>")


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


class _CapturingObserver:
    def __init__(self) -> None:
        self.route_counts: dict[str, int] | None = None

    def record_router_route_counts(self, counts: dict[str, int]) -> None:
        self.route_counts = dict(counts)

    def record_compression(self, *args: object, **kwargs: object) -> None:
        return None


def _big_text(tag: str = "") -> str:
    """Over the 500-char block floor; plain prose, no protection triggers."""
    return " ".join(
        f"entry {i}{tag}: the collector observed {1000 + i} events on channel "
        f"{i % 9} and wrote them to the durable segment without incident."
        for i in range(25)
    )


def _nested_tool_result_message(parts: list) -> dict:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "tu_nested", "content": parts}],
    }


class TestRouterNestedToolResult:
    def test_inner_text_part_is_compressed_not_booked_small(self, monkeypatch):
        """The reproduced COR-47 fixture: a nested text part must compress and
        must NOT be booked as route_counts['small'] (the old mislabel)."""
        text = _big_text()
        obs = _CapturingObserver()
        router = _make_router_with_mock_compress(monkeypatch)
        router._observer = obs
        msg = _nested_tool_result_message([{"type": "text", "text": text}])

        result = router.apply([msg], _make_tokenizer())

        part = result.messages[0]["content"][0]["content"][0]
        assert "[compressed]" in part["text"]
        assert "router:tool_result:text" in result.transforms_applied
        rc = obs.route_counts
        assert rc is not None
        assert rc.get("nested_blocks", 0) == 1
        assert rc.get("small", 0) == 0

    def test_non_text_parts_ship_untouched(self, monkeypatch):
        text = _big_text()
        image_part = {"type": "image", "source": {"type": "base64", "data": "aGk="}}
        router = _make_router_with_mock_compress(monkeypatch)
        msg = _nested_tool_result_message([{"type": "text", "text": text}, image_part])

        result = router.apply([msg], _make_tokenizer())

        parts = result.messages[0]["content"][0]["content"]
        assert "[compressed]" in parts[0]["text"]
        assert parts[1] == image_part

    def test_is_error_block_is_protected_whole(self, monkeypatch):
        text = _big_text()
        router = _make_router_with_mock_compress(monkeypatch)
        msg = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_err",
                    "is_error": True,
                    "content": [{"type": "text", "text": text}],
                }
            ],
        }

        result = router.apply([msg], _make_tokenizer())

        assert result.messages[0]["content"][0]["content"][0]["text"] == text
        assert "router:protected:error_output" in result.transforms_applied

    def test_short_parts_and_pinned_parts_untouched(self, monkeypatch):
        pinned = f"<<ccr:{'ab' * 12}>> " + _big_text("p")
        router = _make_router_with_mock_compress(monkeypatch)
        msg = _nested_tool_result_message(
            [{"type": "text", "text": "tiny"}, {"type": "text", "text": pinned}]
        )

        result = router.apply([msg], _make_tokenizer())

        parts = result.messages[0]["content"][0]["content"]
        assert parts[0]["text"] == "tiny"
        assert parts[1]["text"] == pinned

    def test_cache_control_part_untouched(self, monkeypatch):
        text = _big_text("cc")
        router = _make_router_with_mock_compress(monkeypatch)
        guarded = {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}
        msg = _nested_tool_result_message([guarded])

        result = router.apply([msg], _make_tokenizer())

        assert result.messages[0]["content"][0]["content"][0] == guarded

    def test_nested_parts_share_the_two_tier_cache(self, monkeypatch):
        """Second apply of the same nested text is a Tier-2 cache hit — the
        nested path rides the SAME cache the flat shapes use."""
        text = _big_text("cache")
        obs = _CapturingObserver()
        router = _make_router_with_mock_compress(monkeypatch)
        router._observer = obs
        msg = _nested_tool_result_message([{"type": "text", "text": text}])

        router.apply([msg], _make_tokenizer())
        first_hits = dict(router._cache.stats)["cache_hits"]
        result = router.apply([msg], _make_tokenizer())

        assert dict(router._cache.stats)["cache_hits"] - first_hits == 1
        assert "[compressed]" in result.messages[0]["content"][0]["content"][0]["text"]


class TestSmartCrusherNestedMirror:
    def test_nested_text_part_is_crushed(self):
        rows = [
            {
                "id": i,
                "path": f"/repo/src/module_{i}/file_{i}.py",
                "status": "ok",
                "duration_ms": (i * 13) % 500,
                "notes": f"scanned during pass {i % 4} with no findings recorded",
            }
            for i in range(80)
        ]
        payload = json.dumps(rows)
        msg = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_crush",
                    "content": [{"type": "text", "text": payload}],
                }
            ],
        }

        result = SmartCrusher().apply([msg], _make_tokenizer())

        new_text = result.messages[0]["content"][0]["content"][0]["text"]
        assert new_text != payload
        assert result.tokens_after < result.tokens_before
        assert result.markers_inserted
        assert any(t.startswith("smart_crush:") for t in result.transforms_applied)

    def test_flat_string_shape_unaffected_regression(self):
        """The str branch keeps working exactly as before the list arm."""
        rows = [{"k": i, "v": f"value-{i}", "path": f"/tmp/f_{i}.log"} for i in range(80)]
        payload = json.dumps(rows)
        msg = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_flat", "content": payload}],
        }

        result = SmartCrusher().apply([msg], _make_tokenizer())

        assert result.messages[0]["content"][0]["content"] != payload
        assert result.tokens_after < result.tokens_before


class TestDedupNestedMirror:
    def _tokenizer(self) -> Tokenizer:
        return Tokenizer(EstimatingTokenCounter())

    def _unique_payload(self, tag: str) -> str:
        payload = "\n".join(
            f"PASS nested_{tag}_{i:02d}::case_{i % 7} ({(i * 31) % 800}ms)" for i in range(14)
        )
        assert len(payload) >= MIN_DEDUP_CHARS
        return payload

    def _nested_msg(self, text: str, tu: str) -> dict:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tu,
                    "content": [{"type": "text", "text": text}],
                }
            ],
        }

    def test_exact_nested_duplicate_replaced_and_recoverable(self):
        payload = self._unique_payload("exact")
        messages = [self._nested_msg(payload, "a"), self._nested_msg(payload, "b")]

        result = CrossMessageDeduper().apply(messages, self._tokenizer())

        # First occurrence byte-identical; later occurrence replaced by a
        # single text part carrying the sentinel (list shape preserved).
        assert result.messages[0] == messages[0]
        replaced = result.messages[1]["content"][0]["content"]
        assert isinstance(replaced, list) and len(replaced) == 1
        sentinel_text = replaced[0]["text"]
        match = _DEDUP_SENTINEL_RE.search(sentinel_text)
        assert match is not None
        # Recovery invariant: the concatenated text is byte-recoverable.
        entry = get_compression_store().retrieve(match.group(1))
        assert entry is not None
        assert entry.original_content == payload

    def test_cross_shape_dedup_flat_string_then_nested(self):
        """A nested parts list whose concatenated text is byte-identical to an
        earlier FLAT string tool output dedups against it."""
        payload = self._unique_payload("xshape")
        messages = [
            {"role": "tool", "content": payload, "tool_call_id": "t1"},
            self._nested_msg(payload, "c"),
        ]

        result = CrossMessageDeduper().apply(messages, self._tokenizer())

        assert result.messages[0] == messages[0]
        replaced = result.messages[1]["content"][0]["content"]
        assert isinstance(replaced, list)
        assert _DEDUP_SENTINEL_RE.search(replaced[0]["text"]) is not None

    def test_parts_list_with_non_text_part_never_touched(self):
        payload = self._unique_payload("mixed")
        parts = [
            {"type": "text", "text": payload},
            {"type": "image", "source": {"type": "base64", "data": "aGk="}},
        ]
        messages = [
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "m1", "content": list(parts)}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "m2", "content": list(parts)}],
            },
        ]

        result = CrossMessageDeduper().apply(messages, self._tokenizer())

        # Eliding would lose the image part — both messages ship untouched.
        assert result.messages[0]["content"][0]["content"] == parts
        assert result.messages[1]["content"][0]["content"] == parts
        assert result.transforms_applied == []
