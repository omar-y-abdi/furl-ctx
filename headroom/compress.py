"""One-function compression API for Headroom.

The simplest way to use Headroom — no proxy, no config, just compress:

    from headroom import compress

    result = compress(messages, model="claude-sonnet-4-5-20250929")
    result.messages          # Compressed messages (same format, fewer tokens)
    result.tokens_saved      # Tokens saved
    result.compression_ratio # e.g., 0.65 means 65% saved

Works with any LLM client, any proxy, any framework. Just compress
the messages before sending them.

Examples:

    # With Anthropic SDK
    from anthropic import Anthropic
    from headroom import compress

    client = Anthropic()
    messages = [{"role": "user", "content": huge_tool_output}]
    compressed = compress(messages, model="claude-sonnet-4-5-20250929")
    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        messages=compressed.messages,
    )

    # With OpenAI SDK
    from openai import OpenAI
    from headroom import compress

    client = OpenAI()
    messages = [{"role": "user", "content": "analyze this"}, {"role": "tool", "content": big_data}]
    compressed = compress(messages, model="gpt-4o")
    response = client.chat.completions.create(model="gpt-4o", messages=compressed.messages)

    # With LiteLLM
    import litellm
    from headroom import compress

    messages = [...]
    compressed = compress(messages, model="bedrock/claude-sonnet")
    response = litellm.completion(model="bedrock/claude-sonnet", messages=compressed.messages)

    # With any HTTP client
    import httpx
    from headroom import compress

    compressed = compress(messages, model="claude-sonnet-4-5-20250929")
    httpx.post("https://api.anthropic.com/v1/messages", json={
        "model": "claude-sonnet-4-5-20250929",
        "messages": compressed.messages,
    })
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from .config import DEFAULT_MIN_TOKENS_TO_COMPRESS
from .pipeline import PipelineExtensionManager, PipelineStage, summarize_routing_markers
from .utils import extract_user_query as _extract_user_query

if TYPE_CHECKING:
    from .hooks import CompressionHooks

logger = logging.getLogger(__name__)


# Lazy-initialized singleton pipeline
_pipeline = None
_pipeline_lock = threading.Lock()


@dataclass
class CompressConfig:
    """User-facing compression options.

    Controls what gets compressed, how aggressively, and with which model.
    Pass to ``compress()`` or any integration that uses headroom.

    Examples::

        # Coding agent (default — skip user messages, protect recent)
        compress(messages, model="gpt-4o")

        # Financial document (compress everything, keep 50%)
        compress(messages, model="claude-opus-4-20250514",
            compress_user_messages=True,
            target_ratio=0.5,
            protect_recent=0,
        )

        # Aggressive (logs, search results)
        compress(messages, model="gpt-4o", target_ratio=0.2)
    """

    # What to compress
    compress_user_messages: bool = False
    """Compress user messages too (default: skip them for coding agents).
    Set True for document compression, RAG pipelines, or when user messages
    contain large tool outputs."""

    compress_system_messages: bool = True
    """Compress system messages (default: True).
    Set False to preserve system prompts exactly as-is. Useful for voice
    agents where tool definitions and instructions must not be altered."""

    protect_recent: int = 4
    """Don't compress the last N messages (they're the active conversation).
    Set 0 to compress everything."""

    protect_analysis_context: bool = True
    """Detect 'analyze'/'review' intent and protect code from compression."""

    # How aggressive
    target_ratio: float | None = None
    """Keep ratio for Kompress. None = model decides (~15% kept, aggressive).
    0.5 = keep 50% (safe for documents). 0.7 = keep 70% (conservative).
    Only affects Kompress (text compression). SmartCrusher (JSON) has its
    own logic based on array dedup."""

    min_tokens_to_compress: int = DEFAULT_MIN_TOKENS_TO_COMPRESS
    """Minimum token count for a message to be compressed.
    Messages shorter than this are left unchanged. Default 250.
    Set lower for voice agents where turns are short."""

    # Model variant
    kompress_model: str | None = None
    """Kompress model ID. None = default (chopratejas/kompress-v2-base).
    Set to a HuggingFace model ID for domain-specific compression.
    Set to 'disabled' to skip ML compression entirely
    (only SmartCrusher + CacheAligner will run)."""


@dataclass
class CompressResult:
    """Result of compressing messages.

    Attributes:
        messages: The compressed messages (same format as input).
        tokens_before: Token count before compression.
        tokens_after: Token count after compression.
        tokens_saved: Tokens removed by compression.
        compression_ratio: Ratio of tokens saved (0.0 = no savings, 1.0 = 100% removed).
        transforms_applied: List of transforms that were applied.
        error: Failure description when compression failed and the original
            messages were returned unchanged (fail-open). None on success and
            on genuine no-ops (empty input, optimize=False). Lets callers tell
            a swallowed pipeline failure apart from a real "nothing to do".
    """

    messages: list[dict[str, Any]]
    tokens_before: int = 0
    tokens_after: int = 0
    tokens_saved: int = 0
    compression_ratio: float = 0.0
    transforms_applied: list[str] = field(default_factory=list)
    error: str | None = None


def _compute_frozen_message_count(messages: list[dict[str, Any]]) -> int:
    """Return the frozen-prefix message count for a list of Anthropic messages.

    The authoritative frozen-count implementation (the former Rust
    ``cache_control::compute_frozen_count`` was orphaned — no PyO3 binding, no
    caller — and was removed in the standalone excise):

    - Walk ``messages[i].content[*]``; for each block (dict) that has a
      top-level ``cache_control`` key, record ``i`` as the highest marker index.
    - Return ``highest_index + 1`` (exclusive floor: the marked message is part
      of the cached prefix and must itself be frozen), or ``0`` if no marker.
    - String-content messages are skipped (no block list → no markers possible).
    - Only the ``messages`` field is inspected; ``system`` and ``tools`` markers
      are never passed here and never bump the floor.
    - ``cache_control: null`` counts as present (key-presence, not truthiness).

    Args:
        messages: List of message dicts in Anthropic format.

    Returns:
        Frozen message count (0 if no cache_control markers found).
    """
    highest_index: int | None = None
    for i, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            # String content: no block list, no cache_control possible.
            continue
        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                highest_index = i if highest_index is None else max(highest_index, i)
    return (highest_index + 1) if highest_index is not None else 0


def compress(
    messages: list[dict[str, Any]],
    model: str = "claude-sonnet-4-5-20250929",
    model_limit: int = 200000,
    optimize: bool = True,
    hooks: CompressionHooks | None = None,
    config: CompressConfig | None = None,
    **kwargs: Any,
) -> CompressResult:
    """Compress messages using Headroom's full compression pipeline.

    This is the simplest way to use Headroom. No proxy, no config needed.
    Just pass messages and get compressed messages back.

    Args:
        messages: List of messages in Anthropic or OpenAI format.
        model: Model name (used for token counting and context limit).
        model_limit: Model's context window size in tokens.
        optimize: Whether to actually compress (False = passthrough for A/B testing).
        hooks: Optional CompressionHooks instance for custom behavior.
        config: Compression options (CompressConfig). Overrides defaults.
        **kwargs: Shorthand for CompressConfig fields. These override config:
            compress_user_messages, compress_system_messages, target_ratio,
            protect_recent, protect_analysis_context, min_tokens_to_compress,
            kompress_model.

    Returns:
        CompressResult with compressed messages and metrics.

    Examples::

        # Default (coding agent)
        result = compress(messages, model="gpt-4o")

        # Financial document (keep 50%, compress everything)
        result = compress(messages, model="claude-opus-4-20250514",
            compress_user_messages=True,
            target_ratio=0.5,
            protect_recent=0,
        )
    """
    if not messages or not optimize:
        return CompressResult(messages=messages)

    # Build config from explicit config + kwargs. Never mutate the
    # caller's CompressConfig — a reused config object must not carry
    # one call's kwarg overrides into the next call.
    cfg = config or CompressConfig()
    config_fields = {f.name for f in cfg.__dataclass_fields__.values()}
    overrides = {key: value for key, value in kwargs.items() if key in config_fields}
    unknown = sorted(set(kwargs) - config_fields)
    if unknown:
        # Fail fast at the public boundary — a typo'd field (e.g.
        # ``target_ration``) silently defaulting would hand back differently
        # compressed output than the caller asked for. Matches the strict
        # ``ContentRouter.apply()`` contract (and Python's own unexpected-kwarg
        # behaviour) rather than warning-and-ignoring.
        raise TypeError(
            f"compress() got unexpected keyword argument(s) "
            f"{', '.join(unknown)}; valid CompressConfig fields: "
            f"{', '.join(sorted(config_fields))}"
        )
    if overrides:
        cfg = replace(cfg, **overrides)

    pipeline = _get_pipeline()
    pipeline_extensions = PipelineExtensionManager(hooks=hooks)

    try:
        # Compute biases from hooks if provided
        biases = None
        if hooks:
            from headroom.hooks import CompressContext

            ctx = CompressContext(model=model)
            messages = hooks.pre_compress(messages, ctx)
            biases = hooks.compute_biases(messages, ctx)

        received_event = pipeline_extensions.emit(
            PipelineStage.INPUT_RECEIVED,
            operation="compress",
            model=model,
            messages=messages,
        )
        if received_event.messages is not None:
            messages = received_event.messages

        # Extract user query from messages so transforms can score by
        # relevance.  Without this, SmartCrusher selects items by statistics
        # alone (position, anomaly) and may drop relevant content.
        context = _extract_user_query(messages)

        # Compute the frozen-prefix count from cache_control markers.
        # Must run AFTER pre_compress hook and INPUT_RECEIVED event may have
        # rewritten messages, so the index aligns with what pipeline sees.
        # Mirrors Rust compute_frozen_count (cache_control.rs:109): only
        # messages[i].content[*].cache_control bumps the floor; system/tools
        # are never passed here.
        frozen = _compute_frozen_message_count(messages)

        result = pipeline.apply(
            messages=messages,
            model=model,
            model_limit=model_limit,
            context=context,
            biases=biases,
            # Pass CompressConfig options through to transforms
            compress_user_messages=cfg.compress_user_messages,
            compress_system_messages=cfg.compress_system_messages,
            target_ratio=cfg.target_ratio,
            protect_recent=cfg.protect_recent,
            protect_analysis_context=cfg.protect_analysis_context,
            min_tokens_to_compress=cfg.min_tokens_to_compress,
            kompress_model=cfg.kompress_model,
            frozen_message_count=frozen,
        )

        tokens_before = result.tokens_before
        tokens_after = result.tokens_after
        compressed_messages = result.messages

        # Guard: if "optimization" inflated tokens, revert to originals.
        # The inflation guard the compression path always applies before
        # returning a result.
        if tokens_after > tokens_before:
            logger.warning(
                "Optimization inflated tokens (%d -> %d); reverting to original messages",
                tokens_before,
                tokens_after,
            )
            return CompressResult(
                messages=messages,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                tokens_saved=0,
                compression_ratio=0.0,
                transforms_applied=["inflation_guard:reverted"],
            )

        routing_markers = summarize_routing_markers(result.transforms_applied)
        if routing_markers:
            routed_event = pipeline_extensions.emit(
                PipelineStage.INPUT_ROUTED,
                operation="compress",
                model=model,
                messages=compressed_messages,
                metadata={
                    "routing_markers": routing_markers,
                    "transforms_applied": result.transforms_applied,
                },
            )
            if routed_event.messages is not None:
                compressed_messages = routed_event.messages

        compressed_event = pipeline_extensions.emit(
            PipelineStage.INPUT_COMPRESSED,
            operation="compress",
            model=model,
            messages=compressed_messages,
            metadata={
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "transforms_applied": result.transforms_applied,
            },
        )
        if compressed_event.messages is not None:
            compressed_messages = compressed_event.messages

        tokens_saved = tokens_before - tokens_after
        ratio = tokens_saved / tokens_before if tokens_before > 0 else 0.0

        # Post-compress hook
        if hooks and tokens_saved > 0:
            from headroom.hooks import CompressEvent

            hooks.post_compress(
                CompressEvent(
                    tokens_before=tokens_before,
                    tokens_after=tokens_after,
                    tokens_saved=tokens_saved,
                    compression_ratio=ratio,
                    transforms_applied=result.transforms_applied,
                    model=model,
                )
            )

        return CompressResult(
            messages=compressed_messages,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tokens_saved=tokens_saved,
            compression_ratio=ratio,
            transforms_applied=result.transforms_applied,
        )

    except Exception as e:
        # Fail-open: a compression bug must NEVER break the host's request, so
        # we return the ORIGINAL messages and do not re-raise. But the failure
        # must be LOUD and HONEST — log at ERROR with a full traceback (this may
        # be a genuine bug or a Rust panic, not a benign no-op) and report the
        # real input token count instead of a fabricated 0, so a caller cannot
        # mistake a swallowed failure for "nothing to compress".
        logger.error(
            "compress() failed; returning original messages (fail-open): %s",
            e,
            exc_info=True,
        )
        # Count the untouched input the same way the pipeline does, but never
        # let token counting break fail-open: if even counting fails, fall back
        # to 0 rather than crash the caller.
        try:
            from headroom.tokenizers import get_tokenizer

            tokens_before = get_tokenizer(model).count_messages(messages)
        except Exception:  # noqa: BLE001 - honest metrics are best-effort
            logger.error(
                "compress(): token counting also failed on the fail-open path; "
                "reporting tokens_before=0",
                exc_info=True,
            )
            tokens_before = 0
        return CompressResult(
            messages=messages,
            tokens_before=tokens_before,
            tokens_after=0,
            tokens_saved=0,
            compression_ratio=0.0,
            error=str(e),
        )


def _get_pipeline() -> Any:
    """Get or create the singleton compression pipeline."""
    global _pipeline

    if _pipeline is not None:
        return _pipeline

    with _pipeline_lock:
        if _pipeline is not None:
            return _pipeline

        from headroom.transforms import TransformPipeline

        # Default pipeline: CacheAligner → ContentRouter
        # CacheAligner: stabilizes prefix for provider KV cache hits
        # ContentRouter: routes to the right compressor per content type
        #   (SmartCrusher for JSON, CodeCompressor for code, Kompress for text)
        # There is no trailing context-management stage —
        # live-zone-only compression never drops messages.
        _pipeline = TransformPipeline()
        logger.debug("Headroom compression pipeline initialized")
        return _pipeline
