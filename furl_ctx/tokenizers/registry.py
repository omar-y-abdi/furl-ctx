"""Tokenizer registry for universal model support.

Provides automatic tokenizer selection based on model name with
support for multiple backends and custom tokenizers.

The registry is module-level state + functions: ``get_tokenizer``,
``register_tokenizer``, and ``list_supported_models`` are the public API.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from .base import TokenCounter
from .estimator import EstimatingTokenCounter

logger = logging.getLogger(__name__)


# Tokenizer patterns are ordered from specific to general. Python and Rust agree for supported tiktoken/fixed-estimation
# families; known divergences are unknown-model density heuristics and some legacy OpenAI fallback encodings.
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
    # Anthropic models -> tiktoken o200k_base (closest public BPE, Q1)
    (r"^claude-", "anthropic"),
    # Google models -> estimation (Gemini uses SentencePiece)
    (r"^gemini", "google"),
    (r"^palm", "google"),
    # Cohere models -> estimation
    (r"^command", "cohere"),
]


# Anthropic and fixed-ratio vendor backends are documented proxies, not live vendor-tokenizer measurements.
# Factories stay silent; callers may surface `proxy_for` notes at a cadence they can deduplicate across subprocesses.

ANTHROPIC_O200K_PROXY_NOTE: str = (
    "claude-* token counts are computed with tiktoken's o200k_base encoding "
    "(byte-identical to gpt-4o) as a PROXY for Anthropic's own tokenizer, "
    "which is not publicly available. Per Anthropic's published developer "
    "guidance, this undercounts real Claude billing tokens by roughly "
    "15-20% on typical text, and by more on code or non-English text. "
    "Reported counts and savings percentages for claude-* models are an "
    "approximation, not an exact Anthropic token count."
)

FIXED_RATIO_ESTIMATOR_NOTE: str = (
    "this model's token counts use a fixed 4.0 chars-per-token estimate "
    "(neither Gemini's SentencePiece nor Cohere's tokenizer is accessible "
    "here) instead of a real BPE tokenizer. This fixed ratio can be roughly "
    "2x off — closer for plain English prose/code, worse for CJK, other "
    "non-Latin scripts, or densely structured content such as JSON. "
    "Reported counts and savings percentages for this model are a rough "
    "approximation."
)


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
    """Create Anthropic tokenizer using tiktoken o200k_base as a PROXY (T10).

    Anthropic's own tokenizer is not publicly available. o200k_base (the
    GPT-4o encoding) is the closest public BPE and far more accurate than
    the old 3.5-chars/token flat estimate, especially for CJK and emoji
    content — but it is still a PROXY, not the real Anthropic tokenizer.
    See ``ANTHROPIC_O200K_PROXY_NOTE`` for the documented error band; the
    returned counter's ``proxy_for`` attribute and ``repr`` are the
    caller-visible signal that this is not an exact Anthropic billing
    count. Deliberately does NOT log at construction time (T10
    remediation) — see the module comment above ``ANTHROPIC_O200K_PROXY_NOTE``.

    Falls back to EstimatingTokenCounter(3.5) only when tiktoken is absent
    (ImportError), preserving cold-path safety on minimal installs.
    """
    try:
        from .tiktoken_counter import TiktokenCounter

        return TiktokenCounter(model, proxy_for="anthropic")
    except ImportError:
        logger.warning(
            "tiktoken not installed — claude-* falling back to 3.5-cpt estimation. "
            "Install with: pip install tiktoken"
        )
        return EstimatingTokenCounter(chars_per_token=3.5, proxy_for="anthropic")


def _create_fixed_estimation(model: str) -> TokenCounter:
    """Create fixed-ratio estimation tokenizer (Google/Cohere) as a PROXY (T10).

    Gemini uses SentencePiece and Cohere has its own tokenizer, neither
    easily accessible. Both estimate at ~4 chars per token — a much cruder
    approximation than a real BPE tokenizer. See
    ``FIXED_RATIO_ESTIMATOR_NOTE`` for the documented error band (reproduced
    against this project's own tokenizer, not literature-only).
    Deliberately does NOT log at construction time (T10 remediation) — see
    the module comment above ``ANTHROPIC_O200K_PROXY_NOTE``.
    """
    return EstimatingTokenCounter(chars_per_token=4.0, proxy_for="fixed_ratio_estimate")


def _create_estimation(model: str) -> TokenCounter:
    """Create estimation-based tokenizer."""
    return EstimatingTokenCounter()


# ── Module-level registry state ─────────────────────────────────────────────

# Explicitly registered tokenizers (model -> tokenizer instance).
_tokenizers: dict[str, TokenCounter] = {}

# Registered factories (backend -> factory function). ``register_tokenizer`` with ``factory=`` also lands here keyed by the
# model name — such a factory is reachable via ``get_tokenizer(model, backend=<model>)`` (historical behavior, preserved).
_factories: dict[str, Callable[[str], TokenCounter]] = {
    "tiktoken": _create_tiktoken,
    "anthropic": _create_anthropic,
    "google": _create_fixed_estimation,
    "cohere": _create_fixed_estimation,
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
            # Deliberately NOT cached: caching the fallback would pin this model to estimation for the
            # process lifetime even after a transient failure resolves. The next get() retries creation.
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


def list_supported_models() -> dict[str, str]:
    """List models with known tokenizer mappings.

    Returns:
        Dict mapping model pattern to backend.
    """
    return dict(MODEL_PATTERNS)
