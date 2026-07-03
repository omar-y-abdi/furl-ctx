"""Tokenizer registry for universal model support.

Provides automatic tokenizer selection based on model name with
support for multiple backends and custom tokenizers.

The registry is module-level state + functions (SIMP-11); the public
``TokenizerRegistry`` class survives as a thin compatibility wrapper so
existing ``TokenizerRegistry.get(...)`` / ``.register(...)`` call sites
keep working unchanged.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from .base import TokenCounter
from .estimator import EstimatingTokenCounter

logger = logging.getLogger(__name__)


# Model pattern matching for tokenizer selection
# Order matters - more specific patterns first
# Models that match no pattern here fall back to the "estimation" backend
# (EstimatingTokenCounter). This includes Llama/Mistral/Qwen and other open
# models: their HuggingFace/Mistral tokenizer backends were removed
# (tiktoken-only), and estimation is exactly what their missing-dependency
# fallback produced before the removal.
#
# Rust mirror + known divergences (ARCH-6): the Rust registry
# (crates/furl-core/src/tokenizer/registry.rs) mirrors this pattern →
# backend mapping, and the agreeing families are pinned cross-language by
# TEST-8 (tests/test_tokenizer_rust_parity.py ↔ Rust
# tests/tokenizer_python_parity.rs): OpenAI/tiktoken counts are
# byte-identical; the anthropic (3.5 cpt) and google/cohere (4.0 cpt)
# FIXED-ratio estimations match exactly. Two divergences remain — the
# same model name can count differently across the FFI:
#   1. Unknown-model estimation: _create_estimation returns the AUTO
#      EstimatingTokenCounter (density auto-detection 4.0/3.5/3.2 +
#      URL/UUID overhead); Rust uses a FIXED 4.0.
#   2. Legacy OpenAI encoding corners: names absent from
#      MODEL_TO_ENCODING (e.g. "davinci-002") fall to cl100k_base here
#      but match the r50k prefixes in Rust.
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


# ── Backend factories ───────────────────────────────────────────────────────


def _create_tiktoken(model: str) -> TokenCounter:
    """Create tiktoken-based tokenizer."""
    try:
        from .tiktoken_counter import TiktokenCounter

        return TiktokenCounter(model)
    except ImportError:
        logger.warning("tiktoken not installed. Install with: pip install tiktoken")
        return EstimatingTokenCounter()


def _create_anthropic(model: str) -> TokenCounter:
    """Create Anthropic tokenizer.

    Anthropic uses a custom tokenizer that's not publicly available.
    We use estimation calibrated for Claude models.
    """
    # Claude models use ~3.5 chars per token on average
    return EstimatingTokenCounter(chars_per_token=3.5)


def _create_google(model: str) -> TokenCounter:
    """Create Google tokenizer.

    Gemini uses SentencePiece which isn't easily accessible.
    We use estimation calibrated for Gemini models.
    """
    # Gemini models use ~4 chars per token
    return EstimatingTokenCounter(chars_per_token=4.0)


def _create_cohere(model: str) -> TokenCounter:
    """Create Cohere tokenizer.

    Cohere has its own tokenizer, we use estimation.
    """
    return EstimatingTokenCounter(chars_per_token=4.0)


def _create_estimation(model: str) -> TokenCounter:
    """Create estimation-based tokenizer."""
    return EstimatingTokenCounter()


# ── Module-level registry state ─────────────────────────────────────────────

# Explicitly registered tokenizers (model -> tokenizer instance).
_tokenizers: dict[str, TokenCounter] = {}

# Registered factories (backend -> factory function). ``register_tokenizer``
# with ``factory=`` also lands here keyed by the model name — such a factory
# is reachable via ``get_tokenizer(model, backend=<model>)`` (historical
# behavior, preserved).
_factories: dict[str, Callable[[str], TokenCounter]] = {
    "tiktoken": _create_tiktoken,
    "anthropic": _create_anthropic,
    "google": _create_google,
    "cohere": _create_cohere,
    "estimation": _create_estimation,
}

# Cache for auto-detected tokenizers (``"{model}:{backend or 'auto'}"`` keys).
_cache: dict[str, TokenCounter] = {}


# ── Registry operations ─────────────────────────────────────────────────────


def _detect_backend(model: str) -> str:
    """Detect the best backend for *model* via ``MODEL_PATTERNS``."""
    model_lower = model.lower()

    for pattern, backend in MODEL_PATTERNS:
        if re.match(pattern, model_lower):
            return backend

    # Default to estimation for unknown models
    return "estimation"


def _create_tokenizer(model: str, backend: str | None) -> TokenCounter:
    """Create a tokenizer for *model* using *backend* (or auto-detect).

    Raises:
        ValueError: If the backend is unknown.
    """
    if backend is None:
        backend = _detect_backend(model)

    factory = _factories.get(backend)
    if factory is None:
        raise ValueError(f"Unknown backend: {backend}")

    return factory(model)


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
                If None, auto-detects based on model name.
        fallback: If True, fall back to estimation on errors.

    Returns:
        TokenCounter instance for the model.

    Raises:
        ValueError: If backend not found and fallback=False.

    Example:
        tokenizer = get_tokenizer("gpt-4o")
        tokens = tokenizer.count_text("Hello, world!")
    """
    model_lower = model.lower()

    # Check for explicitly registered tokenizer
    if model_lower in _tokenizers:
        return _tokenizers[model_lower]

    # Check cache
    cache_key = f"{model_lower}:{backend or 'auto'}"
    if cache_key in _cache:
        return _cache[cache_key]

    # Create tokenizer
    try:
        tokenizer = _create_tokenizer(model, backend)
        _cache[cache_key] = tokenizer
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


def register_tokenizer(
    model: str,
    tokenizer: TokenCounter | None = None,
    factory: Callable[[str], TokenCounter] | None = None,
) -> None:
    """Register a custom tokenizer or factory for a model.

    Args:
        model: Model name to register.
        tokenizer: Pre-instantiated tokenizer instance.
        factory: Factory function that creates tokenizer for model.

    Raises:
        ValueError: If neither tokenizer nor factory provided.

    Example:
        # Register instance
        register_tokenizer("my-model", MyTokenizer())

        # Register factory
        register_tokenizer("my-model", factory=lambda m: MyTokenizer(m))
    """
    model_lower = model.lower()

    if tokenizer is not None:
        _tokenizers[model_lower] = tokenizer
    elif factory is not None:
        _factories[model_lower] = factory
    else:
        raise ValueError("Must provide either tokenizer or factory")

    # Clear cache for this model
    keys_to_remove = [k for k in _cache if k.startswith(model_lower)]
    for key in keys_to_remove:
        del _cache[key]


def register_backend(backend: str, factory: Callable[[str], TokenCounter]) -> None:
    """Register a backend factory.

    Args:
        backend: Backend name.
        factory: Factory function (model: str) -> TokenCounter.
    """
    _factories[backend] = factory


def list_backends() -> list[str]:
    """List available backends."""
    return list(_factories.keys())


def list_registered() -> list[str]:
    """List explicitly registered models."""
    return list(_tokenizers.keys())


def clear_cache() -> None:
    """Clear the tokenizer cache."""
    _cache.clear()


def list_supported_models() -> dict[str, str]:
    """List models with known tokenizer mappings.

    Returns:
        Dict mapping model pattern to backend.
    """
    return dict(MODEL_PATTERNS)


# ── Compatibility wrapper ───────────────────────────────────────────────────


class TokenizerRegistry:
    """Thin compatibility wrapper over the module-level registry.

    The registry used to be a singleton-of-classmethods; the state and
    logic now live at module level (SIMP-11). The public entry points
    (``get`` / ``register`` / ``register_backend`` / ``list_backends`` /
    ``list_registered`` / ``clear_cache``) are preserved as static
    delegates so existing call sites keep working.

    Example:
        # Auto-detect tokenizer
        tokenizer = TokenizerRegistry.get("gpt-4o")

        # Register custom tokenizer
        TokenizerRegistry.register("my-model", my_tokenizer)

        # Use specific backend
        tokenizer = TokenizerRegistry.get("gpt-4", backend="tiktoken")
    """

    get = staticmethod(get_tokenizer)
    register = staticmethod(register_tokenizer)
    register_backend = staticmethod(register_backend)
    list_backends = staticmethod(list_backends)
    list_registered = staticmethod(list_registered)
    clear_cache = staticmethod(clear_cache)
