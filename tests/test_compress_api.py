"""Tests for the one-function compress() API."""

import json

from headroom.compress import CompressConfig, CompressResult, compress
from headroom.hooks import CompressionHooks

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
        compress(messages, config=cfg, target_ratio=0.5, protect_recent=0)
        assert cfg.target_ratio is None
        assert cfg.protect_recent == 4

    def test_unknown_kwargs_are_logged(self, caplog):
        """Unknown kwargs (typos) must be surfaced, not silently ignored."""
        import logging

        messages = [{"role": "user", "content": "hi"}]
        with caplog.at_level(logging.WARNING, logger="headroom.compress"):
            compress(messages, target_ration=0.5)  # deliberate typo
        assert any("target_ration" in record.message for record in caplog.records)

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

