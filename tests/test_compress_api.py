"""Tests for the one-function compress() API."""

import json

import pytest

from furl_ctx.compress import CompressConfig, CompressResult, compress
from furl_ctx.hooks import CompressionHooks

# =============================================================================
# Tests: compress() function
# =============================================================================


class TestCompressFunction:
    def test_empty_messages(self):
        result = compress([], model="test")
        assert result.messages == []
        assert result.tokens_saved == 0

    def test_small_messages_passthrough(self):
        """Small messages below compression threshold pass through unchanged."""
        messages = [{"role": "user", "content": "hello"}]
        result = compress(messages, model="gpt-4o")
        assert result.messages[0]["content"] == "hello"
        assert result.tokens_saved == 0

    def test_returns_compress_result(self):
        result = compress([{"role": "user", "content": "hi"}])
        assert isinstance(result, CompressResult)
        assert hasattr(result, "messages")
        assert hasattr(result, "tokens_saved")
        assert hasattr(result, "compression_ratio")
        assert hasattr(result, "transforms_applied")

    def test_large_tool_output_compressed(self):
        """Large JSON tool output should be compressed."""
        big_data = json.dumps(
            [
                {"id": i, "status": "active", "name": f"item_{i}", "value": i * 17}
                for i in range(200)
            ]
        )
        messages = [
            {"role": "user", "content": "What are the top items?"},
            {"role": "tool", "content": big_data, "tool_call_id": "call_1"},
        ]
        result = compress(messages, model="gpt-4o")
        assert result.tokens_after <= result.tokens_before
        assert len(result.messages) == 2

    def test_compact_json_counts_tokens_not_whitespace(self):
        """Compact JSON arrays should still compress under token thresholds."""
        numbers = [42.0 + i * 0.1 for i in range(200)]
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Show metrics"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_metrics", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(numbers)},
        ]

        result = compress(messages, min_tokens_to_compress=250)

        assert result.tokens_saved > 0
        assert any(
            transform.startswith("router:smart_crusher") for transform in result.transforms_applied
        )

    def test_optimize_false_passthrough(self):
        """optimize=False returns messages unchanged."""
        messages = [{"role": "user", "content": "hello world " * 100}]
        result = compress(messages, optimize=False)
        assert result.messages is messages
        assert result.tokens_saved == 0

    def test_kwargs_do_not_mutate_caller_config(self):
        """kwarg overrides must not mutate the caller's CompressConfig."""
        cfg = CompressConfig()
        messages = [{"role": "user", "content": "hi"}]
        compress(messages, config=cfg, compress_user_messages=True, protect_recent=0)
        assert cfg.compress_user_messages is False
        assert cfg.protect_recent == 4

    def test_unknown_kwargs_raise(self):
        """Unknown kwargs (typos) must fail loudly, not silently default —
        matches the strict ``ContentRouter.apply()`` contract."""
        messages = [{"role": "user", "content": "hi"}]
        with pytest.raises(TypeError, match="target_ration"):
            compress(messages, target_ration=0.5)  # deliberate typo

    def test_with_custom_hooks(self):
        """Hooks are called when provided."""
        calls = []

        class TrackingHooks(CompressionHooks):
            def pre_compress(self, messages, ctx):
                calls.append(("pre", len(messages)))
                return messages

            def compute_biases(self, messages, ctx):
                calls.append(("biases", len(messages)))
                return {}

            def post_compress(self, event):
                calls.append(("post", event.tokens_saved))

        big_data = json.dumps([{"id": i, "status": "active"} for i in range(100)])
        messages = [
            {"role": "user", "content": "analyze"},
            {"role": "tool", "content": big_data, "tool_call_id": "c1"},
        ]
        compress(messages, hooks=TrackingHooks())

        assert any(c[0] == "pre" for c in calls)
        assert any(c[0] == "biases" for c in calls)


class TestCompressResultFields:
    def test_fields_populated(self):
        big_data = json.dumps([{"id": i, "type": "log"} for i in range(100)])
        messages = [
            {"role": "user", "content": "summarize"},
            {"role": "tool", "content": big_data, "tool_call_id": "c1"},
        ]
        result = compress(messages, model="claude-sonnet-4-5-20250929")
        assert result.tokens_before > 0
        assert result.tokens_after >= 0
        assert result.tokens_saved >= 0
        assert 0.0 <= result.compression_ratio <= 1.0


class TestNoneToolCallsRegression:
    """COR-46: ``tool_calls`` present with value ``None`` must never fail-open.

    openai-python's ``ChatCompletionMessage.model_dump()`` serializes plain
    assistant text turns as ``{"content": ..., "tool_calls": None}`` — the key
    is PRESENT with value None, so ``msg.get("tool_calls", [])`` returns None
    and iterating it raises TypeError. Because the message stays in history,
    that failed EVERY subsequent request (fail-open, breaker cycling, 0%
    compression forever). The walkers must use the ``or []`` idiom instead.
    """

    def test_none_content_and_none_tool_calls_does_not_fail_open(self):
        """The exact openai-python model_dump() shape must compress cleanly."""
        messages = [
            {"role": "user", "content": "read the config file"},
            # The killer shape: key present, value None (BEFORE the real tool
            # call, so the tool_call_id lookup walker must also survive it).
            {"role": "assistant", "content": None, "tool_calls": None},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_read_1",
                        "type": "function",
                        "function": {
                            "name": "Read",
                            "arguments": json.dumps({"file_path": "/tmp/config.json"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_read_1", "content": "key=value\n" * 120},
            {"role": "assistant", "content": None, "tool_calls": None},
            {"role": "user", "content": "now summarize it"},
        ]

        result = compress(messages, model="gpt-4o")

        assert result.error is None, (
            f"tool_calls=None must not trip the fail-open path: {result.error!r}"
        )
        assert len(result.messages) == len(messages)

    def test_minimal_none_shape_error_is_none(self):
        """Minimal regression fixture from the finding: content+tool_calls None."""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": None, "tool_calls": None},
        ]
        result = compress(messages, model="gpt-4o")
        assert result.error is None

    def test_walkers_tolerate_none_tool_calls_directly(self):
        """All three tool_calls walkers survive the present-but-None shape."""
        from furl_ctx.config import ReadLifecycleConfig
        from furl_ctx.transforms.content_router import ContentRouter
        from furl_ctx.transforms.read_lifecycle import ReadLifecycleManager

        assistant_none = {"role": "assistant", "content": None, "tool_calls": None}

        manager = ReadLifecycleManager(ReadLifecycleConfig())
        assert manager._build_tool_metadata([assistant_none]) == {}
        assert manager._find_tool_call_msg_index([assistant_none], "call_x") is None
        assert ContentRouter()._build_tool_name_map([assistant_none]) == {}
