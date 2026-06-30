"""Tests for the frozen-prefix logic in compress.py.

Contract tested (Contract #2 — prompt-cache ordering):
- For any request with a cache_control marker at message index k,
  messages[0..=k] are returned BYTE-IDENTICAL (no compression, no
  reorder, msg 0 preserved, cache_control bytes intact).

TDD RED tests written before _compute_frozen_message_count is added to
compress.py.  They should FAIL before the implementation is added.

Parity tests (adapter fixtures):
  Feed the same logical bodies to the Python helper and verify the
  Python helper returns the same integer as the Rust test asserts.
  The Rust expected values are taken verbatim from
  crates/headroom-core/tests/cache_control.rs.
"""

from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from headroom.compress import _compute_frozen_message_count, compress


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _big_json_content() -> str:
    """Return a large JSON array that SmartCrusher will want to compress."""
    rows = [
        {"id": i, "status": "active", "name": f"item_{i:04d}", "value": i * 17}
        for i in range(300)
    ]
    return json.dumps(rows)


# ---------------------------------------------------------------------------
# Unit tests for _compute_frozen_message_count
# ---------------------------------------------------------------------------


class TestComputeFrozenMessageCount:
    """Unit tests for the _compute_frozen_message_count() helper."""

    def test_no_messages_yields_zero(self) -> None:
        assert _compute_frozen_message_count([]) == 0

    def test_no_cache_control_anywhere_yields_zero(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "world"}]},
        ]
        assert _compute_frozen_message_count(messages) == 0

    def test_marker_on_index_0_yields_1(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first", "cache_control": {"type": "ephemeral"}},
                ],
            },
            {"role": "assistant", "content": "second"},
        ]
        assert _compute_frozen_message_count(messages) == 1

    def test_marker_on_index_k_yields_k_plus_1(self) -> None:
        # Marker on messages[3] => frozen = 4  (mirrors Rust test)
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "fourth",
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
            },
            {"role": "user", "content": "fifth"},
        ]
        assert _compute_frozen_message_count(messages) == 4

    def test_highest_marker_wins(self) -> None:
        """Multiple markers across messages — highest index is the floor."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "m0", "cache_control": {"type": "ephemeral"}},
                ],
            },
            {"role": "assistant", "content": "m1 string"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "m2", "cache_control": {"type": "ephemeral"}},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "m3"}]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "m4",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    },
                ],
            },
            {"role": "assistant", "content": "m5 string"},
        ]
        # Highest marker is on index 4; floor = 5.
        assert _compute_frozen_message_count(messages) == 5

    def test_string_content_messages_never_bump_floor(self) -> None:
        """String content (no blocks) can never carry cache_control — skip."""
        messages = [
            {"role": "user", "content": "plain string"},
            {"role": "assistant", "content": "another plain string"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "now with marker",
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
            },
        ]
        assert _compute_frozen_message_count(messages) == 3

    def test_multiple_blocks_same_message_counts_once(self) -> None:
        """Multiple marked blocks within one message = floor from that message index."""
        messages = [
            {"role": "user", "content": "first"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "block A",
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": "block B",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    },
                    {"type": "text", "text": "block C", "cache_control": {"type": "ephemeral"}},
                ],
            },
        ]
        assert _compute_frozen_message_count(messages) == 2

    # ----- Edge cases -------------------------------------------------------

    def test_non_dict_block_skipped_gracefully(self) -> None:
        """Non-dict content blocks (strings, ints) must not raise."""
        messages = [
            {
                "role": "user",
                "content": [
                    "not an object",
                    42,
                    None,
                    {"type": "text", "text": "real block", "cache_control": {"type": "ephemeral"}},
                ],
            },
        ]
        assert _compute_frozen_message_count(messages) == 1

    def test_cache_control_null_value_is_still_present(self) -> None:
        """cache_control: null — key is present so the message IS frozen."""
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi", "cache_control": None}],
            },
        ]
        # Key present (even if null) → marker found → floor = 1
        assert _compute_frozen_message_count(messages) == 1


# ---------------------------------------------------------------------------
# Parity micro-tests (Python helper vs Rust compute_frozen_count fixtures)
# ---------------------------------------------------------------------------


class TestParity:
    """Shared fixture parity.

    These fixtures are the table-driven cases from
    crates/headroom-core/tests/cache_control.rs.  Expected values are the
    integer literals asserted by the Rust tests.
    """

    def _run(self, messages: list[dict]) -> int:
        return _compute_frozen_message_count(messages)

    def test_parity_marker_at_3_yields_4(self) -> None:
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "fourth", "cache_control": {"type": "ephemeral"}},
                ],
            },
            {"role": "user", "content": "fifth"},
        ]
        assert self._run(messages) == 4  # Rust: assert_eq!(compute_frozen_count(&body), 4)

    def test_parity_system_markers_dont_bump(self) -> None:
        """System-level cache_control never bumps the message-index floor."""
        # system markers: note we only pass messages here, system is ignored
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        assert self._run(messages) == 0  # Rust: assert_eq!(compute_frozen_count(&body), 0)

    def test_parity_tools_markers_dont_bump(self) -> None:
        """Tool-level cache_control never bumps the message-index floor."""
        messages = [
            {"role": "user", "content": "what is 2+2?"},
        ]
        assert self._run(messages) == 0  # Rust: assert_eq!(compute_frozen_count(&body), 0)

    def test_parity_ttl_1h_and_5m_both_bump(self) -> None:
        """Both 1h and 5m TTL markers count; highest index wins."""
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "first 1h",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "second 5m",
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
            },
        ]
        assert self._run(messages) == 2  # Rust: assert_eq!(compute_frozen_count(&body), 2)

    def test_parity_no_markers_zero(self) -> None:
        messages = [
            {"role": "user", "content": "no marker here"},
            {"role": "assistant", "content": [{"type": "text", "text": "no marker either"}]},
        ]
        assert self._run(messages) == 0  # Rust: assert_eq!(compute_frozen_count(&body), 0)

    def test_parity_multiple_non_adjacent_markers(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "m0", "cache_control": {"type": "ephemeral"}},
                ],
            },
            {"role": "assistant", "content": "m1 string"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "m2", "cache_control": {"type": "ephemeral"}},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "m3"}]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "m4",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    },
                ],
            },
            {"role": "assistant", "content": "m5 string"},
        ]
        assert self._run(messages) == 5  # Rust: assert_eq!(compute_frozen_count(&body), 5)

    def test_parity_multi_block_one_message(self) -> None:
        messages = [
            {"role": "user", "content": "first"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "block A",
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": "block B",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    },
                    {"type": "text", "text": "block C", "cache_control": {"type": "ephemeral"}},
                ],
            },
        ]
        assert self._run(messages) == 2  # Rust: assert_eq!(compute_frozen_count(&body), 2)

    def test_parity_string_then_block_with_marker(self) -> None:
        messages = [
            {"role": "user", "content": "plain string"},
            {"role": "assistant", "content": "another plain string"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "now with marker",
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
            },
        ]
        assert self._run(messages) == 3  # Rust: assert_eq!(compute_frozen_count(&body), 3)

    def test_parity_missing_messages_field_yields_zero(self) -> None:
        # Python helper takes messages list directly; empty list = same
        assert self._run([]) == 0  # Rust: assert_eq!(compute_frozen_count(&body), 0)

    def test_parity_non_object_blocks_skipped(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    "not an object",
                    42,
                    None,
                    {"type": "text", "text": "real block", "cache_control": {"type": "ephemeral"}},
                ],
            },
        ]
        assert self._run(messages) == 1  # Rust: assert_eq!(compute_frozen_count(&body), 1)


# ---------------------------------------------------------------------------
# Integration tests — compress() wires frozen_message_count correctly
# ---------------------------------------------------------------------------


class TestCompressFrozenPrefixWiring:
    """Test that compress() passes the correct frozen_message_count kwarg to pipeline.apply."""

    def _make_messages_with_cache_control_at(self, k: int, total: int) -> list[dict]:
        """Build a list of `total` messages where message index k has cache_control."""
        msgs: list[dict[str, Any]] = []
        for i in range(total):
            if i == k:
                msgs.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "system instructions (cached)",
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                )
            else:
                msgs.append(
                    {
                        "role": "user" if i % 2 == 0 else "assistant",
                        "content": _big_json_content(),
                    }
                )
        return msgs

    def test_pipeline_receives_correct_frozen_message_count(self) -> None:
        """compress() must pass frozen_message_count=k+1 when marker is at index k.

        RED before fix: pipeline.apply is called WITHOUT frozen_message_count
        kwarg (or with 0), causing the assertion to fail.
        GREEN after fix: kwarg is present and correct.
        """
        k = 1
        total = 4
        messages = self._make_messages_with_cache_control_at(k, total)

        captured_kwargs: dict[str, Any] = {}

        original_apply = None

        def spy_apply(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            return original_apply(**kwargs)

        from headroom.compress import _get_pipeline

        pipeline = _get_pipeline()
        original_apply = pipeline.apply

        with patch.object(pipeline, "apply", side_effect=spy_apply):
            compress(messages, model="gpt-4o", min_tokens_to_compress=1)

        assert "frozen_message_count" in captured_kwargs, (
            f"compress() must pass frozen_message_count to pipeline.apply; "
            f"kwargs seen: {list(captured_kwargs.keys())}"
        )
        assert captured_kwargs["frozen_message_count"] == k + 1, (
            f"expected frozen_message_count={k + 1}, got "
            f"{captured_kwargs['frozen_message_count']}"
        )

    def test_pipeline_receives_zero_when_no_cache_control(self) -> None:
        """When no messages have cache_control, frozen_message_count must be 0."""
        messages = [
            {"role": "user", "content": _big_json_content()},
            {"role": "assistant", "content": "some response"},
            {"role": "user", "content": _big_json_content()},
        ]

        captured_kwargs: dict[str, Any] = {}

        from headroom.compress import _get_pipeline

        pipeline = _get_pipeline()
        original_apply = pipeline.apply

        def spy_apply(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            return original_apply(**kwargs)

        with patch.object(pipeline, "apply", side_effect=spy_apply):
            compress(messages, model="gpt-4o", min_tokens_to_compress=1)

        # When no cache_control anywhere, frozen_message_count should be 0
        # (absent or explicitly 0 both satisfy this)
        frozen = captured_kwargs.get("frozen_message_count", 0)
        assert frozen == 0, f"expected frozen_message_count=0, got {frozen}"


class TestCompressFrozenPrefixByteIdentity:
    """End-to-end: messages in the frozen prefix are returned byte-identical.

    Contract #2: never drop msg index 0 / reorder cached prefix / rewrite cache_control.

    RED before fix: message 0 contains compressible JSON but NO cache_control.
    With frozen=0 (broken), ContentRouter compresses it and the content changes.
    GREEN after fix: frozen=k+1 so message 0 stays byte-identical.
    """

    def test_frozen_prefix_messages_unchanged(self) -> None:
        """Messages before and including the cache_control marker are byte-identical.

        Self-validating: also asserts that msg2 (AFTER marker) WAS compressed,
        so the test can only pass when real compression ran AND the prefix
        survived — eliminating the exception-fallback and inflation-guard
        false-green paths.
        """
        # Message 0: large compressible JSON (NO cache_control — this is the
        # key test: it sits *before* the marker, so the frozen-prefix count
        # must include it even though it has no cache_control itself).
        msg0 = {"role": "user", "content": _big_json_content()}

        # Message 1: cache_control marker at index k=1
        msg1 = {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "You are a helpful assistant. (cached prefix)",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }

        # Message 2: compressible JSON AFTER the marker (compression should proceed)
        msg2 = {
            "role": "user",
            "content": _big_json_content(),
        }

        messages = [msg0, msg1, msg2]
        msg0_snapshot = copy.deepcopy(msg0)
        msg1_snapshot = copy.deepcopy(msg1)
        msg2_snapshot = copy.deepcopy(msg2)

        result = compress(
            messages,
            model="gpt-4o",
            min_tokens_to_compress=1,
            compress_user_messages=True,
        )

        out = result.messages
        # No messages dropped AND none injected: the 3 input messages map to
        # exactly 3 output messages. `>= 3` would pass a path that silently
        # appended a message; pin the exact count (input was [msg0, msg1, msg2]).
        assert len(out) == len(messages) == 3, "exactly the 3 input messages, none dropped or added"

        # Self-validation: compression actually ran (msg2 was compressed).
        # If this fails, the test proves nothing about the frozen prefix.
        assert out[2] != msg2_snapshot or result.tokens_saved > 0, (
            "msg2 should have been compressed (post-marker message) — "
            "compression must have actually run for this test to be meaningful"
        )

        # Index 0 must be byte-identical
        assert out[0] == msg0_snapshot, (
            "Message 0 (compressible but in frozen prefix) must be byte-identical. "
            f"Original content length: {len(msg0_snapshot['content'])}, "
            f"result content: {out[0].get('content', '')[:80]!r}"
        )

        # Index 1 must be byte-identical (carries the cache_control marker)
        assert out[1] == msg1_snapshot, (
            "Message 1 (cache_control marker) must be byte-identical."
        )

        # cache_control key must still be present on the marked block
        result_block = out[1]["content"][0]
        assert "cache_control" in result_block, (
            "cache_control must not be stripped from the frozen prefix block"
        )

    def test_no_cache_control_allows_normal_compression(self) -> None:
        """When there are no cache_control markers, compression proceeds normally."""
        big_content = _big_json_content()
        messages = [
            {"role": "user", "content": big_content},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": big_content},
        ]
        result = compress(
            messages,
            model="gpt-4o",
            min_tokens_to_compress=1,
            compress_user_messages=True,
        )
        # Compression should have run (tokens saved > 0).
        assert len(result.messages) == 3
        assert result.tokens_saved > 0, (
            "Without cache_control, compression must proceed normally (tokens_saved > 0)"
        )

    def test_cache_control_bytes_preserved_exactly(self) -> None:
        """The cache_control value itself must be byte-identical after compress()."""
        cache_ctrl_value = {"type": "ephemeral", "ttl": "1h", "extra_field": "must_survive"}
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "cached system prompt",
                        "cache_control": cache_ctrl_value,
                    }
                ],
            },
            {
                "role": "assistant",
                "content": _big_json_content(),
            },
        ]
        result = compress(
            messages,
            model="gpt-4o",
            min_tokens_to_compress=1,
            compress_user_messages=True,
        )
        out_block = result.messages[0]["content"][0]
        assert out_block["cache_control"] == cache_ctrl_value, (
            "cache_control dict must be preserved byte-for-byte"
        )

    def test_msg_index_0_never_dropped(self) -> None:
        """Message at index 0 must always be present in the output."""
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "The very first message must survive.",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
            {"role": "assistant", "content": _big_json_content()},
            {"role": "user", "content": _big_json_content()},
        ]
        result = compress(messages, model="gpt-4o", min_tokens_to_compress=1)
        assert len(result.messages) >= 1
        assert result.messages[0]["role"] == "user"
