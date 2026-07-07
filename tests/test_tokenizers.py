"""Tests for the pluggable tokenizer system."""

from __future__ import annotations

from furl_ctx.tokenizers import (
    BaseTokenizer,
    CharacterCounter,
    EstimatingTokenCounter,
    TiktokenCounter,
    TokenCounter,
    TokenizerRegistry,
    get_tokenizer,
    list_supported_models,
    register_tokenizer,
)


class TestTiktokenCounter:
    """Tests for TiktokenCounter."""

    def test_init_default_model(self):
        """Test initialization with default model."""
        counter = TiktokenCounter()
        assert counter.model == "gpt-4o"
        assert counter.encoding_name == "o200k_base"

    def test_init_gpt4_model(self):
        """Test initialization with GPT-4."""
        counter = TiktokenCounter("gpt-4")
        assert counter.model == "gpt-4"
        assert counter.encoding_name == "cl100k_base"

    def test_unknown_gpt4_snapshot_uses_cl100k(self):
        """Unknown gpt-4 (non-o, non-turbo) snapshots must use cl100k_base.

        Regression: the prefix matcher scanned MODEL_TO_ENCODING for the
        first key starting with the prefix. For prefix "gpt-4" that matched
        the "gpt-4o" entry first and wrongly returned o200k_base for any
        gpt-4 snapshot not in the table (e.g. a future dated build).
        """
        from furl_ctx.tokenizers.tiktoken_counter import get_encoding_for_model

        assert get_encoding_for_model("gpt-4-2025-01-01") == "cl100k_base"
        assert get_encoding_for_model("gpt-4-future") == "cl100k_base"
        # gpt-4o snapshots still resolve to o200k_base (most-specific first).
        assert get_encoding_for_model("gpt-4o-2099-12-31") == "o200k_base"
        # gpt-4-turbo snapshots use cl100k_base.
        assert get_encoding_for_model("gpt-4-turbo-2099") == "cl100k_base"

    def test_title_case_model_resolves_to_o200k(self):
        """Model-name case must not change the encoding: "GPT-4o" == "gpt-4o".

        Regression: encoding lookup was case-sensitive, so "GPT-4o" silently
        fell through to the cl100k_base default instead of o200k_base.
        """
        assert TiktokenCounter("GPT-4o").encoding_name == "o200k_base"
        assert TiktokenCounter("GPT-4O-MINI").encoding_name == "o200k_base"
        assert TiktokenCounter("GPT-4").encoding_name == "cl100k_base"

        # The registry path resolves identically.
        TokenizerRegistry.clear_cache()
        tokenizer = get_tokenizer("GPT-4o")
        assert isinstance(tokenizer, TiktokenCounter)
        assert tokenizer.encoding_name == "o200k_base"

    def test_count_text_empty(self):
        """Test counting empty text."""
        counter = TiktokenCounter()
        assert counter.count_text("") == 0

    def test_count_text_simple(self):
        """Simple text counts exactly (o200k_base pin, TEST-11)."""
        counter = TiktokenCounter()
        assert counter.count_text("Hello, world!") == 4

    def test_count_text_unicode(self):
        """Unicode text counts exactly (o200k_base pin, TEST-11)."""
        counter = TiktokenCounter()
        assert counter.count_text("Hello, 世界!") == 4

    def test_count_messages_single(self):
        """A single message is its text plus the documented overheads.

        TEST-11: pins the composition (text tokens + MESSAGE_OVERHEAD +
        REPLY_OVERHEAD) instead of `count > 0`, so an overhead regression
        or a double-count fails loudly.
        """
        counter = TiktokenCounter()
        messages = [{"role": "user", "content": "Hello!"}]
        count = counter.count_messages(messages)
        expected_floor = (
            counter.count_text("Hello!")
            + BaseTokenizer.MESSAGE_OVERHEAD
            + BaseTokenizer.REPLY_OVERHEAD
        )
        # Role tokens may add a small constant on top of the floor; the
        # exact total for THIS message under o200k_base is pinned.
        assert count >= expected_floor
        assert count == 9

    def test_count_messages_with_tool_calls(self):
        """tool_calls contribute tokens (counterfactual pin, TEST-11)."""
        counter = TiktokenCounter()
        tool_call_msg = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": '{"query": "Python"}',
                    },
                }
            ],
        }
        messages = [
            {"role": "user", "content": "Search for Python"},
            tool_call_msg,
            {
                "role": "tool",
                "tool_call_id": "call_123",
                "content": "Results...",
            },
        ]
        without_tool_calls = [
            messages[0],
            {"role": "assistant"},
            messages[2],
        ]

        count = counter.count_messages(messages)
        # Counterfactual: dropping the tool_calls block must lower the count —
        # proves the block's name/arguments/id are actually being counted
        # (`count > 0` passed even when tool_calls were ignored entirely).
        assert count > counter.count_messages(without_tool_calls)
        assert count == 36  # o200k_base pin for THESE messages

    def test_anthropic_base64_image_part_uses_fixed_image_cost(self):
        """A base64 image part costs the fixed image estimate, not text tokens.

        Regression: count_messages stringified part types it didn't
        special-case, so a ~200KB Anthropic base64 image part was counted
        as ~100K text tokens (60x the base handler's fixed cost of 1600),
        inflating routing and context-pressure decisions.
        """
        counter = TiktokenCounter()
        text_part = {"type": "text", "text": "What is in this image?"}
        image_part = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "iVBORw0KGgoAAAANSUhEUg" * 10_000,  # ~220KB of base64
            },
        }

        with_image = counter.count_messages([{"role": "user", "content": [text_part, image_part]}])
        text_only = counter.count_messages([{"role": "user", "content": [text_part]}])

        # The image contributes exactly the base handler's fixed image cost.
        assert with_image - text_only == 1600

    def test_tool_result_part_counts_content_not_repr(self):
        """A tool_result part counts its content, not the dict's repr."""
        counter = TiktokenCounter()
        result_text = "The weather in Paris is sunny with a high of 24C."
        tool_result_part = {
            "type": "tool_result",
            "tool_use_id": "toolu_01",
            "content": result_text,
        }

        with_part = counter.count_messages([{"role": "user", "content": [tool_result_part]}])
        empty = counter.count_messages([{"role": "user", "content": []}])

        assert with_part - empty == counter.count_text(result_text)

    def test_encode_decode_roundtrip(self):
        """Test encode/decode roundtrip."""
        counter = TiktokenCounter()
        text = "Hello, world!"
        tokens = counter.encode(text)
        decoded = counter.decode(tokens)
        assert decoded == text

    def test_repr(self):
        """Test string representation."""
        counter = TiktokenCounter("gpt-4o")
        assert "TiktokenCounter" in repr(counter)
        assert "gpt-4o" in repr(counter)


class TestEstimatingTokenCounter:
    """Tests for EstimatingTokenCounter."""

    def test_init_default(self):
        """Test initialization with defaults."""
        counter = EstimatingTokenCounter()
        assert counter._fixed_ratio is None

    def test_init_fixed_ratio(self):
        """Test initialization with fixed ratio."""
        counter = EstimatingTokenCounter(chars_per_token=3.5)
        assert counter._fixed_ratio == 3.5

    def test_count_text_empty(self):
        """Test counting empty text."""
        counter = EstimatingTokenCounter()
        assert counter.count_text("") == 0

    def test_count_text_simple(self):
        """Simple text uses the default 4.0 ratio: 13 chars → 3 (TEST-11 pin)."""
        counter = EstimatingTokenCounter()
        text = "Hello, world!"
        # max(1, int(13 / 4.0 + 0.5)) == 3 — the shared estimation formula.
        assert counter.count_text(text) == 3

    def test_count_text_fixed_ratio(self):
        """Test counting with fixed ratio."""
        counter = EstimatingTokenCounter(chars_per_token=5.0)
        text = "x" * 50  # 50 chars
        count = counter.count_text(text)
        assert count == 10  # 50 / 5 = 10

    def test_count_text_minimum_one(self):
        """Test minimum of 1 token."""
        counter = EstimatingTokenCounter()
        assert counter.count_text("x") >= 1

    def test_count_messages(self):
        """Messages count text + per-message and reply overheads (TEST-11).

        The composition floor (content tokens + 2×MESSAGE_OVERHEAD +
        REPLY_OVERHEAD) must hold, and the exact total for THESE messages
        is pinned — `count > 0` accepted any behavior at all.
        """
        counter = EstimatingTokenCounter()
        messages = [
            {"role": "user", "content": "Hello!"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        count = counter.count_messages(messages)
        floor = (
            counter.count_text("Hello!")
            + counter.count_text("Hi there!")
            + 2 * BaseTokenizer.MESSAGE_OVERHEAD
            + BaseTokenizer.REPLY_OVERHEAD
        )
        assert count >= floor
        assert count == 18

    def test_json_detection(self):
        """JSON content is detected and counted at the JSON ratio (TEST-11).

        The old assert (`count > 0`) held even if detection never fired;
        pin the detected ratio class AND the resulting count.
        """
        counter = EstimatingTokenCounter()
        json_text = '{"name": "test", "value": 123}'
        assert counter._detect_ratio(json_text) == counter.CHARS_PER_TOKEN_JSON
        # 30 chars at the 3.2 JSON ratio → max(1, int(30/3.2 + 0.5)) == 9.
        assert counter.count_text(json_text) == 9

    def test_code_detection(self):
        """Code content is detected and counted at the code ratio (TEST-11)."""
        counter = EstimatingTokenCounter()
        code_text = """
def hello():
    return "Hello, world!"
"""
        assert counter._detect_ratio(code_text) == counter.CHARS_PER_TOKEN_CODE
        # 42 chars at the 3.5 code ratio → max(1, int(42/3.5 + 0.5)) == 12.
        assert counter.count_text(code_text) == 12

    def test_repr(self):
        """Test string representation."""
        counter = EstimatingTokenCounter()
        assert "EstimatingTokenCounter" in repr(counter)


class TestEstimatorPrefixSampling:
    """PERF-14: auto-mode detection runs on a 4KB prefix sample.

    ``count_text`` previously ``json.loads``-parsed multi-MB valid-JSON
    strings and ran two full-text regex scans (URL + UUID) PER CALL just to
    pick 3.2 vs 4.0 chars/token. Detection — including
    ``_count_special_overhead``, not just ``_detect_ratio`` — now operates
    on a ``_DETECTION_SAMPLE_CHARS`` prefix; texts at or under the sample
    size keep the exact historical behavior.
    """

    def _huge_json(self) -> str:
        import json as _json

        rows = [
            {"id": i, "path": f"/repo/src/module_{i}.py", "status": "ok", "score": i * 0.5}
            for i in range(20_000)
        ]
        return _json.dumps(rows)

    def test_huge_valid_json_keeps_the_json_ratio_class(self):
        """Same ratio class as the historical full parse — via the prefix."""
        from furl_ctx.tokenizers.estimator import _DETECTION_SAMPLE_CHARS

        counter = EstimatingTokenCounter()
        huge = self._huge_json()
        assert len(huge) > 100 * _DETECTION_SAMPLE_CHARS

        assert counter._detect_ratio(huge) == counter.CHARS_PER_TOKEN_JSON

    def test_huge_json_never_pays_a_full_parse(self, monkeypatch):
        """The 'fast' half of the pin: counting a multi-MB JSON string must
        not call json.loads AT ALL (a truncated prefix can never parse, and
        the full parse was the cost being removed)."""
        import json as _json

        calls = {"n": 0}
        real_loads = _json.loads

        def spy_loads(*args, **kwargs):
            calls["n"] += 1
            return real_loads(*args, **kwargs)

        monkeypatch.setattr(_json, "loads", spy_loads)
        counter = EstimatingTokenCounter()
        huge = self._huge_json()

        count = counter.count_text(huge)

        # Sanity floor: the JSON ratio (3.2 chars/token) means the count must
        # exceed a chars/4 floor — a stub returning a token or two would pass
        # the old `count > 0`.
        assert count >= len(huge) // 4
        assert calls["n"] == 0

    def test_huge_numeric_json_array_keeps_the_json_ratio_class(self):
        counter = EstimatingTokenCounter()
        huge = "[" + ", ".join(str(1_000_000_000 + i) for i in range(50_000)) + "]"

        assert counter._detect_ratio(huge) == counter.CHARS_PER_TOKEN_JSON

    def test_huge_bracketed_log_is_not_misread_as_json(self):
        """'[INFO] ...' starts with the JSON head byte but is prose-dense,
        not structural-dense — stays at the default ratio."""
        counter = EstimatingTokenCounter()
        huge = "\n".join(
            f"[INFO] worker {i} finished the assigned batch without any retries"
            for i in range(10_000)
        )

        assert counter._detect_ratio(huge) == counter.CHARS_PER_TOKEN

    def test_small_texts_keep_exact_historical_behavior(self):
        counter = EstimatingTokenCounter()
        assert counter._detect_ratio('{"name": "test", "value": 123}') == (
            counter.CHARS_PER_TOKEN_JSON
        )
        assert counter._detect_ratio("[not json at all") == counter.CHARS_PER_TOKEN
        assert (
            counter._detect_ratio("def f():\n    pass\ndef g():\n    pass\n")
            == counter.CHARS_PER_TOKEN_CODE
        )

    def test_special_overhead_scans_only_the_prefix_sample(self):
        from furl_ctx.tokenizers.estimator import _DETECTION_SAMPLE_CHARS

        counter = EstimatingTokenCounter()
        uuid = "0f8fad5b-d9cb-469f-a165-70867728950e"
        inside = uuid + " " + "x" * (2 * _DETECTION_SAMPLE_CHARS)
        outside = "x" * (2 * _DETECTION_SAMPLE_CHARS) + " " + uuid

        assert counter._count_special_overhead(inside) == 2
        assert counter._count_special_overhead(outside) == 0

    def test_huge_json_counts_fast(self):
        """Loose wall-clock sanity bound (was O(full parse + 2 full regex
        scans) per call; a generous ceiling keeps this stable on CI)."""
        import time

        counter = EstimatingTokenCounter()
        huge = self._huge_json()

        t0 = time.perf_counter()
        for _ in range(20):
            counter.count_text(huge)
        elapsed = time.perf_counter() - t0

        assert elapsed < 1.0, f"20 estimator counts on {len(huge)} chars took {elapsed:.2f}s"


class TestCharacterCounter:
    """Tests for CharacterCounter."""

    def test_init_default(self):
        """Test initialization with default ratio."""
        counter = CharacterCounter()
        assert counter.chars_per_token == 4.0

    def test_init_custom_ratio(self):
        """Test initialization with custom ratio."""
        counter = CharacterCounter(chars_per_token=3.5)
        assert counter.chars_per_token == 3.5

    def test_count_text(self):
        """Test counting text."""
        counter = CharacterCounter(chars_per_token=4.0)
        text = "x" * 40  # 40 chars
        count = counter.count_text(text)
        assert count == 10  # 40 / 4 = 10

    def test_count_text_empty(self):
        """Test counting empty text."""
        counter = CharacterCounter()
        assert counter.count_text("") == 0


class TestTokenizerRegistry:
    """Tests for TokenizerRegistry."""

    def test_get_openai_model(self):
        """Test getting tokenizer for OpenAI model."""
        tokenizer = get_tokenizer("gpt-4o")
        assert isinstance(tokenizer, TiktokenCounter)

    def test_get_anthropic_model(self):
        """claude-* models use TiktokenCounter with o200k_base (Q1)."""
        tokenizer = get_tokenizer("claude-3-sonnet")
        assert isinstance(tokenizer, TiktokenCounter)
        assert tokenizer.encoding_name == "o200k_base"

    def test_claude_tiktoken_count_differs_from_3pt5_estimate(self):
        """o200k_base CJK count != 3.5-cpt estimate, proving real tokenizer (Q1)."""
        # CJK: est@3.5=9, o200k_base=24 — a 2.7x difference ensures we'd
        # never accidentally pass this with the old EstimatingTokenCounter.
        TokenizerRegistry.clear_cache()
        cjk = "东京タワーは高いです。北京烤鸭很好吃。한국어 텍스트도 있다."
        tokenizer = get_tokenizer("claude-sonnet-4-6")
        assert tokenizer.count_text(cjk) == 24  # o200k_base, not est@3.5 (9)

    def test_claude_tiktoken_importerror_fallback(self, monkeypatch):
        """ImportError in tiktoken falls back to EstimatingTokenCounter(3.5) (Q1).

        The _create_anthropic factory does ``from .tiktoken_counter import
        TiktokenCounter`` inside a try/except ImportError. We verify the fallback
        by hiding furl_ctx.tokenizers.tiktoken_counter from sys.modules so the
        dynamic import raises ImportError.
        """
        import sys

        import furl_ctx.tokenizers.registry as reg

        # Remove tiktoken_counter from sys.modules so the dynamic import inside
        # _create_anthropic raises ImportError. We restore it in the finally block.
        tc_module = sys.modules.pop("furl_ctx.tokenizers.tiktoken_counter", None)
        try:
            # Also block the top-level tiktoken so TiktokenCounter cannot re-import.
            monkeypatch.setitem(sys.modules, "tiktoken", None)
            TokenizerRegistry.clear_cache()
            tokenizer = reg.get_tokenizer("claude-3-sonnet")
            assert isinstance(tokenizer, EstimatingTokenCounter)
        finally:
            if tc_module is not None:
                sys.modules["furl_ctx.tokenizers.tiktoken_counter"] = tc_module
            TokenizerRegistry.clear_cache()

    def test_get_unknown_model_fallback(self):
        """Test fallback for unknown model."""
        tokenizer = get_tokenizer("unknown-model-xyz")
        assert isinstance(tokenizer, EstimatingTokenCounter)

    def test_removed_backend_models_fall_back_to_estimation(self):
        """Llama/Mistral-family names resolve via the estimation fallback.

        The HuggingFace and Mistral tokenizer backends were removed
        (tiktoken-only). Their model names must keep resolving to a working
        EstimatingTokenCounter -- the same result their missing-dependency
        fallback produced before the removal -- and must never error.
        """
        for model in ("llama-3", "meta-llama/Llama-3.1-8B", "mistral-large", "mixtral-8x7b"):
            tokenizer = get_tokenizer(model)
            assert isinstance(tokenizer, EstimatingTokenCounter)
            assert tokenizer.count_text("hello world") >= 1

        # Totality: these names route to the estimation backend directly,
        # so resolution succeeds even with fallback=False.
        strict = get_tokenizer("llama-3", fallback=False)
        assert isinstance(strict, EstimatingTokenCounter)

    def test_get_with_specific_backend(self):
        """Test forcing specific backend."""
        tokenizer = get_tokenizer("any-model", backend="estimation")
        assert isinstance(tokenizer, EstimatingTokenCounter)

    def test_register_custom_tokenizer(self):
        """Test registering custom tokenizer."""
        custom = EstimatingTokenCounter(chars_per_token=3.0)
        register_tokenizer("my-custom-model", tokenizer=custom)
        retrieved = get_tokenizer("my-custom-model")
        assert retrieved is custom

    def test_list_supported_models(self):
        """Test listing supported models."""
        models = list_supported_models()
        assert isinstance(models, dict)
        assert "gpt-4o" in str(models) or "^gpt-4o" in str(models)

    def test_clear_cache(self):
        """Test clearing tokenizer cache."""
        # Get a tokenizer to populate cache
        get_tokenizer("gpt-4o")
        # Clear cache
        TokenizerRegistry.clear_cache()
        # Should still work after clearing
        tokenizer = get_tokenizer("gpt-4o")
        assert tokenizer is not None

    def test_creation_failure_fallback_not_cached(self):
        """A failed creation must not pin the model to estimation forever.

        Regression: the fallback EstimatingTokenCounter was written into the
        registry cache, so a single transient creation failure degraded that
        model to estimation for the rest of the process lifetime.
        """
        calls = {"n": 0}

        def flaky_factory(model: str) -> EstimatingTokenCounter:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated transient failure")
            return EstimatingTokenCounter(chars_per_token=2.0)

        TokenizerRegistry.register_backend("flaky-test-backend", flaky_factory)
        TokenizerRegistry.clear_cache()

        first = TokenizerRegistry.get("flaky-model", backend="flaky-test-backend")
        assert isinstance(first, EstimatingTokenCounter)
        assert first._fixed_ratio is None  # the generic fallback estimator

        second = TokenizerRegistry.get("flaky-model", backend="flaky-test-backend")
        assert calls["n"] == 2  # creation was retried, not served from cache
        assert isinstance(second, EstimatingTokenCounter)
        assert second._fixed_ratio == 2.0  # the real (retried) tokenizer


class TestTokenCounterProtocol:
    """Tests for TokenCounter protocol."""

    def test_tiktoken_implements_protocol(self):
        """Test TiktokenCounter implements protocol."""
        counter = TiktokenCounter()
        assert isinstance(counter, TokenCounter)

    def test_estimating_implements_protocol(self):
        """Test EstimatingTokenCounter implements protocol."""
        counter = EstimatingTokenCounter()
        assert isinstance(counter, TokenCounter)

    def test_character_implements_protocol(self):
        """Test CharacterCounter implements protocol."""
        counter = CharacterCounter()
        assert isinstance(counter, TokenCounter)


class TestBaseTokenizer:
    """Tests for BaseTokenizer base class."""

    def test_message_overhead_constant(self):
        """Test message overhead constant."""
        assert BaseTokenizer.MESSAGE_OVERHEAD == 4

    def test_reply_overhead_constant(self):
        """Test reply overhead constant."""
        assert BaseTokenizer.REPLY_OVERHEAD == 3
