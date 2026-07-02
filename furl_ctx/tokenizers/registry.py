"""Tokenizer registry for universal model support.

Provides automatic tokenizer selection based on model name with
support for multiple backends and custom tokenizers.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from .base import TokenCounter
from .estimator import EstimatingTokenCounter

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# Model pattern matching for tokenizer selection
# Order matters - more specific patterns first
# Models that match no pattern here fall back to the "estimation" backend
# (EstimatingTokenCounter). This includes Llama/Mistral/Qwen and other open
# models: their HuggingFace/Mistral tokenizer backends were removed
# (tiktoken-only), and estimation is exactly what their missing-dependency
# fallback produced before the removal.
MODEL_PATTERNS: list[tuple[str, str]] = [
    # OpenAI models -> tiktoken
    (r"^gpt-4o", "tiktoken"),
    (r"^gpt-4", "tiktoken"),
    (r"^gpt-3\.5", "tiktoken"),
    (r"^o1", "tiktoken"),
    (r"^o3", "tiktoken"),
    (r"^text-embedding", "tiktoken"),
    (r"^text-davinci", "tiktoken"),
    (r"^code-", "tiktoken"),
    (r"^davinci", "tiktoken"),
    (r"^curie", "tiktoken"),
    (r"^babbage", "tiktoken"),
    (r"^ada", "tiktoken"),
    # Anthropic models -> estimation (Claude uses custom tokenizer)
    (r"^claude-", "anthropic"),
    # Google models -> estimation (Gemini uses SentencePiece)
    (r"^gemini", "google"),
    (r"^palm", "google"),
    # Cohere models -> estimation
    (r"^command", "cohere"),
]


class TokenizerRegistry:
    """Registry for tokenizer instances and factories.

    Supports:
    - Automatic tokenizer selection based on model name
    - Custom tokenizer registration
    - Multiple backends (tiktoken, estimation)
    - Lazy loading of tokenizer dependencies

    Example:
        # Auto-detect tokenizer
        tokenizer = TokenizerRegistry.get("gpt-4o")

        # Register custom tokenizer
        TokenizerRegistry.register("my-model", my_tokenizer)

        # Use specific backend
        tokenizer = TokenizerRegistry.get("gpt-4", backend="tiktoken")
    """

    # Singleton registry instance
    _instance: TokenizerRegistry | None = None

    # Registered tokenizers (model -> tokenizer instance)
    _tokenizers: dict[str, TokenCounter] = {}

    # Registered factories (backend -> factory function)
    _factories: dict[str, Callable[[str], TokenCounter]] = {}

    # Cache for auto-detected tokenizers
    _cache: dict[str, TokenCounter] = {}

    def __new__(cls) -> TokenizerRegistry:
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_factories()
        return cls._instance

    def _init_factories(self) -> None:
        """Initialize default tokenizer factories."""
        self._factories = {
            "tiktoken": self._create_tiktoken,
            "anthropic": self._create_anthropic,
            "google": self._create_google,
            "cohere": self._create_cohere,
            "estimation": self._create_estimation,
        }

    @classmethod
    def get(
        cls,
        model: str,
        backend: str | None = None,
        fallback: bool = True,
    ) -> TokenCounter:
        """Get tokenizer for a model.

        Args:
            model: Model name (e.g., 'gpt-4o', 'claude-3-sonnet').
            backend: Force specific backend ('tiktoken', 'estimation', etc.).
                    If None, auto-detects based on model name.
            fallback: If True, fall back to estimation on errors.

        Returns:
            TokenCounter instance for the model.

        Raises:
            ValueError: If backend not found and fallback=False.
        """
        registry = cls()
        model_lower = model.lower()

        # Check for explicitly registered tokenizer
        if model_lower in registry._tokenizers:
            return registry._tokenizers[model_lower]

        # Check cache
        cache_key = f"{model_lower}:{backend or 'auto'}"
        if cache_key in registry._cache:
            return registry._cache[cache_key]

        # Create tokenizer
        try:
            tokenizer = registry._create_tokenizer(model, backend)
            registry._cache[cache_key] = tokenizer
            return tokenizer
        except Exception as e:
            if fallback:
                logger.warning(
                    f"Failed to create tokenizer for {model}: {e}. Falling back to estimation."
                )
                # Deliberately NOT cached: caching the fallback would pin this
                # model to estimation for the process lifetime even after a
                # transient failure resolves. The next get() retries creation.
                return EstimatingTokenCounter()
            raise ValueError(f"No tokenizer available for {model}: {e}") from e

    @classmethod
    def register(
        cls,
        model: str,
        tokenizer: TokenCounter | None = None,
        factory: Callable[[str], TokenCounter] | None = None,
    ) -> None:
        """Register a tokenizer or factory for a model.

        Args:
            model: Model name to register.
            tokenizer: Pre-instantiated tokenizer instance.
            factory: Factory function that creates tokenizer for model.

        Raises:
            ValueError: If neither tokenizer nor factory provided.
        """
        registry = cls()
        model_lower = model.lower()

        if tokenizer is not None:
            registry._tokenizers[model_lower] = tokenizer
        elif factory is not None:
            registry._factories[model_lower] = factory
        else:
            raise ValueError("Must provide either tokenizer or factory")

        # Clear cache for this model
        keys_to_remove = [k for k in registry._cache if k.startswith(model_lower)]
        for key in keys_to_remove:
            del registry._cache[key]

    @classmethod
    def register_backend(
        cls,
        backend: str,
        factory: Callable[[str], TokenCounter],
    ) -> None:
        """Register a backend factory.

        Args:
            backend: Backend name.
            factory: Factory function (model: str) -> TokenCounter.
        """
        registry = cls()
        registry._factories[backend] = factory

    @classmethod
    def list_backends(cls) -> list[str]:
        """List available backends."""
        registry = cls()
        return list(registry._factories.keys())

    @classmethod
    def list_registered(cls) -> list[str]:
        """List explicitly registered models."""
        registry = cls()
        return list(registry._tokenizers.keys())

    @classmethod
    def clear_cache(cls) -> None:
        """Clear the tokenizer cache."""
        registry = cls()
        registry._cache.clear()

    def _create_tokenizer(
        self,
        model: str,
        backend: str | None,
    ) -> TokenCounter:
        """Create tokenizer for model.

        Args:
            model: Model name.
            backend: Backend to use (or None for auto-detect).

        Returns:
            TokenCounter instance.
        """
        if backend is None:
            backend = self._detect_backend(model)

        factory = self._factories.get(backend)
        if factory is None:
            raise ValueError(f"Unknown backend: {backend}")

        return factory(model)

    def _detect_backend(self, model: str) -> str:
        """Detect best backend for model.

        Args:
            model: Model name.

        Returns:
            Backend name.
        """
        model_lower = model.lower()

        for pattern, backend in MODEL_PATTERNS:
            if re.match(pattern, model_lower):
                return backend

        # Default to estimation for unknown models
        return "estimation"

    def _create_tiktoken(self, model: str) -> TokenCounter:
        """Create tiktoken-based tokenizer."""
        try:
            from .tiktoken_counter import TiktokenCounter

            return TiktokenCounter(model)
        except ImportError:
            logger.warning("tiktoken not installed. Install with: pip install tiktoken")
            return EstimatingTokenCounter()

    def _create_anthropic(self, model: str) -> TokenCounter:
        """Create Anthropic tokenizer.

        Anthropic uses a custom tokenizer that's not publicly available.
        We use estimation calibrated for Claude models.
        """
        # Claude models use ~3.5 chars per token on average
        return EstimatingTokenCounter(chars_per_token=3.5)

    def _create_google(self, model: str) -> TokenCounter:
        """Create Google tokenizer.

        Gemini uses SentencePiece which isn't easily accessible.
        We use estimation calibrated for Gemini models.
        """
        # Gemini models use ~4 chars per token
        return EstimatingTokenCounter(chars_per_token=4.0)

    def _create_cohere(self, model: str) -> TokenCounter:
        """Create Cohere tokenizer.

        Cohere has its own tokenizer, we use estimation.
        """
        return EstimatingTokenCounter(chars_per_token=4.0)

    def _create_estimation(self, model: str) -> TokenCounter:
        """Create estimation-based tokenizer."""
        return EstimatingTokenCounter()


# Convenience functions
def get_tokenizer(
    model: str,
    backend: str | None = None,
    fallback: bool = True,
) -> TokenCounter:
    """Get tokenizer for a model.

    This is the main entry point for getting tokenizers.

    Args:
        model: Model name (e.g., 'gpt-4o', 'claude-3-sonnet').
        backend: Force specific backend ('tiktoken', 'estimation', etc.).
        fallback: If True, fall back to estimation on errors.

    Returns:
        TokenCounter instance.

    Example:
        tokenizer = get_tokenizer("gpt-4o")
        tokens = tokenizer.count_text("Hello, world!")
    """
    return TokenizerRegistry.get(model, backend, fallback)


def register_tokenizer(
    model: str,
    tokenizer: TokenCounter | None = None,
    factory: Callable[[str], TokenCounter] | None = None,
) -> None:
    """Register a custom tokenizer for a model.

    Args:
        model: Model name.
        tokenizer: Tokenizer instance.
        factory: Factory function.

    Example:
        # Register instance
        register_tokenizer("my-model", MyTokenizer())

        # Register factory
        register_tokenizer("my-model", factory=lambda m: MyTokenizer(m))
    """
    TokenizerRegistry.register(model, tokenizer, factory)


def list_supported_models() -> dict[str, str]:
    """List models with known tokenizer mappings.

    Returns:
        Dict mapping model pattern to backend.
    """
    return dict(MODEL_PATTERNS)
