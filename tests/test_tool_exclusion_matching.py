"""Tool-exclusion matching + Bash exclusion fix (ENGINE-COMPARISON P0-2).

Two regressions vs upstream are pinned here:

1. ``Bash`` sat in ``DEFAULT_EXCLUDE_TOOLS`` even though the comment above
   the set says Bash outputs (build logs, test output) are ideal compression
   targets — the LOG route was mostly dead in the default config and the
   ``Bash`` entry in ``DEFAULT_TOOL_PROFILES`` could never fire. Owner
   decision (confirmed): the frozenset was the bug, the comment is the
   truth. ``Bash``/``bash`` are removed from the default exclusion set, so
   Bash tool outputs now route to compression under the moderate profile.

2. Exclusion matching was exact-string only. Upstream ships
   ``is_tool_excluded``-style matching: case-insensitive compare plus
   fnmatch-style globs (e.g. ``mcp__*``). Restored as
   :func:`furl_ctx.config.is_tool_excluded` and threaded through every
   exclusion decision in the router; existing exact-string entries keep
   working unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from furl_ctx.config import (
    DEFAULT_EXCLUDE_TOOLS,
    DEFAULT_TOOL_PROFILES,
    is_tool_excluded,
)
from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import EstimatingTokenCounter
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig


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


def _big_tool_output() -> str:
    """Well over the 50-token raw-``apply()`` floor and the 500-char block
    floor; plain prose so no other protection gate fires."""
    return " ".join(
        f"row {i}: the indexer scanned partition {i % 7} and recorded "
        f"{1000 + i} documents with checksum digest value {i * 37}."
        for i in range(30)
    )


def _bash_build_log() -> str:
    """Log-shaped Bash output: repetitive npm build noise, WARN-only so the
    strong-error protection gate (>=2 distinct indicator keywords) cannot
    fire. Over the router's 50-token floor AND the default LogCompressor
    threshold (min_lines_for_ccr=50), with plenty of duplicate lines for
    the log compressor to fold."""
    lines = ["npm WARN deprecated left-pad@0.1.0: use String.prototype.padStart"] * 80
    lines += [f"added {n} packages in {n % 9}s" for n in (12, 40, 7)]
    return "\n".join(lines)


class TestDefaultExcludeTools:
    """Pin the P0-2 decision: Bash outputs are compression targets."""

    def test_bash_removed_from_default_exclude_tools(self):
        assert "Bash" not in DEFAULT_EXCLUDE_TOOLS
        assert "bash" not in DEFAULT_EXCLUDE_TOOLS

    def test_reference_tools_remain_excluded(self):
        """The reference-data tools (exact-content contracts) stay excluded."""
        for name in ("Read", "Glob", "Grep", "Write", "Edit"):
            assert name in DEFAULT_EXCLUDE_TOOLS, name

    def test_bash_profile_is_live_and_sane(self):
        """With Bash out of the exclusion set its DEFAULT_TOOL_PROFILES entry
        becomes reachable — pin that it is the moderate preset."""
        profile = DEFAULT_TOOL_PROFILES["Bash"]
        assert profile.bias == 1.0  # moderate: no extra keep/drop pressure
        assert profile.min_k >= 1  # never compresses to nothing
        # And the router resolves it (the profile can actually fire now).
        router = ContentRouter(ContentRouterConfig())
        assert router._get_tool_bias("Bash") == 1.0
        assert router._get_tool_bias("bash") == 1.0


class TestIsToolExcluded:
    """Unit surface for the restored upstream matcher."""

    def test_exact_entry_matches(self):
        assert is_tool_excluded("Read", DEFAULT_EXCLUDE_TOOLS)
        assert is_tool_excluded("Grep", DEFAULT_EXCLUDE_TOOLS)

    def test_case_insensitive_exact_entry(self):
        # "READ" is not literally in the set — only case-folded matching
        # can catch it.
        assert "READ" not in DEFAULT_EXCLUDE_TOOLS
        assert is_tool_excluded("READ", DEFAULT_EXCLUDE_TOOLS)
        assert is_tool_excluded("gLoB", DEFAULT_EXCLUDE_TOOLS)
        assert is_tool_excluded("read", frozenset({"Read"}))

    def test_glob_pattern_matches(self):
        assert is_tool_excluded("mcp__github__search_code", frozenset({"mcp__*"}))
        assert is_tool_excluded("tool_a", frozenset({"tool_?"}))

    def test_glob_matches_case_insensitively(self):
        assert is_tool_excluded("MCP__GitHub__Search_Code", frozenset({"mcp__*"}))

    def test_non_matching_stays_included(self):
        assert not is_tool_excluded("Bash", DEFAULT_EXCLUDE_TOOLS)
        assert not is_tool_excluded("WebFetch", frozenset({"mcp__*"}))
        assert not is_tool_excluded("Reader", DEFAULT_EXCLUDE_TOOLS)  # no prefix bleed
        assert not is_tool_excluded("Read", frozenset())

    def test_empty_name_never_excluded(self):
        assert not is_tool_excluded("", DEFAULT_EXCLUDE_TOOLS)
        assert not is_tool_excluded("", frozenset({"*"}))


class TestBashRouting:
    """Bash tool outputs now route to compression (the P0-2 behavior pin)."""

    def test_bash_tool_output_routes_to_compression(self, monkeypatch):
        router = _make_router_with_mock_compress(monkeypatch)
        content = _big_tool_output()
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_bash", "function": {"name": "Bash"}}],
            },
            {"role": "tool", "tool_call_id": "call_bash", "content": content},
        ]

        result = router.apply(messages, _make_tokenizer())

        assert "router:excluded:tool" not in result.transforms_applied
        assert "[compressed]" in result.messages[1]["content"]

    def test_bash_log_output_gets_compressed_for_real(self):
        """End-to-end (no mocks): a log-shaped Bash output goes through the
        real LOG route and actually shrinks."""
        router = ContentRouter(ContentRouterConfig())
        content = _bash_build_log()
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_bash", "function": {"name": "Bash"}}],
            },
            {"role": "tool", "tool_call_id": "call_bash", "content": content},
        ]

        result = router.apply(messages, _make_tokenizer())

        assert "router:excluded:tool" not in result.transforms_applied
        assert any(t.startswith("router:log:") for t in result.transforms_applied), (
            result.transforms_applied
        )
        assert result.messages[1]["content"] != content
        assert result.tokens_after < result.tokens_before


class TestRouterGlobAndCaseExclusion:
    """Glob + case-insensitive exclusion through the router's tool gates."""

    def test_mcp_glob_pattern_excludes_matching_tool(self, monkeypatch):
        router = _make_router_with_mock_compress(
            monkeypatch, ContentRouterConfig(exclude_tools={"mcp__*"})
        )
        content = _big_tool_output()
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "function": {"name": "mcp__github__list_issues"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": content},
        ]

        result = router.apply(messages, _make_tokenizer())

        assert result.messages[1]["content"] == content
        assert "router:excluded:tool" in result.transforms_applied

    def test_mcp_glob_matches_case_insensitively(self, monkeypatch):
        router = _make_router_with_mock_compress(
            monkeypatch, ContentRouterConfig(exclude_tools={"mcp__*"})
        )
        content = _big_tool_output()
        msg = {
            "role": "function",
            "name": "MCP__GitHub__List_Issues",
            "content": content,
        }

        result = router.apply([msg], _make_tokenizer())

        assert result.messages[0]["content"] == content
        assert "router:excluded:tool" in result.transforms_applied

    def test_uppercase_variant_of_default_entry_is_excluded(self, monkeypatch):
        router = _make_router_with_mock_compress(monkeypatch)
        content = _big_tool_output()
        msg = {"role": "function", "name": "READ", "content": content}

        result = router.apply([msg], _make_tokenizer())

        assert result.messages[0]["content"] == content
        assert "router:excluded:tool" in result.transforms_applied

    def test_non_matching_tool_still_compresses(self, monkeypatch):
        """Control: glob support must not blanket-protect tool traffic."""
        router = _make_router_with_mock_compress(
            monkeypatch, ContentRouterConfig(exclude_tools={"mcp__*"})
        )
        msg = {"role": "function", "name": "WebFetch", "content": _big_tool_output()}

        result = router.apply([msg], _make_tokenizer())

        assert "[compressed]" in result.messages[0]["content"]

    def test_glob_excludes_tool_result_block(self, monkeypatch):
        """Block path (Anthropic shape): excluded_tool_ids is built with the
        same matcher, so a glob entry protects tool_result blocks too."""
        router = _make_router_with_mock_compress(
            monkeypatch, ContentRouterConfig(exclude_tools={"mcp__*"})
        )
        content = _big_tool_output()
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "mcp__github__get_file",
                        "input": {},
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": content}],
            },
        ]

        result = router.apply(messages, _make_tokenizer())

        assert result.messages[1]["content"][0]["content"] == content
        assert "router:excluded:tool" in result.transforms_applied
