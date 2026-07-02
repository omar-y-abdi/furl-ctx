"""One-function compression API for Furl.

The simplest way to use Furl — no proxy, no config, just compress:

    from furl_ctx import compress

    result = compress(messages, model="claude-sonnet-4-5-20250929")
    result.messages          # Compressed messages (same format, fewer tokens)
    result.tokens_saved      # Tokens saved
    result.compression_ratio # e.g., 0.65 means 65% saved

Works with any LLM client, any proxy, any framework. Just compress
the messages before sending them.

Examples:

    # With Anthropic SDK
    from anthropic import Anthropic
    from furl_ctx import compress

    client = Anthropic()
    messages = [{"role": "user", "content": huge_tool_output}]
    compressed = compress(messages, model="claude-sonnet-4-5-20250929")
    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        messages=compressed.messages,
    )

    # With OpenAI SDK
    from openai import OpenAI
    from furl_ctx import compress

    client = OpenAI()
    messages = [{"role": "user", "content": "analyze this"}, {"role": "tool", "content": big_data}]
    compressed = compress(messages, model="gpt-4o")
    response = client.chat.completions.create(model="gpt-4o", messages=compressed.messages)

    # With LiteLLM
    import litellm
    from furl_ctx import compress

    messages = [...]
    compressed = compress(messages, model="bedrock/claude-sonnet")
    response = litellm.completion(model="bedrock/claude-sonnet", messages=compressed.messages)

    # With any HTTP client
    import httpx
    from furl_ctx import compress

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

# Warn when the frozen prefix swallows (nearly) the whole conversation:
# cache_control on the LAST message — the multi-turn idiom Anthropic's docs
# teach — freezes everything, every transform skips, and compression reports
# 0 saved with no error. > 0.9 also catches the "all but the newest turn"
# shape, where the same surprise applies.
_FROZEN_WARN_FRACTION = 0.9

# Roles whose string content is a tool output the replacement transforms
# rewrite (mirrors cross_message_dedup._TOOL_ROLES; ReadLifecycleManager
# handles the "tool" subset). Used by the frozen-prefix conflict detector.
_TOOL_OUTPUT_ROLES = frozenset({"tool", "function"})


@dataclass
class CompressConfig:
    """User-facing compression options.

    Controls what gets compressed, how aggressively, and with which model.
    Pass to ``compress()`` or any integration that uses furl_ctx.

    Examples::

        # Coding agent (default — skip user messages, protect recent)
        compress(messages, model="gpt-4o")

        # Document pipeline (compress everything)
        compress(messages, model="claude-opus-4-20250514",
            compress_user_messages=True,
            protect_recent=0,
        )
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

    min_tokens_to_compress: int = DEFAULT_MIN_TOKENS_TO_COMPRESS
    """Minimum token count for a message to be compressed.
    Messages shorter than this are left unchanged. Default 250.
    Set lower for voice agents where turns are short."""


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
        warnings: Non-fatal problems detected while compressing (each is also
            logged at WARNING). Aggregates transform-level warnings with
            compress()-level diagnostics — e.g. a ``cache_control`` breakpoint
            freezing the whole conversation (0 tokens can be saved), or a
            frozen message whose bytes Furl previously shipped compressed
            (a guaranteed provider prefix-cache miss). Empty on clean runs.
    """

    messages: list[dict[str, Any]]
    tokens_before: int = 0
    tokens_after: int = 0
    tokens_saved: int = 0
    compression_ratio: float = 0.0
    transforms_applied: list[str] = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


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


def _frozen_prefix_warning(total_messages: int, frozen: int) -> str | None:
    """Warning when the cache_control floor freezes (nearly) every message.

    With ``cache_control`` on the LAST message — the multi-turn idiom
    Anthropic's docs teach — ``frozen == len(messages)``: every transform
    skips everything, 0 tokens are saved, ``error`` stays None, and without
    this warning there is no way to notice. Pure and total: returns the
    warning string, or None when the frozen fraction is unremarkable.

    Args:
        total_messages: Number of messages in the request.
        frozen: Frozen-prefix message count (see
            :func:`_compute_frozen_message_count`).

    Returns:
        Warning text when ``frozen == total_messages`` or the frozen fraction
        exceeds ``_FROZEN_WARN_FRACTION``; None otherwise.
    """
    if total_messages <= 0 or frozen <= 0:
        return None
    if frozen >= total_messages:
        return (
            f"cache_control on the last message freezes all {total_messages} "
            "messages: every transform skips the frozen prefix, so 0 tokens "
            "can be saved. Mark the cache breakpoint BEFORE the live zone you "
            "want compressed, or compress before marking."
        )
    fraction = frozen / total_messages
    if fraction > _FROZEN_WARN_FRACTION:
        return (
            f"cache_control freezes {frozen} of {total_messages} messages "
            f"({fraction:.0%}): nearly the whole conversation is skipped by "
            "every transform. Mark the cache breakpoint BEFORE the live zone "
            "you want compressed, or compress before marking."
        )
    return None


def _frozen_tool_output_units(message: dict[str, Any]) -> list[str]:
    """Tool-output strings in *message* that replacement transforms rewrite.

    OpenAI-style tool/function messages with string content, and
    Anthropic-style ``tool_result`` blocks with string content — the two
    shapes ``CrossMessageDeduper`` / ``ReadLifecycleManager`` replace. Pure
    and total: any other shape yields an empty list.
    """
    role = message.get("role", "")
    content = message.get("content", "")
    if role in _TOOL_OUTPUT_ROLES and isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        block["content"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_result"
        and isinstance(block.get("content"), str)
    ]


def _frozen_transformed_content_warning(messages: list[dict[str, Any]], frozen: int) -> str | None:
    """Detect a cache breakpoint moved PAST previously-compressed turns.

    The frozen prefix freezes the INPUT bytes, but what the provider cached
    last turn is whatever Furl SHIPPED last turn. A caller who re-sends
    the original (uncompressed) history each turn and moves the cache_control
    marker forward across a message that previously shipped transformed
    (dedup sentinel, read-lifecycle marker) gets (a) a guaranteed prefix-cache
    miss at the very message asserted cached, then (b) a permanent token
    regression — the message is frozen, so it ships uncompressed forever —
    with no signal. There is no exact stateless test for this, so this is a
    cheap best-effort detector over the CCR registry:

    * a frozen tool output that is a byte-identical LATER duplicate of an
      earlier frozen unit, with a CCR entry for those bytes → it previously
      shipped as a ``cross_message_dedup`` sentinel (dedup only ever rewrites
      later copies, and the earlier copy is in the frozen prefix too);
    * a frozen tool output whose CCR entry stored ``compressed_content == ""``
      → it previously shipped as a read-lifecycle marker (the only writer of
      empty compressed content).

    Best-effort on purpose: CCR entries expire with the store TTL, the store
    is process-global (a hit can come from a sibling conversation), and
    router-side lossy compressions are not covered — a None here is not proof
    of safety. The contract itself is documented on :func:`compress`.

    Returns:
        Warning text naming the suspect message indexes, or None. Never
        raises — diagnostics must not break the request.
    """
    if frozen <= 0 or not messages:
        return None
    try:
        from .cache.compression_store import get_compression_store
        from .transforms.cross_message_dedup import MIN_DEDUP_CHARS, _content_hash

        store = get_compression_store()
        seen_frozen_hashes: set[str] = set()
        suspect_indexes: list[int] = []

        for index in range(min(frozen, len(messages))):
            for unit in _frozen_tool_output_units(messages[index]):
                if len(unit) < MIN_DEDUP_CHARS:
                    # Below the smallest unit any replacement transform
                    # rewrites — and well-behaved callers' frozen sentinels
                    # land here, so the common path does zero store lookups.
                    continue
                unit_hash = _content_hash(unit)
                duplicate_of_frozen = unit_hash in seen_frozen_hashes
                seen_frozen_hashes.add(unit_hash)
                metadata = store.get_metadata(unit_hash)
                if metadata is None:
                    continue
                previously_dedup_sentinel = duplicate_of_frozen
                previously_lifecycle_marker = metadata.get("compressed_content") == ""
                if previously_dedup_sentinel or previously_lifecycle_marker:
                    suspect_indexes.append(index)
                    break  # One suspect unit per message is enough.

        if not suspect_indexes:
            return None
        indexes = ", ".join(str(i) for i in suspect_indexes)
        return (
            f"frozen prefix message(s) [{indexes}] contain tool output that "
            "Furl previously shipped in compressed form (CCR registry "
            "hit): the provider cached the TRANSFORMED bytes, so re-sending "
            "the original history with the cache breakpoint moved past that "
            "turn guarantees a prefix-cache miss there and pins the message "
            "uncompressed forever. Pass the previously returned "
            "result.messages back in, or do not move the cache_control "
            "marker past turns that already shipped compressed."
        )
    except Exception:  # noqa: BLE001 - best-effort diagnostics only
        logger.debug(
            "frozen-prefix transformed-content detection failed (non-fatal)",
            exc_info=True,
        )
        return None


def compress(
    messages: list[dict[str, Any]],
    model: str = "claude-sonnet-4-5-20250929",
    model_limit: int = 200000,
    optimize: bool = True,
    hooks: CompressionHooks | None = None,
    config: CompressConfig | None = None,
    **kwargs: Any,
) -> CompressResult:
    """Compress messages using Furl's full compression pipeline.

    This is the simplest way to use Furl. No proxy, no config needed.
    Just pass messages and get compressed messages back.

    Args:
        messages: List of messages in Anthropic or OpenAI format.
        model: Model name (used for token counting and context limit).
        model_limit: Model's context window size in tokens.
        optimize: Whether to actually compress (False = passthrough for A/B testing).
        hooks: Optional CompressionHooks instance for custom behavior.
        config: Compression options (CompressConfig). Overrides defaults.
        **kwargs: Shorthand for CompressConfig fields. These override config:
            compress_user_messages, compress_system_messages, protect_recent,
            protect_analysis_context, min_tokens_to_compress.

    Prompt caching (``cache_control``):
        Messages up to and including the HIGHEST message carrying an
        Anthropic ``cache_control`` marker form the frozen prefix — Furl
        never modifies them, so the provider's prompt cache keeps hitting.
        Two consequences follow:

        * Marking the LAST message freezes the entire conversation: every
          transform skips everything and 0 tokens are saved (``error`` stays
          None). Mark the breakpoint BEFORE the live zone you want
          compressed, or compress before marking. Detected and surfaced via
          ``result.warnings``.
        * The frozen prefix freezes the bytes you PASS IN, while the provider
          cached the bytes Furl SHIPPED last turn. On multi-turn
          conversations, pass the previously returned ``result.messages``
          (the compressed history) back in — or do not move the marker
          forward past turns that already shipped compressed. Re-sending
          original history with a forward-moved marker guarantees a
          prefix-cache miss at the previously compressed message and pins it
          uncompressed forever (it is frozen). Best-effort detected via
          ``result.warnings``.

    Returns:
        CompressResult with compressed messages and metrics.

    Examples::

        # Default (coding agent)
        result = compress(messages, model="gpt-4o")

        # Document pipeline (compress everything)
        result = compress(messages, model="claude-opus-4-20250514",
            compress_user_messages=True,
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
            from furl_ctx.hooks import CompressContext

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

        # cache_control interactions must be LOUD but non-fatal. A breakpoint
        # on (nearly) the last message freezes everything — every transform
        # skips, 0 tokens saved, error=None — and a breakpoint moved forward
        # past a turn that previously shipped compressed guarantees a
        # prefix-cache miss at the very message the caller asserts is cached.
        # Neither is a failure (fail-open stays for real failures); both
        # surface in CompressResult.warnings and the log.
        compress_warnings: list[str] = []
        frozen_floor_warning = _frozen_prefix_warning(len(messages), frozen)
        if frozen_floor_warning is not None:
            compress_warnings.append(frozen_floor_warning)
        frozen_content_warning = _frozen_transformed_content_warning(messages, frozen)
        if frozen_content_warning is not None:
            compress_warnings.append(frozen_content_warning)
        for warning in compress_warnings:
            logger.warning("%s", warning)

        result = pipeline.apply(
            messages=messages,
            model=model,
            model_limit=model_limit,
            context=context,
            biases=biases,
            # Pass CompressConfig options through to transforms
            compress_user_messages=cfg.compress_user_messages,
            compress_system_messages=cfg.compress_system_messages,
            protect_recent=cfg.protect_recent,
            protect_analysis_context=cfg.protect_analysis_context,
            min_tokens_to_compress=cfg.min_tokens_to_compress,
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
                warnings=[*compress_warnings, *result.warnings],
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
            from furl_ctx.hooks import CompressEvent

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
            # Transform warnings (TransformResult.warnings) were previously
            # dropped here; plumb them through alongside the compress()-level
            # frozen-prefix diagnostics so callers can actually see them.
            warnings=[*compress_warnings, *result.warnings],
        )

    except (KeyboardInterrupt, SystemExit):
        # NEVER swallow these: a Ctrl-C or an interpreter shutdown during
        # compression must tear down exactly as the operator intended, not be
        # masked as a fail-open no-op. Re-raise before the BaseException catch
        # below can reach them.
        raise
    except BaseException as e:  # noqa: BLE001
        # Fail-open: a compression bug must NEVER break the host's request, so
        # we return the ORIGINAL messages and do not re-raise. But the failure
        # must be LOUD and HONEST — log at ERROR with a full traceback (this may
        # be a genuine bug or a Rust panic, not a benign no-op) and report the
        # real input token count instead of a fabricated 0, so a caller cannot
        # mistake a swallowed failure for "nothing to compress".
        #
        # We catch BaseException (not just Exception) on purpose: a Rust panic
        # crosses the PyO3 FFI as ``pyo3_runtime.PanicException``, which is a
        # ``BaseException`` and would otherwise escape ``except Exception`` —
        # crashing the host request, the exact class this fail-open exists for.
        # The bridge methods also convert panics to ``PyRuntimeError`` at the
        # Rust edge (see crates/furl-py/src/lib.rs); this is the
        # belt-and-braces backstop for any entry point not wrapped there.
        logger.error(
            "compress() failed; returning original messages (fail-open): %s",
            e,
            exc_info=True,
        )
        # Count the untouched input the same way the pipeline does, but never
        # let token counting break fail-open: if even counting fails, fall back
        # to 0 rather than crash the caller.
        try:
            from furl_ctx.tokenizers import get_tokenizer

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

        from furl_ctx.transforms import TransformPipeline

        # Default pipeline: CrossMessageDeduper → ContentRouter.
        # CacheAligner is opt-in (FurlConfig.cache_aligner.enabled,
        # default False): when enabled it runs first and only WARNS about
        # unstable prefixes — detector-only, never rewrites the prompt.
        # ContentRouter: routes to the right compressor per content type
        #   (SmartCrusher for JSON; log/search/diff compressors;
        #   plain text and source code pass through)
        # There is no trailing context-management stage —
        # live-zone-only compression never drops messages.
        _pipeline = TransformPipeline()
        logger.debug("Furl compression pipeline initialized")
        return _pipeline
