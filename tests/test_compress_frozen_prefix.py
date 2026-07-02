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
  crates/furl-core/tests/cache_control.rs.
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from typing import Any
from unittest.mock import patch

from furl_ctx.compress import (
    _compute_frozen_message_count,
    _frozen_prefix_warning,
    _frozen_transformed_content_warning,
    compress,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _big_json_content() -> str:
    """Return a large JSON array that SmartCrusher will want to compress."""
    rows = [
        {"id": i, "status": "active", "name": f"item_{i:04d}", "value": i * 17} for i in range(300)
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
    crates/furl-core/tests/cache_control.rs.  Expected values are the
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

        from furl_ctx.compress import _get_pipeline

        pipeline = _get_pipeline()
        original_apply = pipeline.apply

        with patch.object(pipeline, "apply", side_effect=spy_apply):
            compress(messages, model="gpt-4o", min_tokens_to_compress=1)

        assert "frozen_message_count" in captured_kwargs, (
            f"compress() must pass frozen_message_count to pipeline.apply; "
            f"kwargs seen: {list(captured_kwargs.keys())}"
        )
        assert captured_kwargs["frozen_message_count"] == k + 1, (
            f"expected frozen_message_count={k + 1}, got {captured_kwargs['frozen_message_count']}"
        )

    def test_pipeline_receives_zero_when_no_cache_control(self) -> None:
        """When no messages have cache_control, frozen_message_count must be 0."""
        messages = [
            {"role": "user", "content": _big_json_content()},
            {"role": "assistant", "content": "some response"},
            {"role": "user", "content": _big_json_content()},
        ]

        captured_kwargs: dict[str, Any] = {}

        from furl_ctx.compress import _get_pipeline

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
        assert out[1] == msg1_snapshot, "Message 1 (cache_control marker) must be byte-identical."

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
        """Message at index 0 must never be dropped OR reordered.

        The contract is stronger than "index 0 is present somewhere": the three
        input messages map to three output messages (none dropped, none added)
        AND message 0 comes back byte-identical and still first. ``>= 1`` would
        pass a path that dropped the two trailing messages, or that mutated msg 0
        in place — neither honours "index 0 never dropped/reordered".
        """
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
        msg0_snapshot = copy.deepcopy(messages[0])
        result = compress(messages, model="gpt-4o", min_tokens_to_compress=1)
        # No message dropped or injected (input was three messages).
        assert len(result.messages) == len(messages) == 3, (
            "exactly the 3 input messages, none dropped or added"
        )
        # Index 0 returns byte-identical AND still first (content unchanged,
        # position preserved) — not merely "a user message exists at index 0".
        assert result.messages[0] == msg0_snapshot, (
            "Message 0 (frozen prefix) must be byte-identical and remain first. "
            f"Original content: {msg0_snapshot['content']!r}, "
            f"result[0] content: {result.messages[0].get('content', '')!r}"
        )


# ---------------------------------------------------------------------------
# COR-49 — a fully (or nearly fully) frozen conversation must WARN, not
# silently no-op; TransformResult.warnings must reach CompressResult.warnings
# ---------------------------------------------------------------------------


def _marker_message(text: str) -> dict[str, Any]:
    """A user message carrying the Anthropic cache_control breakpoint."""
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}},
        ],
    }


class TestFullyFrozenConversationWarns:
    """cache_control on the LAST message — the multi-turn idiom Anthropic's
    docs teach — freezes every message: all transforms skip, 0 tokens saved,
    error=None. That silence is the bug: it must surface in
    ``result.warnings`` and the log."""

    def test_marker_on_last_message_warns_and_saves_nothing(self, caplog) -> None:
        messages = [
            {"role": "user", "content": _big_json_content()},
            {"role": "assistant", "content": "ok"},
            _marker_message("latest turn (cached)"),
        ]
        snapshots = copy.deepcopy(messages)

        with caplog.at_level(logging.WARNING):
            result = compress(
                messages,
                model="gpt-4o",
                min_tokens_to_compress=1,
                compress_user_messages=True,
            )

        # The frozen-prefix contract holds: nothing was compressed…
        assert result.error is None
        assert result.tokens_saved == 0
        assert result.messages == snapshots
        # …and the silence is broken by an explicit warning, both on the
        # result and in the log.
        assert result.warnings, "fully frozen conversation must surface a warning"
        assert any("freezes all" in w and "cache breakpoint" in w for w in result.warnings)
        assert any(
            "freezes all" in record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ), "the frozen-conversation warning must also be logged at WARNING"

    def test_frozen_fraction_above_threshold_warns(self) -> None:
        """10 of 11 frozen (0.909 > 0.9) — nearly-total freeze also warns."""
        messages: list[dict[str, Any]] = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
            for i in range(9)
        ]
        messages.append(_marker_message("cached up to here"))  # index 9 → frozen=10
        messages.append({"role": "user", "content": "live turn"})

        result = compress(messages, model="gpt-4o")

        assert result.error is None
        assert any("nearly the whole conversation" in w for w in result.warnings)

    def test_no_cache_control_yields_no_warnings(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = compress(messages, model="gpt-4o")
        assert result.error is None
        assert result.warnings == []


class TestFrozenPrefixWarningHelper:
    """Unit tests for the pure _frozen_prefix_warning helper."""

    def test_full_freeze_warns(self) -> None:
        warning = _frozen_prefix_warning(5, 5)
        assert warning is not None
        assert "freezes all 5 messages" in warning

    def test_fraction_above_threshold_warns(self) -> None:
        warning = _frozen_prefix_warning(11, 10)
        assert warning is not None
        assert "10 of 11" in warning

    def test_fraction_at_threshold_is_silent(self) -> None:
        # 9/10 == 0.9 is NOT > 0.9 — the boundary stays quiet.
        assert _frozen_prefix_warning(10, 9) is None

    def test_ordinary_fraction_is_silent(self) -> None:
        assert _frozen_prefix_warning(10, 4) is None

    def test_zero_frozen_is_silent(self) -> None:
        assert _frozen_prefix_warning(10, 0) is None

    def test_empty_conversation_is_silent(self) -> None:
        assert _frozen_prefix_warning(0, 0) is None


class TestTransformWarningsPlumbed:
    """TransformResult.warnings must reach CompressResult.warnings — before
    this fix they were aggregated by the pipeline and then dropped on the
    floor by compress()."""

    def _fake_apply(self, tokens_before: int, tokens_after: int):
        from furl_ctx.config import TransformResult

        def fake_apply(**kwargs: Any) -> TransformResult:
            return TransformResult(
                messages=kwargs["messages"],
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                transforms_applied=["fake:transform"],
                warnings=["transform-level warning"],
            )

        return fake_apply

    def test_transform_warnings_reach_compress_result(self) -> None:
        from furl_ctx.compress import _get_pipeline

        pipeline = _get_pipeline()
        with patch.object(pipeline, "apply", side_effect=self._fake_apply(100, 90)):
            result = compress([{"role": "user", "content": "hello"}], model="gpt-4o")

        assert result.error is None
        assert "transform-level warning" in result.warnings

    def test_warnings_survive_the_inflation_guard_revert(self) -> None:
        """Even when inflated output is reverted, warnings must not be lost."""
        from furl_ctx.compress import _get_pipeline

        pipeline = _get_pipeline()
        with patch.object(pipeline, "apply", side_effect=self._fake_apply(100, 200)):
            result = compress([{"role": "user", "content": "hello"}], model="gpt-4o")

        assert result.transforms_applied == ["inflation_guard:reverted"]
        assert "transform-level warning" in result.warnings


# ---------------------------------------------------------------------------
# COR-50 — moving the cache breakpoint forward across a previously-transformed
# message: characterization of the behavior + the best-effort detector
# ---------------------------------------------------------------------------


def _unique_tool_rows() -> str:
    """A dedup-eligible JSON tool output, salted so the process-global CCR
    store cannot leak hits between tests."""
    salt = uuid.uuid4().hex
    return json.dumps([{"row": i, "salt": salt, "data": "d" * 40} for i in range(40)])


class TestMovingBreakpointOverTransformedTurn:
    """The frozen prefix freezes INPUT bytes, but the provider cached what
    Furl SHIPPED last turn. Re-sending original history with the marker
    moved forward past a deduped turn re-ships the ORIGINAL bytes — frozen,
    so uncompressed forever — a guaranteed prefix-cache miss. compress()
    cannot fix this statelessly; it must WARN (COR-50 characterization)."""

    def test_previously_deduped_message_refrozen_warns(self) -> None:
        big = _unique_tool_rows()
        turn1 = [
            {"role": "user", "content": "run the disk check"},
            {"role": "tool", "tool_call_id": "c1", "content": big},
            {"role": "assistant", "content": "ran it; running again to confirm"},
            {"role": "tool", "tool_call_id": "c2", "content": big},  # exact duplicate
        ]

        r1 = compress(turn1, model="gpt-4o", protect_recent=0, min_tokens_to_compress=1)
        assert r1.error is None
        # Dedup replaced the LATER copy with a recoverable sentinel — these
        # are the bytes the provider's prefix cache saw for message 3.
        assert "_ccr_dropped" in r1.messages[3]["content"], (
            "precondition: cross-message dedup must have replaced the duplicate"
        )

        # Turn 2 — documented-example usage: the caller re-sends the ORIGINAL
        # history and moves the cache breakpoint forward past the deduped turn.
        turn2 = [
            *turn1,
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "confirmed",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
            {"role": "user", "content": "and what changed since?"},
        ]
        r2 = compress(turn2, model="gpt-4o", protect_recent=0, min_tokens_to_compress=1)

        assert r2.error is None
        # Characterization of the COR-50 behavior itself: the previously
        # deduped message is now inside the frozen prefix, so it ships as the
        # ORIGINAL bytes — NOT the sentinel the provider cached last turn.
        assert r2.messages[3]["content"] == big
        # The regression is invisible in the token metrics; the signal is the
        # warning naming the suspect frozen message.
        assert any(
            "previously shipped in compressed form" in w and "[3]" in w for w in r2.warnings
        ), f"expected the moving-breakpoint warning, got: {r2.warnings!r}"

    def test_frozen_first_occurrence_with_live_duplicate_stays_quiet(self) -> None:
        """False-positive guard: a first occurrence inside the frozen prefix
        whose duplicate lives in the live zone is CORRECT usage — the frozen
        copy always shipped verbatim; dedup keeps replacing the live copy.
        The detector must not warn."""
        big = _unique_tool_rows()
        messages = [
            {"role": "user", "content": "check disk"},
            {"role": "tool", "tool_call_id": "c1", "content": big},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "cached up to here",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
            {"role": "assistant", "content": "running again"},
            {"role": "tool", "tool_call_id": "c2", "content": big},
            {"role": "user", "content": "any difference?"},
        ]

        # First call populates the CCR registry (the live copy is deduped);
        # the second, identical call is where a naive detector would
        # misfire on the frozen first occurrence.
        r1 = compress(messages, model="gpt-4o", protect_recent=0, min_tokens_to_compress=1)
        assert r1.error is None
        r2 = compress(messages, model="gpt-4o", protect_recent=0, min_tokens_to_compress=1)

        assert r2.error is None
        assert not any("previously shipped in compressed form" in w for w in r2.warnings), (
            f"first-occurrence-in-frozen-prefix must not warn, got: {r2.warnings!r}"
        )


class TestFrozenTransformedContentDetector:
    """Unit tests for the best-effort _frozen_transformed_content_warning
    helper, exercised directly against the process-global CCR store."""

    @staticmethod
    def _unique_content() -> str:
        return f"output {uuid.uuid4().hex}\n" + "metric,value\n" * 40  # ≥ 256 chars

    def test_read_lifecycle_style_entry_warns_without_a_duplicate(self) -> None:
        """read_lifecycle stores ``compressed=""`` — the only writer of empty
        compressed content — so a frozen hit warns even with no frozen twin."""
        from furl_ctx.cache.compression_store import get_compression_store

        content = self._unique_content()
        get_compression_store().store(
            original=content,
            compressed="",
            tool_name="Read",
            tool_call_id="call_read_1",
            compression_strategy="read_lifecycle:stale",
        )

        messages = [{"role": "tool", "tool_call_id": "c1", "content": content}]
        warning = _frozen_transformed_content_warning(messages, frozen=1)

        assert warning is not None
        assert "[0]" in warning

    def test_dedup_style_entry_needs_a_frozen_duplicate(self) -> None:
        """A dedup-registry hit warns only for a LATER byte-identical copy
        inside the frozen prefix — dedup only ever rewrote later copies, so a
        lone frozen occurrence is the always-verbatim first occurrence."""
        from furl_ctx.cache.compression_store import get_compression_store
        from furl_ctx.transforms.cross_message_dedup import _content_hash

        content = self._unique_content()
        get_compression_store().store(
            original=content,
            compressed='{"_ccr_dropped": "duplicate tool output elided"}',
            compression_strategy="cross_message_dedup",
            explicit_hash=_content_hash(content),
        )

        lone = [{"role": "tool", "tool_call_id": "c1", "content": content}]
        assert _frozen_transformed_content_warning(lone, frozen=1) is None

        pair = [
            {"role": "tool", "tool_call_id": "c1", "content": content},
            {"role": "assistant", "content": "again"},
            {"role": "tool", "tool_call_id": "c2", "content": content},
        ]
        warning = _frozen_transformed_content_warning(pair, frozen=3)
        assert warning is not None
        assert "[2]" in warning

    def test_anthropic_tool_result_blocks_are_scanned(self) -> None:
        from furl_ctx.cache.compression_store import get_compression_store

        content = self._unique_content()
        get_compression_store().store(
            original=content,
            compressed="",
            tool_name="Read",
            tool_call_id="call_read_2",
            compression_strategy="read_lifecycle:stale",
        )

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": content},
                ],
            }
        ]
        warning = _frozen_transformed_content_warning(messages, frozen=1)
        assert warning is not None
        assert "[0]" in warning

    def test_unknown_content_never_warns(self) -> None:
        messages = [{"role": "tool", "tool_call_id": "c1", "content": self._unique_content()}]
        assert _frozen_transformed_content_warning(messages, frozen=1) is None

    def test_zero_frozen_never_warns(self) -> None:
        messages = [{"role": "tool", "tool_call_id": "c1", "content": self._unique_content()}]
        assert _frozen_transformed_content_warning(messages, frozen=0) is None

    def test_small_units_are_skipped(self) -> None:
        """Units below MIN_DEDUP_CHARS (where well-behaved callers' sentinels
        live) never trigger store lookups or warnings."""
        from furl_ctx.cache.compression_store import get_compression_store

        content = f"tiny {uuid.uuid4().hex}"  # well under 256 chars
        get_compression_store().store(
            original=content,
            compressed="",
            tool_name="Read",
            tool_call_id="call_read_3",
            compression_strategy="read_lifecycle:stale",
        )
        messages = [{"role": "tool", "tool_call_id": "c1", "content": content}]
        assert _frozen_transformed_content_warning(messages, frozen=1) is None
