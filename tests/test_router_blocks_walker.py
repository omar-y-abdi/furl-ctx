"""Direct unit tests for ``ContentBlockWalker`` (``router_blocks.py``).

The walker owns the per-block cache-safety walk — the cache_control guard,
the role gate for text blocks, error-output protection, excluded-tool aging
with the retrieval-tool exemption, already-compressed pinning, and the
two-tier-cache dispositions. Until this file, none of those gates had a
direct test: the module was exercised only through ``ContentRouter.apply()``
integration suites. Mutation testing (see the PR that added this file)
showed six of the walker's behaviors were completely unpinned — the full
suite stayed green under mutations of the store-gate rejection path, both
size/recency boundaries, per-tool bias threading, the nested feedback
multiplier, and the untouched-message identity contract — while the outright
gate deletions were each held by only 1-4 incidental integration tests.

Every test here drives :meth:`ContentBlockWalker.process_content_blocks`
with explicit fake injected callables — no router instance, no
monkeypatching — so each pin names exactly one behavior and its red-proof
mutation reddens the matching test(s).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import pytest

from furl_ctx.cache.retrieval_feedback import NEUTRAL_HINTS, FeedbackHints
from furl_ctx.transforms.router_blocks import ContentBlockWalker
from furl_ctx.transforms.router_cache import Recompute, ServeCached, ServeOriginal
from furl_ctx.transforms.router_engine import RouterCompressionResult
from furl_ctx.transforms.router_policy import CompressionStrategy

# Long enough to clear every size floor used in these tests (min_chars=100).
LONG_TEXT = "x" * 300 + " row data " * 30

# Unstructured error dump: >=2 distinct indicator keywords, non-JSON shape.
ERROR_TEXT = (
    "Traceback (most recent call last):\n"
    '  File "job.py", line 10, in run\n'
    "ValueError: bad input state\n" + "surrounding context line\n" * 20
)

# Strict-grammar engine marker (24-hex + double-angle) — matches
# ``marker_grammar.DOUBLE_ANGLE_PATTERN`` via ``_looks_like_ccr_output``.
CCR_MARKER_TEXT = (
    "kept rows above; full original: "
    "<<ccr:0123456789abcdef01234567 42_rows_offloaded>> " + "padding " * 60
)


@dataclass
class _WalkerConfig:
    """Minimal stand-in for the ``MessagePolicyConfig`` fields the walker reads."""

    protect_error_outputs: bool = True
    error_protection_max_chars: int = 2000
    enable_retrieval_feedback: bool = False


@dataclass
class _Harness:
    """Fake injected callables with call recording.

    Defaults model the hot path: cache miss (``Recompute``), store gate
    accepts, compressor returns ``COMPRESSED`` under the LOG strategy.
    """

    config: _WalkerConfig = field(default_factory=_WalkerConfig)
    disposition: Any = field(default_factory=Recompute)
    store_accepts: bool = True
    hints: FeedbackHints = NEUTRAL_HINTS
    tool_bias: float = 1.0
    compress_calls: list[str] = field(default_factory=list)
    compress_biases: list[float] = field(default_factory=list)
    bias_calls: list[str] = field(default_factory=list)
    hint_calls: list[tuple[str, str]] = field(default_factory=list)

    COMPRESSED = "<<compressed-payload>>"

    def lookup_disposition(
        self, key: Any, context: str, min_ratio: float, route_counts: Any
    ) -> Any:
        return self.disposition

    def store_disposition(self, key: Any, result: Any, min_ratio: float, route_counts: Any) -> bool:
        return self.store_accepts

    def compress_fn(self, text: str, **kwargs: Any) -> RouterCompressionResult:
        self.compress_calls.append(text)
        self.compress_biases.append(kwargs["bias"])
        return RouterCompressionResult(
            compressed=self.COMPRESSED,
            original=text,
            strategy_used=CompressionStrategy.LOG,
        )

    def get_tool_bias(self, tool_name: str) -> float:
        self.bias_calls.append(tool_name)
        return self.tool_bias

    def get_feedback_hints(self, tool_name: str, content: str) -> FeedbackHints:
        self.hint_calls.append((tool_name, content))
        return self.hints

    def result_cache_key(self, text: str, bias: float) -> tuple[str, float]:
        return (text, bias)

    def walk(
        self,
        message: dict[str, Any],
        *,
        excluded_tool_ids: set[str] | None = None,
        tool_name_map: dict[str, str] | None = None,
        min_chars: int = 100,
        read_protection_window: int = 5,
        messages_from_end: int = 10,
        skip_user: bool = True,
        skip_system: bool = True,
        compress_assistant_text_blocks: bool = False,
    ) -> tuple[dict[str, Any], list[str], Counter[str]]:
        """Run the walker over ``message`` and return (result, transforms, counts)."""
        walker = ContentBlockWalker(self.config)  # type: ignore[arg-type]
        transforms: list[str] = []
        route_counts: Counter[str] = Counter()
        result = walker.process_content_blocks(
            message,
            message["content"],
            "test context",
            transforms,
            excluded_tool_ids or set(),
            tool_name_map=tool_name_map,
            route_counts=route_counts,
            compressed_details=None,
            min_ratio=0.9,
            read_protection_window=read_protection_window,
            messages_from_end=messages_from_end,
            compressor_timing=None,
            min_chars=min_chars,
            skip_user=skip_user,
            skip_system=skip_system,
            compress_assistant_text_blocks=compress_assistant_text_blocks,
            token_counter=None,
            lookup_disposition=self.lookup_disposition,
            store_disposition=self.store_disposition,
            compress_fn=self.compress_fn,
            get_tool_bias=self.get_tool_bias,
            get_feedback_hints=self.get_feedback_hints,
            result_cache_key=self.result_cache_key,
        )
        return result, transforms, route_counts


def _tool_result_message(content: Any, **block_extra: Any) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": "toolu_01", "content": content, **block_extra}
    return {"role": "user", "content": [block]}


# ---------------------------------------------------------------------------
# cache_control: the client's explicit cache breakpoint (contract clause 1).
# ---------------------------------------------------------------------------


class TestCacheControlGuard:
    def test_cache_control_block_ships_byte_identical(self) -> None:
        """A cache_control'd block is never modified — not even a compressible one."""
        h = _Harness()
        msg = _tool_result_message(LONG_TEXT, cache_control={"type": "ephemeral"})
        result, transforms, counts = h.walk(msg)
        assert result is msg  # untouched message returned by reference
        assert h.compress_calls == []
        assert counts["cache_control_protected"] == 1
        assert transforms == []

    def test_nested_part_with_cache_control_ships_untouched(self) -> None:
        """Inside a nested parts list, a cache_control'd text part is skipped
        while an eligible sibling part still compresses."""
        h = _Harness()
        pinned = {
            "type": "text",
            "text": LONG_TEXT + "-pinned",
            "cache_control": {"type": "ephemeral"},
        }
        free = {"type": "text", "text": LONG_TEXT + "-free"}
        msg = _tool_result_message([pinned, free])
        result, _, _ = h.walk(msg)
        new_parts = result["content"][0]["content"]
        assert new_parts[0] is pinned
        assert new_parts[1]["text"] == _Harness.COMPRESSED
        assert h.compress_calls == [LONG_TEXT + "-free"]


# ---------------------------------------------------------------------------
# Role gate for text blocks (contract clauses 2-3).
# ---------------------------------------------------------------------------


class TestTextBlockRoleGate:
    @staticmethod
    def _text_message(role: str) -> dict[str, Any]:
        return {"role": role, "content": [{"type": "text", "text": LONG_TEXT}]}

    def test_assistant_text_protected_by_default(self) -> None:
        """Assistant text is echoed into provider auto-prefix caches: default-skip."""
        h = _Harness()
        msg = self._text_message("assistant")
        result, _, _ = h.walk(msg)
        assert result is msg
        assert h.compress_calls == []

    def test_assistant_text_compresses_only_on_explicit_opt_in(self) -> None:
        h = _Harness()
        msg = self._text_message("assistant")
        result, transforms, _ = h.walk(msg, compress_assistant_text_blocks=True)
        assert result["content"][0]["text"] == _Harness.COMPRESSED
        assert "router:text_block:log" in transforms

    @pytest.mark.parametrize("role", ["user", "system", "developer"])
    def test_user_and_system_text_always_protected(self, role: str) -> None:
        h = _Harness()
        msg = self._text_message(role)
        result, _, _ = h.walk(msg)
        assert result is msg
        assert h.compress_calls == []

    def test_unknown_role_text_defaults_to_protected(self) -> None:
        h = _Harness()
        msg = self._text_message("some_future_role")
        result, _, _ = h.walk(msg)
        assert result is msg
        assert h.compress_calls == []

    def test_tool_role_text_block_compresses(self) -> None:
        """Tool-role text blocks are tool outputs — the compressible case that
        proves the gate above is role-based, not a blanket skip."""
        h = _Harness()
        msg = self._text_message("tool")
        result, _, _ = h.walk(msg)
        assert result["content"][0]["text"] == _Harness.COMPRESSED


# ---------------------------------------------------------------------------
# Excluded tools: recency aging and the retrieval-tool exemption (P0-5).
# ---------------------------------------------------------------------------


class TestExcludedToolAging:
    def test_recent_excluded_tool_is_protected(self) -> None:
        h = _Harness()
        msg = _tool_result_message(LONG_TEXT)
        result, transforms, counts = h.walk(
            msg,
            excluded_tool_ids={"toolu_01"},
            messages_from_end=3,
            read_protection_window=5,
        )
        assert result is msg
        assert transforms == ["router:excluded:tool"]
        assert counts["excluded_tool"] == 1
        assert h.compress_calls == []

    def test_old_excluded_tool_ages_out_and_compresses(self) -> None:
        """Past the read-protection window, a non-retrieval excluded tool's
        output falls through to compression."""
        h = _Harness()
        msg = _tool_result_message(LONG_TEXT)
        result, _, _ = h.walk(
            msg,
            excluded_tool_ids={"toolu_01"},
            tool_name_map={"toolu_01": "Read"},
            messages_from_end=10,
            read_protection_window=5,
        )
        assert result["content"][0]["content"] == _Harness.COMPRESSED

    def test_excluded_tool_protected_at_exact_window_edge(self) -> None:
        """``messages_from_end <= read_protection_window`` is inclusive: a
        message exactly AT the window edge is still protected."""
        h = _Harness()
        msg = _tool_result_message(LONG_TEXT)
        result, transforms, _ = h.walk(
            msg,
            excluded_tool_ids={"toolu_01"},
            tool_name_map={"toolu_01": "Read"},
            messages_from_end=5,
            read_protection_window=5,
        )
        assert result is msg
        assert transforms == ["router:excluded:tool"]
        assert h.compress_calls == []

    @pytest.mark.parametrize("tool_name", ["furl_retrieve", "mcp__furl__furl_retrieve"])
    def test_retrieval_tool_output_never_ages_out(self, tool_name: str) -> None:
        """The CCR retrieval tool's outputs ARE previously-compressed originals;
        re-compressing them would mint a compress -> retrieve -> compress loop.
        They stay protected at ANY distance from the end of the conversation."""
        h = _Harness()
        msg = _tool_result_message(LONG_TEXT)
        result, transforms, _ = h.walk(
            msg,
            excluded_tool_ids={"toolu_01"},
            tool_name_map={"toolu_01": tool_name},
            messages_from_end=100,
            read_protection_window=5,
        )
        assert result is msg
        assert transforms == ["router:excluded:tool"]
        assert h.compress_calls == []


# ---------------------------------------------------------------------------
# Error-output protection (string branch and nested branch).
# ---------------------------------------------------------------------------


class TestErrorProtection:
    def test_is_error_flagged_tool_result_ships_verbatim(self) -> None:
        h = _Harness()
        msg = _tool_result_message(LONG_TEXT, is_error=True)
        result, transforms, counts = h.walk(msg)
        assert result is msg
        assert transforms == ["router:protected:error_output"]
        assert counts["error_protected"] == 1
        assert h.compress_calls == []

    def test_unstructured_error_text_ships_verbatim_without_flag(self) -> None:
        h = _Harness()
        msg = _tool_result_message(ERROR_TEXT)
        result, transforms, _ = h.walk(msg)
        assert result is msg
        assert transforms == ["router:protected:error_output"]

    def test_error_protection_size_cap_falls_through_to_compression(self) -> None:
        """Above ``error_protection_max_chars`` even a flagged error compresses —
        LogCompressor preserves error lines in big logs."""
        h = _Harness(config=_WalkerConfig(error_protection_max_chars=100))
        msg = _tool_result_message(ERROR_TEXT, is_error=True)
        result, _, _ = h.walk(msg)
        assert result["content"][0]["content"] == _Harness.COMPRESSED

    def test_protect_error_outputs_off_compresses_errors(self) -> None:
        h = _Harness(config=_WalkerConfig(protect_error_outputs=False))
        msg = _tool_result_message(ERROR_TEXT, is_error=True)
        result, _, _ = h.walk(msg)
        assert result["content"][0]["content"] == _Harness.COMPRESSED

    def test_nested_is_error_protects_the_whole_block(self) -> None:
        """The block-level is_error flag must protect the nested parts shape
        exactly as it protects the flat string shape."""
        h = _Harness()
        parts = [{"type": "text", "text": LONG_TEXT}]
        msg = _tool_result_message(parts, is_error=True)
        result, transforms, counts = h.walk(msg)
        assert result is msg
        assert transforms == ["router:protected:error_output"]
        assert counts["error_protected"] == 1
        assert h.compress_calls == []

    def test_nested_part_error_indicator_scan_protects_that_part(self) -> None:
        h = _Harness()
        parts = [
            {"type": "text", "text": ERROR_TEXT},
            {"type": "text", "text": LONG_TEXT},
        ]
        msg = _tool_result_message(parts)
        result, transforms, _ = h.walk(msg)
        new_parts = result["content"][0]["content"]
        assert new_parts[0]["text"] == ERROR_TEXT
        assert new_parts[1]["text"] == _Harness.COMPRESSED
        assert "router:protected:error_output" in transforms


# ---------------------------------------------------------------------------
# Already-compressed pinning (strict CCR marker grammar).
# ---------------------------------------------------------------------------


class TestAlreadyCompressedPinning:
    def test_flat_tool_result_with_ccr_marker_is_pinned(self) -> None:
        h = _Harness()
        msg = _tool_result_message(CCR_MARKER_TEXT)
        result, _, counts = h.walk(msg)
        assert result is msg
        assert counts["already_compressed"] == 1
        assert h.compress_calls == []

    def test_nested_part_with_ccr_marker_is_pinned(self) -> None:
        """Re-compressing an engine-emitted sentinel would orphan its backing:
        sentinel survival through a second crush is not contractual."""
        h = _Harness()
        parts = [
            {"type": "text", "text": CCR_MARKER_TEXT},
            {"type": "text", "text": LONG_TEXT},
        ]
        msg = _tool_result_message(parts)
        result, _, counts = h.walk(msg)
        new_parts = result["content"][0]["content"]
        assert new_parts[0]["text"] == CCR_MARKER_TEXT
        assert new_parts[1]["text"] == _Harness.COMPRESSED
        assert counts["already_compressed"] == 1

    @pytest.mark.parametrize(
        "phrase",
        [
            "Retrieve more: hash=0123456789abcdef01234567",
            "Retrieve original: hash=0123456789abcdef01234567",
        ],
    )
    def test_text_block_retrieve_phrase_is_pinned(self, phrase: str) -> None:
        h = _Harness()
        msg = {"role": "tool", "content": [{"type": "text", "text": LONG_TEXT + " " + phrase}]}
        result, _, counts = h.walk(msg)
        assert result is msg
        assert counts["already_compressed"] == 1


# ---------------------------------------------------------------------------
# Two-tier-cache dispositions and the store gate.
# ---------------------------------------------------------------------------


class TestCacheDispositions:
    def test_serve_original_disposition_keeps_block(self) -> None:
        h = _Harness(disposition=ServeOriginal())
        msg = _tool_result_message(LONG_TEXT)
        result, transforms, _ = h.walk(msg)
        assert result is msg
        assert transforms == []
        assert h.compress_calls == []

    def test_serve_cached_swaps_content_without_recompressing(self) -> None:
        h = _Harness(disposition=ServeCached(compressed="cached-bytes", strategy="log", ratio=0.25))
        msg = _tool_result_message(LONG_TEXT)
        result, transforms, _ = h.walk(msg)
        assert result["content"][0]["content"] == "cached-bytes"
        assert transforms == ["router:tool_result:log"]
        assert h.compress_calls == []

    def test_store_gate_rejection_serves_original_block(self) -> None:
        """When the store gate refuses the fresh result (ratio too high), the
        ORIGINAL block ships and no transform string is booked."""
        h = _Harness(store_accepts=False)
        msg = _tool_result_message(LONG_TEXT)
        result, transforms, _ = h.walk(msg)
        assert result is msg
        assert transforms == []
        assert h.compress_calls == [LONG_TEXT]  # it did try

    def test_unexpected_disposition_raises(self) -> None:
        h = _Harness(disposition=object())
        msg = _tool_result_message(LONG_TEXT)
        with pytest.raises(RuntimeError, match="unexpected CacheDisposition"):
            h.walk(msg)


# ---------------------------------------------------------------------------
# Size floor and non-dict blocks.
# ---------------------------------------------------------------------------


class TestFloorsAndPassthrough:
    def test_small_tool_result_books_small_and_ships(self) -> None:
        h = _Harness()
        msg = _tool_result_message("tiny")
        result, _, counts = h.walk(msg)
        assert result is msg
        assert counts["small"] == 1
        assert h.compress_calls == []

    def test_content_exactly_at_min_chars_floor_is_small(self) -> None:
        """The floor is strict ``> min_chars``: content of EXACTLY min_chars
        books ``small`` and ships untouched."""
        h = _Harness()
        msg = _tool_result_message("y" * 100)
        result, _, counts = h.walk(msg, min_chars=100)
        assert result is msg
        assert counts["small"] == 1
        assert h.compress_calls == []

    def test_non_dict_block_passes_through(self) -> None:
        h = _Harness()
        msg = {"role": "tool", "content": ["bare string block", 42]}
        result, _, _ = h.walk(msg)
        assert result is msg

    def test_untouched_message_returned_by_reference(self) -> None:
        """No compression anywhere -> the original message object comes back
        (identity is what the engine's PERF-1 delta accounting keys on)."""
        h = _Harness(disposition=ServeOriginal())
        msg = _tool_result_message(LONG_TEXT)
        result, _, _ = h.walk(msg)
        assert result is msg


# ---------------------------------------------------------------------------
# Retrieval-feedback hints (Engine P2-13, opt-in).
# ---------------------------------------------------------------------------


class TestFeedbackHints:
    def test_flag_off_never_consults_hints(self) -> None:
        h = _Harness()
        msg = _tool_result_message(LONG_TEXT)
        h.walk(msg, tool_name_map={"toolu_01": "Bash"})
        assert h.hint_calls == []

    def test_skip_hint_serves_original(self) -> None:
        h = _Harness(
            config=_WalkerConfig(enable_retrieval_feedback=True),
            hints=FeedbackHints(skip_compression=True),
        )
        msg = _tool_result_message(LONG_TEXT)
        result, transforms, counts = h.walk(msg, tool_name_map={"toolu_01": "Bash"})
        assert result is msg
        assert transforms == ["router:feedback:skip"]
        assert counts["feedback_skip"] == 1
        assert h.compress_calls == []

    def test_keep_budget_multiplier_scales_bias_on_string_path(self) -> None:
        """The feedback keep-budget multiplier composes with the per-tool
        bias before compression (P2-13): compress must see the product."""
        h = _Harness(
            config=_WalkerConfig(enable_retrieval_feedback=True),
            hints=FeedbackHints(keep_budget_multiplier=2.0),
            tool_bias=0.5,
        )
        msg = _tool_result_message(LONG_TEXT)
        h.walk(msg, tool_name_map={"toolu_01": "Bash"})
        assert h.compress_biases == [pytest.approx(1.0)]  # 0.5 * 2.0

    def test_keep_budget_multiplier_scales_bias_on_nested_path(self) -> None:
        h = _Harness(
            config=_WalkerConfig(enable_retrieval_feedback=True),
            hints=FeedbackHints(keep_budget_multiplier=2.0),
            tool_bias=0.5,
        )
        msg = _tool_result_message([{"type": "text", "text": LONG_TEXT}])
        h.walk(msg, tool_name_map={"toolu_01": "Bash"})
        assert h.compress_biases == [pytest.approx(1.0)]  # 0.5 * 2.0


# ---------------------------------------------------------------------------
# Per-tool bias threading.
# ---------------------------------------------------------------------------


class TestToolBias:
    def test_tool_bias_reaches_the_compressor(self) -> None:
        """A named tool's bias must thread through to ``compress_fn`` — a
        walker that silently pins bias to 1.0 defeats per-tool profiles."""
        h = _Harness(tool_bias=0.25)
        msg = _tool_result_message(LONG_TEXT)
        h.walk(msg, tool_name_map={"toolu_01": "Bash"})
        assert h.bias_calls == ["Bash"]
        assert h.compress_biases == [pytest.approx(0.25)]

    def test_unnamed_tool_defaults_to_neutral_bias(self) -> None:
        h = _Harness(tool_bias=0.25)
        msg = _tool_result_message(LONG_TEXT)
        h.walk(msg)  # no tool_name_map: tool name resolves to ""
        assert h.bias_calls == []
        assert h.compress_biases == [pytest.approx(1.0)]
