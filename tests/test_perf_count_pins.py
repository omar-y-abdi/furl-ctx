"""PERF-1 / PERF-2 call-count pins.

PERF-1 — one request used to pay up to EIGHT full-conversation token counts
and TWO deep copies (measured 2026-07-03 on the dedup-replacement path with
the aligner enabled: pipeline entry+exit = 2, aligner = 2, dedup = 2, router
= 2; copies: pipeline entry + aligner). The fix pins:

* CacheAligner.apply: ONE count, ``tokens_after == tokens_before``, and the
  result list IS the input list (the isolation copy moved to the public
  ``align_for_cache`` wrapper).
* CrossMessageDeduper.apply: ONE full-conversation count at entry; the
  after-count derives from per-replaced-message deltas.
* End-to-end pipeline: SIX full-conversation counts, ONE deep copy.
  (The router's own before/after pair is deliberately untouched — the
  §4.1 decomposition just landed and router count-reuse is a separate,
  larger change.)

PERF-2 — content detection (a Rust FFI round-trip) used to run TWICE per
compressed message (once in the Pass-1 gate chain, again inside
``compress()``) and ran even for messages about to be pinned. The fix pins:

* pinned (CCR-marked) message → ZERO detect calls (pin gate hoisted above
  detection);
* compressible message with both code protections inert → ONE detect call
  (classify skips; the engine detects once);
* compressible message inside the recent-code window → ONE detect call
  (classify detects for the protection gate; the engine reuses it via the
  precomputed-detection seam, PERF-2c).
"""

from __future__ import annotations

import json
from typing import Any

from furl_ctx.config import CacheAlignerConfig, FurlConfig
from furl_ctx.tokenizer import Tokenizer
from furl_ctx.transforms import cache_aligner as cache_aligner_module
from furl_ctx.transforms import content_router as content_router_module
from furl_ctx.transforms import pipeline as pipeline_module
from furl_ctx.transforms.cache_aligner import CacheAligner, align_for_cache
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig
from furl_ctx.transforms.cross_message_dedup import CrossMessageDeduper
from furl_ctx.transforms.pipeline import TransformPipeline

# ---------------------------------------------------------------------------
# Counting stub tokenizer (TokenCounter protocol).
# ---------------------------------------------------------------------------


class SpyCounter:
    """Deterministic 4-chars-per-token counter that counts its own calls."""

    def __init__(self) -> None:
        self.count_text_calls = 0
        self.count_message_calls = 0
        self.count_messages_calls = 0

    def _msg_tokens(self, message: dict[str, Any]) -> int:
        content = message.get("content") or ""
        if isinstance(content, list):
            content = json.dumps(content)
        return 4 + max(1, len(str(content)) // 4)

    def count_text(self, text: str) -> int:
        self.count_text_calls += 1
        return max(1, len(text) // 4)

    def count_message(self, message: dict[str, Any]) -> int:
        self.count_message_calls += 1
        return self._msg_tokens(message)

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        self.count_messages_calls += 1
        return sum(self._msg_tokens(m) for m in messages) + 3


class SpyProvider:
    def __init__(self, counter: SpyCounter) -> None:
        self._counter = counter

    def get_token_counter(self, model: str) -> SpyCounter:
        return self._counter


def _spy_tokenizer() -> tuple[Tokenizer, SpyCounter]:
    counter = SpyCounter()
    return Tokenizer(counter, "spy"), counter  # type: ignore[arg-type]


_BIG_TOOL_OUTPUT = json.dumps(
    [
        {
            "id": i,
            "path": f"/repo/src/module_{i}.py",
            "status": "ok" if i % 3 else "changed",
            "detail": f"line detail payload number {i} with some repeated text body",
        }
        for i in range(80)
    ]
)


def _dedup_conversation() -> list[dict[str, Any]]:
    """A conversation whose second big tool output is an exact duplicate."""
    return [
        {"role": "system", "content": "You are a code assistant. Session 2024-01-15T10:00:00Z"},
        {"role": "user", "content": "please list the files and summarize the changes for me"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "run_query", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": _BIG_TOOL_OUTPUT},
        {"role": "assistant", "content": "Re-checking the file list now."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c2", "function": {"name": "run_query", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c2", "content": _BIG_TOOL_OUTPUT},
        {"role": "user", "content": "and now show only the changed ones please thanks"},
    ]


# ---------------------------------------------------------------------------
# PERF-1: aligner counts once, returns the input list.
# ---------------------------------------------------------------------------


class TestCacheAlignerSingleCount:
    def test_counts_once_and_returns_input_list(self) -> None:
        tokenizer, counter = _spy_tokenizer()
        messages = [
            {"role": "system", "content": "Prompt with a stamp 2024-01-15T10:00:00Z inside"},
            {"role": "user", "content": "hello"},
        ]
        aligner = CacheAligner(CacheAlignerConfig(enabled=True))

        result = aligner.apply(messages, tokenizer)

        # The aligner never mutates: after == before, no second recount.
        assert result.tokens_after == result.tokens_before
        assert counter.count_messages_calls == 1
        # No isolation copy in apply() — the result IS the input list,
        # byte-identical.
        assert result.messages is messages
        assert result.messages == messages

    def test_align_for_cache_wrapper_keeps_the_public_copy(self) -> None:
        messages = [
            {"role": "system", "content": "Prompt content"},
            {"role": "user", "content": "hello"},
        ]

        aligned, stable_hash = align_for_cache(messages)

        # Direct public callers still get an isolated list they can mutate.
        assert aligned is not messages
        assert aligned == messages
        assert aligned[0] is not messages[0]
        assert stable_hash


# ---------------------------------------------------------------------------
# PERF-1: dedup counts the full conversation once, at entry.
# ---------------------------------------------------------------------------


class TestDedupSingleFullCount:
    def test_no_replacement_path_counts_once(self) -> None:
        tokenizer, counter = _spy_tokenizer()
        messages = [
            {"role": "user", "content": "look at this"},
            {"role": "tool", "content": "unique tool output " * 30},
            {"role": "tool", "content": "another distinct output " * 30},
        ]

        result = CrossMessageDeduper().apply(messages, tokenizer)

        assert result.transforms_applied == []
        assert counter.count_messages_calls == 1
        assert counter.count_message_calls == 0
        assert result.tokens_after == result.tokens_before

    def test_replacement_path_counts_once_plus_per_message_deltas(self) -> None:
        tokenizer, counter = _spy_tokenizer()
        messages = _dedup_conversation()

        result = CrossMessageDeduper().apply(messages, tokenizer)

        assert result.transforms_applied == ["cross_message_dedup:exact:1"]
        # ONE full-conversation count (entry); the second full recount is
        # replaced by exactly 2 single-message counts (the one replaced
        # message, before + after).
        assert counter.count_messages_calls == 1
        assert counter.count_message_calls == 2
        # The delta-derived after-count is EXACT: identical to what a full
        # recount of the result would report.
        fresh = SpyCounter()
        assert result.tokens_after == fresh.count_messages(result.messages)
        assert result.tokens_before == fresh.count_messages(messages)


# ---------------------------------------------------------------------------
# PERF-1: end-to-end pipeline pin — 6 full counts, 1 deep copy.
# ---------------------------------------------------------------------------


class TestPipelineFullConversationCounts:
    def test_pipeline_pays_six_full_counts_and_one_deep_copy(self, monkeypatch) -> None:
        """Was 8 counts + 2 copies before PERF-1 (measured); now 6 + 1:
        pipeline entry/exit (2) + aligner (1) + dedup (1) + router
        before/after (2, deliberately untouched — see module docstring)."""
        counter = SpyCounter()
        deep_copies = {"n": 0}
        import copy
        real_deep_copy = copy.deepcopy

        def counting_deep_copy(messages, memo=None):
            deep_copies["n"] += 1
            return real_deep_copy(messages, memo)

        monkeypatch.setattr(copy, "deepcopy", counting_deep_copy)

        pipeline = TransformPipeline(
            config=FurlConfig(cache_aligner=CacheAlignerConfig(enabled=True)),
            provider=SpyProvider(counter),
        )

        result = pipeline.apply(_dedup_conversation(), model="spy", model_limit=128000)

        assert "cross_message_dedup:exact:1" in result.transforms_applied
        assert counter.count_messages_calls == 6
        assert deep_copies["n"] == 1


# ---------------------------------------------------------------------------
# PERF-2: detection-call pins on the router's string path.
# ---------------------------------------------------------------------------


def _counting_detect(monkeypatch) -> dict[str, int]:
    """Wrap the module-global ``_detect_content`` with a call counter."""
    calls = {"n": 0}
    real_detect = content_router_module._detect_content

    def spy(content: str):
        calls["n"] += 1
        return real_detect(content)

    monkeypatch.setattr(content_router_module, "_detect_content", spy)
    return calls


class TestDetectionCallPins:
    def _apply_single_tool_message(
        self,
        message_content: str,
        *,
        pad_messages: int,
        config: ContentRouterConfig | None = None,
    ):
        """Run apply() with the target tool message FIRST and ``pad_messages``
        small user messages after it (pushing it outside the recent-code
        window when pad_messages >= protect_recent_code)."""
        tokenizer, _ = _spy_tokenizer()
        messages: list[dict[str, Any]] = [
            {"role": "assistant", "content": "seed message so index 0 is never the target"},
            {"role": "tool", "tool_call_id": "t1", "content": message_content},
        ]
        for i in range(pad_messages):
            messages.append({"role": "user", "content": f"short follow-up number {i}"})
        router = ContentRouter(config or ContentRouterConfig())
        return router.apply(messages, tokenizer)

    def test_compressible_outside_window_detects_once(self, monkeypatch) -> None:
        """Both code protections inert for this message (outside the
        recent-code window, no analysis intent) → classify skips detection
        entirely; the engine detects once inside compress(). Was 2."""
        calls = _counting_detect(monkeypatch)

        result = self._apply_single_tool_message(_BIG_TOOL_OUTPUT, pad_messages=6)

        assert any(t.startswith("router:smart_crusher") for t in result.transforms_applied)
        assert calls["n"] == 1

    def test_compressible_inside_window_detects_once(self, monkeypatch) -> None:
        """Inside the recent-code window the protection gate needs the
        detection — classify pays it ONCE and threads it into compress()
        (PERF-2c), so the engine never re-detects. Was 2."""
        calls = _counting_detect(monkeypatch)

        result = self._apply_single_tool_message(_BIG_TOOL_OUTPUT, pad_messages=0)

        assert any(t.startswith("router:smart_crusher") for t in result.transforms_applied)
        assert calls["n"] == 1

    def test_pinned_message_detects_zero_times(self, monkeypatch) -> None:
        """A message carrying a real engine-emitted CCR marker is pinned
        BEFORE detection (PERF-2a) — the detect round-trip is never paid.
        Was 1 (inside the window it was paid before the pin gate)."""
        calls = _counting_detect(monkeypatch)
        pinned_content = (
            "row data elided for brevity "
            + "<<ccr:0123456789abcdef01234567 4096_bytes>> "
            + "(Retrieve original: hash=0123456789abcdef01234567) "
            + "padding words to clear the size floor " * 20
        )

        result = self._apply_single_tool_message(pinned_content, pad_messages=0)

        assert result.messages[1]["content"] == pinned_content
        assert calls["n"] == 0

    def test_direct_compress_without_precomputed_detection_detects_once(self, monkeypatch) -> None:
        """Direct ``compress()`` callers (no threading) keep the historical
        single engine-side detection."""
        calls = _counting_detect(monkeypatch)
        router = ContentRouter(ContentRouterConfig())

        router.compress(_BIG_TOOL_OUTPUT)

        assert calls["n"] == 1
