"""Tests for the pluggable tokenizer system."""

from __future__ import annotations

import sys
import types

import pytest

from headroom.tokenizers import (
    BaseTokenizer,
    CharacterCounter,
    EstimatingTokenCounter,
    TiktokenCounter,
    TokenCounter,
    TokenizerRegistry,
    get_mistral_tokenizer,
    get_tokenizer,
    is_mistral_tokenizer_available,
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
        from headroom.tokenizers.tiktoken_counter import get_encoding_for_model

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
        """Test counting simple text."""
        counter = TiktokenCounter()
        count = counter.count_text("Hello, world!")
        assert count > 0
        assert count < 10  # Should be a few tokens

    def test_count_text_unicode(self):
        """Test counting text with unicode."""
        counter = TiktokenCounter()
        count = counter.count_text("Hello, 世界!")
        assert count > 0

    def test_count_messages_single(self):
        """Test counting single message."""
        counter = TiktokenCounter()
        messages = [{"role": "user", "content": "Hello!"}]
        count = counter.count_messages(messages)
        assert count > 0

    def test_count_messages_with_tool_calls(self):
        """Test counting messages with tool calls."""
        counter = TiktokenCounter()
        messages = [
            {"role": "user", "content": "Search for Python"},
            {
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
            },
            {
                "role": "tool",
                "tool_call_id": "call_123",
                "content": "Results...",
            },
        ]
        count = counter.count_messages(messages)
        assert count > 0

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
        """Test counting simple text."""
        counter = EstimatingTokenCounter()
        text = "Hello, world!"
        count = counter.count_text(text)
        assert count > 0
        # Rough estimate: 13 chars / 4 chars per token ≈ 3-4 tokens
        assert 2 <= count <= 6

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
        """Test counting messages."""
        counter = EstimatingTokenCounter()
        messages = [
            {"role": "user", "content": "Hello!"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        count = counter.count_messages(messages)
        assert count > 0

    def test_json_detection(self):
        """Test JSON content detection."""
        counter = EstimatingTokenCounter()
        json_text = '{"name": "test", "value": 123}'
        # Should use JSON ratio
        count = counter.count_text(json_text)
        assert count > 0

    def test_code_detection(self):
        """Test code content detection."""
        counter = EstimatingTokenCounter()
        code_text = """
def hello():
    return "Hello, world!"
"""
        count = counter.count_text(code_text)
        assert count > 0

    def test_repr(self):
        """Test string representation."""
        counter = EstimatingTokenCounter()
        assert "EstimatingTokenCounter" in repr(counter)


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
        """Test getting tokenizer for Anthropic model."""
        tokenizer = get_tokenizer("claude-3-sonnet")
        assert isinstance(tokenizer, EstimatingTokenCounter)

    def test_get_unknown_model_fallback(self):
        """Test fallback for unknown model."""
        tokenizer = get_tokenizer("unknown-model-xyz")
        assert isinstance(tokenizer, EstimatingTokenCounter)

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


def _fake_transformers_module(from_pretrained) -> types.ModuleType:
    """Build a stand-in `transformers` module for loader-failure tests.

    The real package is an optional dependency and must not be required
    to test the loader's failure/recovery behavior.
    """
    module = types.ModuleType("transformers")
    # Class bodies skip enclosing-function scope for names they also assign,
    # so bind the parameter to a differently-named local first.
    loader = from_pretrained

    class AutoTokenizer:
        from_pretrained = staticmethod(loader)

    module.AutoTokenizer = AutoTokenizer
    return module


class _FakeHFTokenizer:
    """Minimal tokenizer double: one token per whitespace-separated word."""

    def encode(self, text: str, add_special_tokens: bool = False) -> list[int]:
        return [0] * len(text.split())


class TestHuggingFaceLoadFailureRecovery:
    """A transient tokenizer-load failure must not poison later loads.

    Regression: three stacked permanent negative caches (lru_cache on the
    None from a load exception, the instance `_tokenizer = False` latch,
    and the registry counter cache) meant one transient network failure
    degraded counting to estimation for the whole process lifetime.
    """

    @pytest.fixture(autouse=True)
    def _isolated_loader_state(self):
        """Reset module-level loader caches around each test."""
        import headroom.tokenizers.huggingface as hf

        hf._load_tokenizer_cached.cache_clear()
        yield
        hf._load_tokenizer_cached.cache_clear()

    def test_transient_failure_does_not_poison_later_success(self, monkeypatch):
        """First load fails, second succeeds: the failure must not be latched."""
        import headroom.tokenizers.huggingface as hf

        calls = {"n": 0}

        def flaky_from_pretrained(name: str, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated transient network failure")
            return _FakeHFTokenizer()

        monkeypatch.setitem(
            sys.modules, "transformers", _fake_transformers_module(flaky_from_pretrained)
        )
        monkeypatch.setattr(hf, "_failed_loads", {})

        counter = hf.HuggingFaceTokenizer("llama-3-8b")

        # First access fails -> estimation fallback, no exception. Within
        # the TTL the failure is negative-cached, so repeated accesses do
        # not hammer the loader.
        assert counter.tokenizer is None
        assert counter._use_fallback() is True
        assert calls["n"] == 1

        # Simulate the TTL elapsing.
        hf._failed_loads.clear()

        # The next access retries and succeeds -- neither the instance nor
        # the module-level cache may have latched the earlier failure.
        assert counter.tokenizer is not None
        assert calls["n"] == 2
        assert counter._use_fallback() is False
        assert counter.count_text("hello world") == 2

        # The success is cached: further loads do not hit the loader again.
        assert hf.HuggingFaceTokenizer("llama-3-8b").tokenizer is not None
        assert calls["n"] == 2

    def test_failure_negative_cached_within_ttl(self, monkeypatch):
        """Within the TTL a known-failed load is not re-attempted (no hammering)."""
        import headroom.tokenizers.huggingface as hf

        calls = {"n": 0}

        def always_failing_from_pretrained(name: str, **kwargs):
            calls["n"] += 1
            raise OSError("simulated persistent failure")

        monkeypatch.setitem(
            sys.modules,
            "transformers",
            _fake_transformers_module(always_failing_from_pretrained),
        )
        monkeypatch.setattr(hf, "_failed_loads", {})

        assert hf._load_tokenizer("some/flaky-tokenizer") is None
        assert hf._load_tokenizer("some/flaky-tokenizer") is None
        assert calls["n"] == 1  # second lookup served from the negative cache


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


class TestMistralTokenizer:
    """Tests for Mistral tokenizer using official mistral-common."""

    def test_is_available(self):
        """Test availability check."""
        result = is_mistral_tokenizer_available()
        assert isinstance(result, bool)

    @pytest.mark.skipif(
        not is_mistral_tokenizer_available(),
        reason="mistral-common not installed",
    )
    def test_get_mistral_tokenizer_class(self):
        """Test getting MistralTokenizer class."""
        MistralTokenizer = get_mistral_tokenizer()
        assert MistralTokenizer is not None
        assert hasattr(MistralTokenizer, "count_text")

    @pytest.mark.skipif(
        not is_mistral_tokenizer_available(),
        reason="mistral-common not installed",
    )
    def test_init_default_model(self):
        """Test initialization with default model."""
        MistralTokenizer = get_mistral_tokenizer()
        counter = MistralTokenizer()
        assert counter.model == "mistral-large"
        assert counter.version == "v3"

    @pytest.mark.skipif(
        not is_mistral_tokenizer_available(),
        reason="mistral-common not installed",
    )
    def test_init_mixtral_model(self):
        """Test initialization with Mixtral model (uses v1)."""
        MistralTokenizer = get_mistral_tokenizer()
        counter = MistralTokenizer("mixtral-8x7b")
        assert counter.version == "v1"

    @pytest.mark.skipif(
        not is_mistral_tokenizer_available(),
        reason="mistral-common not installed",
    )
    def test_count_text_empty(self):
        """Test counting empty text."""
        MistralTokenizer = get_mistral_tokenizer()
        counter = MistralTokenizer()
        assert counter.count_text("") == 0

    @pytest.mark.skipif(
        not is_mistral_tokenizer_available(),
        reason="mistral-common not installed",
    )
    def test_count_text_simple(self):
        """Test counting simple text."""
        MistralTokenizer = get_mistral_tokenizer()
        counter = MistralTokenizer()
        count = counter.count_text("Hello, world!")
        assert count > 0
        assert count < 10

    @pytest.mark.skipif(
        not is_mistral_tokenizer_available(),
        reason="mistral-common not installed",
    )
    def test_count_text_unicode(self):
        """Test counting text with unicode."""
        MistralTokenizer = get_mistral_tokenizer()
        counter = MistralTokenizer()
        count = counter.count_text("Hello, 世界!")
        assert count > 0

    @pytest.mark.skipif(
        not is_mistral_tokenizer_available(),
        reason="mistral-common not installed",
    )
    def test_count_messages(self):
        """Test counting messages."""
        MistralTokenizer = get_mistral_tokenizer()
        counter = MistralTokenizer()
        messages = [
            {"role": "user", "content": "Hello!"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        count = counter.count_messages(messages)
        assert count > 0

    @pytest.mark.skipif(
        not is_mistral_tokenizer_available(),
        reason="mistral-common not installed",
    )
    def test_count_messages_with_system(self):
        """Test counting messages with system prompt."""
        MistralTokenizer = get_mistral_tokenizer()
        counter = MistralTokenizer()
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ]
        count = counter.count_messages(messages)
        assert count > 0

    @pytest.mark.skipif(
        not is_mistral_tokenizer_available(),
        reason="mistral-common not installed",
    )
    def test_encode_decode_roundtrip(self):
        """Test encode/decode roundtrip."""
        MistralTokenizer = get_mistral_tokenizer()
        counter = MistralTokenizer()
        text = "Hello, world!"
        tokens = counter.encode(text)
        decoded = counter.decode(tokens)
        assert decoded == text

    @pytest.mark.skipif(
        not is_mistral_tokenizer_available(),
        reason="mistral-common not installed",
    )
    def test_implements_protocol(self):
        """Test MistralTokenizer implements TokenCounter protocol."""
        MistralTokenizer = get_mistral_tokenizer()
        counter = MistralTokenizer()
        assert isinstance(counter, TokenCounter)

    @pytest.mark.skipif(
        not is_mistral_tokenizer_available(),
        reason="mistral-common not installed",
    )
    def test_repr(self):
        """Test string representation."""
        MistralTokenizer = get_mistral_tokenizer()
        counter = MistralTokenizer("mistral-large")
        assert "MistralTokenizer" in repr(counter)
        assert "mistral-large" in repr(counter)

    @pytest.mark.skipif(
        not is_mistral_tokenizer_available(),
        reason="mistral-common not installed",
    )
    def test_registry_returns_mistral_for_mistral_models(self):
        """Test registry returns Mistral tokenizer for Mistral models."""
        tokenizer = get_tokenizer("mistral-large")
        MistralTokenizer = get_mistral_tokenizer()
        assert isinstance(tokenizer, MistralTokenizer)

    @pytest.mark.skipif(
        not is_mistral_tokenizer_available(),
        reason="mistral-common not installed",
    )
    def test_registry_returns_mistral_for_mixtral(self):
        """Test registry returns Mistral tokenizer for Mixtral models."""
        tokenizer = get_tokenizer("mixtral-8x7b")
        MistralTokenizer = get_mistral_tokenizer()
        assert isinstance(tokenizer, MistralTokenizer)

    @pytest.mark.skipif(
        not is_mistral_tokenizer_available(),
        reason="mistral-common not installed",
    )
    def test_registry_returns_mistral_for_codestral(self):
        """Test registry returns Mistral tokenizer for Codestral models."""
        tokenizer = get_tokenizer("codestral")
        MistralTokenizer = get_mistral_tokenizer()
        assert isinstance(tokenizer, MistralTokenizer)
