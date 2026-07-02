"""Function-role parity for the router's tool gates (COR-48).

The router's protection gates fired only on ``role == "tool"`` while dedup's
eligibility set is ``{"tool", "function"}`` — a legacy OpenAI function-calling
message (``role: "function", name: "Read"``) was COMPRESSED despite ``Read``
being in ``DEFAULT_EXCLUDE_TOOLS``, and its error outputs lost the verbatim
protection. Separately, an OpenAI tool-role message whose content is a parts
LIST reached the block walker, which checks exclusion only via block-level
``tool_use_id`` — a field that shape doesn't carry — so excluded-tool
protection vanished for it too.

Pinned here:
* uniform ``role in {"tool", "function"}`` at the exclusion and
  error-protection gates, with function names resolved from
  ``message["name"]`` (which also backstops tool-role messages whose call id
  was never mapped);
* the message-level exclusion check in the block (parts-list) path.
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
    """Router whose compress() always emits a half-length ``[compressed]``
    payload at ratio 0.5 — same pattern as test_transforms_content_router."""
    router = ContentRouter(ContentRouterConfig())

    def fake_compress(content, context: str = "", bias: float = 1.0, **kwargs):
        return SimpleNamespace(
            compressed=content[: len(content) // 2] + "[compressed]",
            compression_ratio=0.5,
            strategy_used=SimpleNamespace(value="text"),
        )

    monkeypatch.setattr(router, "compress", fake_compress)
    return router


def _big_tool_output() -> str:
    """Well over the 50-token raw-``apply()`` floor and the 500-char block
    floor; plain prose so no other protection gate fires."""
    return " ".join(
        f"row {i}: the indexer scanned partition {i % 7} and recorded "
        f"{1000 + i} documents with checksum digest value {i * 37}."
        for i in range(30)
    )


def _error_output() -> str:
    """Strong error output: >=2 distinct indicator keywords, non-JSON,
    under the 8000-char protection cap, over the 50-token floor."""
    frames = "\n".join(
        f'  File "/srv/app/module_{i}.py", line {10 + i}, in step_{i}' for i in range(8)
    )
    return (
        "Traceback (most recent call last):\n"
        f"{frames}\n"
        "ValueError: connection refused by upstream service\n"
        "ERROR: task aborted after 3 retries, see the frames above for details\n"
        "the worker halted and wrote no further output to the result stream\n"
    )


class TestFunctionRoleStringContent:
    def test_function_role_excluded_tool_is_protected(self, monkeypatch):
        router = _make_router_with_mock_compress(monkeypatch)
        content = _big_tool_output()
        msg = {"role": "function", "name": "Read", "content": content}

        result = router.apply([msg], _make_tokenizer())

        assert result.messages[0]["content"] == content
        assert "router:excluded:tool" in result.transforms_applied

    def test_function_role_error_output_is_protected(self, monkeypatch):
        router = _make_router_with_mock_compress(monkeypatch)
        content = _error_output()
        msg = {"role": "function", "name": "run_pipeline", "content": content}

        result = router.apply([msg], _make_tokenizer())

        assert result.messages[0]["content"] == content
        assert "router:protected:error_output" in result.transforms_applied

    def test_function_role_non_excluded_still_compresses(self, monkeypatch):
        """Control: the widened gate must not blanket-protect function-role
        traffic — non-excluded names keep compressing."""
        router = _make_router_with_mock_compress(monkeypatch)
        msg = {"role": "function", "name": "custom_lookup", "content": _big_tool_output()}

        result = router.apply([msg], _make_tokenizer())

        assert "[compressed]" in result.messages[0]["content"]

    def test_tool_role_name_field_backstops_unmapped_call_id(self, monkeypatch):
        """A tool-role message whose tool_call_id was never mapped (assistant
        turn truncated out of history) but which carries ``name`` resolves
        exclusion through the name."""
        router = _make_router_with_mock_compress(monkeypatch)
        content = _big_tool_output()
        msg = {
            "role": "tool",
            "tool_call_id": "call_orphaned",
            "name": "Grep",
            "content": content,
        }

        result = router.apply([msg], _make_tokenizer())

        assert result.messages[0]["content"] == content
        assert "router:excluded:tool" in result.transforms_applied


class TestPartsListMessageLevelExclusion:
    """The block-path shape: role tool/function with ``content`` as a parts
    list. Exclusion must be resolved at MESSAGE level (tool_call_id / name)
    because the text parts carry no ``tool_use_id``."""

    def _parts(self, text: str) -> list[dict]:
        return [{"type": "text", "text": text}]

    def test_tool_role_parts_list_excluded_by_message_tool_call_id(self, monkeypatch):
        router = _make_router_with_mock_compress(monkeypatch)
        content = _big_tool_output()
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "function": {"name": "Read"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": self._parts(content)},
        ]

        result = router.apply(messages, _make_tokenizer())

        assert result.messages[1]["content"][0]["text"] == content
        assert "router:excluded:tool" in result.transforms_applied

    def test_function_role_parts_list_excluded_by_name(self, monkeypatch):
        router = _make_router_with_mock_compress(monkeypatch)
        content = _big_tool_output()
        msg = {"role": "function", "name": "Read", "content": self._parts(content)}

        result = router.apply([msg], _make_tokenizer())

        assert result.messages[0]["content"][0]["text"] == content
        assert "router:excluded:tool" in result.transforms_applied

    def test_tool_role_parts_list_non_excluded_compresses(self, monkeypatch):
        """Control: message-level exclusion is exclusion-list-driven, not a
        blanket protection of the parts-list shape."""
        router = _make_router_with_mock_compress(monkeypatch)
        content = _big_tool_output()
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_2", "function": {"name": "custom_lookup"}}],
            },
            {"role": "tool", "tool_call_id": "call_2", "content": self._parts(content)},
        ]

        result = router.apply(messages, _make_tokenizer())

        assert "[compressed]" in result.messages[1]["content"][0]["text"]
