"""Tokenizer registry for universal model support.

Provides automatic tokenizer selection based on model name with
support for multiple backends and custom tokenizers.

The registry is module-level state + functions (SIMP-11): ``get_tokenizer``,
``register_tokenizer``, and ``list_supported_models`` are the public API.
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
# byte-identical; Anthropic claude-* now also uses tiktoken o200k_base
# (byte-identical across FFI, Q1); google/cohere (4.0 cpt) FIXED-ratio
# estimations match exactly. Two divergences remain — the same model name
# can count differently across the FFI:
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
    # Anthropic models -> tiktoken o200k_base (closest public BPE, Q1)
    (r"^claude-", "anthropic"),
    # Google models -> estimation (Gemini uses SentencePiece)
    (r"^gemini", "google"),
    (r"^palm", "google"),
    # Cohere models -> estimation
    (r"^command", "cohere"),
]


# ── Documented tokenizer-accuracy caveats (T10) ─────────────────────────────
#
# Two backends below stand in for a vendor tokenizer this project has no
# access to. Neither error band is fabricated:
#
# - ANTHROPIC_O200K_PROXY_NOTE cites Anthropic's own published developer
#   guidance on tiktoken vs. Claude token counts, not a live measurement —
#   the `anthropic` package is not a project dependency, and even installed,
#   real calibration needs a live `POST /v1/messages/count_tokens` API call
#   (network + an API key) this project does not make from a tokenizer
#   constructor. See tests/test_tokenizers.py for the calibration-limit note.
# - FIXED_RATIO_ESTIMATOR_NOTE's "~2x" was reproduced directly against this
#   project's own o200k_base tokenizer (see tests/test_tokenizers.py), not
#   measured against a real Gemini/Cohere tokenizer, which this project has
#   no access to either.
#
# If a real tokenizer for either vendor is ever wired in, `_create_anthropic`
# / `_create_fixed_estimation` must stop tagging their result `proxy_for=...`
# — the pinning tests in tests/test_tokenizers.py force that to be a
# conscious change, not a silently stale label.
#
# Neither factory logs at construction time. `get_tokenizer` is a plain
# library call, cached per process at module scope (`_cache`) — but Furl's
# own plugin hooks (plugins/furl/hooks/pipe_compress.py,
# compress_tool_output.py) invoke it from a FRESH subprocess per tool call,
# so that cache starts empty on every single invocation. A construction-time
# `logger.warning` here fired on every tool call, not once per session — an
# earlier revision of this fix did exactly that, and an adversarial review
# caught it: with no logging handler configured, Python's `lastResort`
# handler prints straight to stderr, roughly 100 writes over a 50-call
# default-config session. See tests/test_tokenizers.py's
# `test_claude_proxy_construction_note_never_leaks_across_real_subprocesses`
# for the reproduction. Surfacing this note at a sane cadence is a
# caller-side concern: a caller that wants it shown reads `proxy_for` off
# the returned counter and formats the NOTE constant itself, at whatever
# cadence its own layer can dedupe correctly across process boundaries —
# this module has no visibility into "session" once a process exits.

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

# Registered factories (backend -> factory function). ``register_tokenizer``
# with ``factory=`` also lands here keyed by the model name — such a factory
# is reachable via ``get_tokenizer(model, backend=<model>)`` (historical
# behavior, preserved).
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


def list_supported_models() -> dict[str, str]:
    """List models with known tokenizer mappings.

    Returns:
        Dict mapping model pattern to backend.
    """
    return dict(MODEL_PATTERNS)
