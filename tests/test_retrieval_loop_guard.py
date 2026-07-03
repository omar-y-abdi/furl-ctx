"""Retrieval-loop guard (ENGINE-COMPARISON P0-5).

CCR retrieval outputs (``furl_retrieve``) ARE the originals the engine
previously compressed. If the router compressed them again it would mint a
fresh retrieval marker for content the model just asked to see — a
compress → retrieve → compress ping-pong that burns tokens and can loop.
Upstream guards by ALWAYS excluding the retrieval tool's own outputs in the
router's tool-exclusion path.

Pinned here:

* the guard holds under the default config,
* the guard holds even when a caller OVERRIDES ``exclude_tools`` — including
  the empty set,
* the MCP channel spelling (``mcp__<server>__furl_retrieve``) is covered,
* the guard never decays with the read-protection window (ordinary excluded
  tools do; the retrieval tool must not — an aged-out retrieval output that
  recompresses re-opens the loop).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from furl_ctx.ccr import CCR_TOOL_NAME
from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import EstimatingTokenCounter
from furl_ctx.transforms.content_router import (
    ALWAYS_EXCLUDE_TOOLS,
    ContentRouter,
    ContentRouterConfig,
)


def _make_tokenizer() -> Tokenizer:
    return Tokenizer(EstimatingTokenCounter())


def _make_router_with_mock_compress(
    monkeypatch: pytest.MonkeyPatch,
    config: ContentRouterConfig | None = None,
) -> ContentRouter:
    """Router whose compress() always emits a half-length ``[compressed]``
    payload at ratio 0.5 — same pattern as test_router_function_role_gates."""
    router = ContentRouter(config or ContentRouterConfig())

    def fake_compress(content, context: str = "", bias: float = 1.0, **kwargs):
        return SimpleNamespace(
            compressed=content[: len(content) // 2] + "[compressed]",
            compression_ratio=0.5,
            strategy_used=SimpleNamespace(value="text"),
        )

    monkeypatch.setattr(router, "compress", fake_compress)
    return router


def _retrieval_output() -> str:
    """The shape ``furl_retrieve`` actually returns: the ORIGINAL items that
    were previously compressed away. Big enough to clear the 50-token raw
    floor and the 500-char block floor — exactly what would tempt the router
    into recompressing."""
    items = [
        {"index": i, "status": "ok", "detail": f"original row {i} payload with all fields intact"}
        for i in range(40)
    ]
    return json.dumps(items)


def _retrieve_messages(tool_name: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_ret", "function": {"name": tool_name}}],
        },
        {"role": "tool", "tool_call_id": "call_ret", "content": _retrieval_output()},
    ]


class TestGuardConstant:
    def test_guard_covers_both_retrieval_channels(self):
        """Direct tool-injection name and the MCP server spelling (any
        server alias) are both unconditionally excluded."""
        from furl_ctx.config import is_tool_excluded

        assert CCR_TOOL_NAME in ALWAYS_EXCLUDE_TOOLS
        assert is_tool_excluded("mcp__furl__furl_retrieve", ALWAYS_EXCLUDE_TOOLS)
        assert is_tool_excluded("mcp__my_furl__furl_retrieve", ALWAYS_EXCLUDE_TOOLS)
        # And it is a guard, not a blanket: sibling MCP tools stay eligible.
        assert not is_tool_excluded("mcp__github__list_issues", ALWAYS_EXCLUDE_TOOLS)


class TestRetrievalOutputsNeverCompressed:
    def test_protected_under_default_config(self, monkeypatch):
        router = _make_router_with_mock_compress(monkeypatch)
        content = _retrieval_output()

        result = router.apply(_retrieve_messages(CCR_TOOL_NAME), _make_tokenizer())

        assert result.messages[1]["content"] == content
        assert "router:excluded:tool" in result.transforms_applied

    def test_protected_even_with_empty_user_exclusions(self, monkeypatch):
        """The spec case: a caller overriding exclude_tools (here: nothing
        excluded at all) must not re-enable retrieval-output compression."""
        router = _make_router_with_mock_compress(
            monkeypatch, ContentRouterConfig(exclude_tools=set())
        )
        content = _retrieval_output()

        result = router.apply(_retrieve_messages(CCR_TOOL_NAME), _make_tokenizer())

        assert result.messages[1]["content"] == content
        assert "router:excluded:tool" in result.transforms_applied

    def test_mcp_channel_protected_with_empty_user_exclusions(self, monkeypatch):
        router = _make_router_with_mock_compress(
            monkeypatch, ContentRouterConfig(exclude_tools=set())
        )
        content = _retrieval_output()

        result = router.apply(_retrieve_messages("mcp__furl__furl_retrieve"), _make_tokenizer())

        assert result.messages[1]["content"] == content
        assert "router:excluded:tool" in result.transforms_applied

    def test_tool_result_block_protected_with_empty_user_exclusions(self, monkeypatch):
        """Anthropic/Claude-Code shape: tool_result block in a user message."""
        router = _make_router_with_mock_compress(
            monkeypatch, ContentRouterConfig(exclude_tools=set())
        )
        content = _retrieval_output()
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_ret",
                        "name": "mcp__furl__furl_retrieve",
                        "input": {"hash": "a" * 12},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_ret", "content": content}
                ],
            },
        ]

        result = router.apply(messages, _make_tokenizer())

        assert result.messages[1]["content"][0]["content"] == content
        assert "router:excluded:tool" in result.transforms_applied

    def test_parts_list_message_protected_with_empty_user_exclusions(self, monkeypatch):
        """OpenAI tool-role message whose content is a parts LIST (COR-48
        shape) — the message-level gate must honor the guard too."""
        router = _make_router_with_mock_compress(
            monkeypatch, ContentRouterConfig(exclude_tools=set())
        )
        content = _retrieval_output()
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_ret", "function": {"name": CCR_TOOL_NAME}}],
            },
            {
                "role": "tool",
                "tool_call_id": "call_ret",
                "content": [{"type": "text", "text": content}],
            },
        ]

        result = router.apply(messages, _make_tokenizer())

        assert result.messages[1]["content"][0]["text"] == content
        assert "router:excluded:tool" in result.transforms_applied


class TestGuardImmuneToAgeDecay:
    def test_ordinary_excluded_tools_decay_but_retrieval_does_not(self, monkeypatch):
        """With read_protection_window=0 every excluded tool ages out of
        protection (age-based decay) — EXCEPT the retrieval tool, whose
        recompression would re-open the retrieve loop."""
        router = _make_router_with_mock_compress(monkeypatch)
        read_content = " ".join(
            f"line {i}: def handler_{i}(): return registry.lookup({i})" for i in range(40)
        )
        retrieve_content = _retrieval_output()
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_read", "function": {"name": "Read"}},
                    {"id": "call_ret", "function": {"name": CCR_TOOL_NAME}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_read", "content": read_content},
            {"role": "tool", "tool_call_id": "call_ret", "content": retrieve_content},
        ]

        result = router.apply(messages, _make_tokenizer(), read_protection_window=0)

        # Read aged out of protection and compressed (existing decay behavior)
        assert "[compressed]" in result.messages[1]["content"]
        # The retrieval output did NOT decay — still verbatim
        assert result.messages[2]["content"] == retrieve_content

    def test_tool_result_block_immune_to_decay(self, monkeypatch):
        router = _make_router_with_mock_compress(monkeypatch)
        content = _retrieval_output()
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_ret",
                        "name": "mcp__furl__furl_retrieve",
                        "input": {"hash": "b" * 12},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_ret", "content": content}
                ],
            },
        ]

        result = router.apply(messages, _make_tokenizer(), read_protection_window=0)

        assert result.messages[1]["content"][0]["content"] == content
