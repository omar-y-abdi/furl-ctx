"""Content router for intelligent compression strategy selection.

This module provides the ContentRouter, which analyzes content and routes it
to the optimal compressor. It handles mixed content by splitting, routing
each section to the appropriate compressor, and reassembling.

Supported Compressors:
- SmartCrusher: JSON arrays
- SearchCompressor: grep/ripgrep results
- LogCompressor: Build/test output
- KompressCompressor: Plain text (ML-based, requires [ml] extra)

Routing Strategy:
1. Use source hint if available (highest confidence)
2. Check for mixed content (split and route sections)
3. Detect content type (JSON, code, search, logs, text)
4. Route to appropriate compressor
5. Reassemble and return with routing metadata

Usage:
    >>> from headroom.transforms import ContentRouter
    >>> router = ContentRouter()
    >>> result = router.compress(content)  # Auto-routes to best compressor
    >>> print(result.strategy_used)
    >>> print(result.routing_log)

Pipeline Usage:
    >>> pipeline = TransformPipeline([
    ...     ContentRouter(),   # Handles all content types
    ... ])
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..telemetry.models import ToolSignature
    from .kompress_compressor import KompressCompressor

from ..config import (
    DEFAULT_EXCLUDE_TOOLS,
    CompressRequest,
    ReadLifecycleConfig,
    TransformResult,
)
from ..tokenizer import Tokenizer
from .base import Transform
from .compressor_registry import CompressorRegistry
from .content_detector import ContentType, DetectionResult
from .content_detector import detect_content_type as _regex_detect_content_type
from .error_detection import content_has_strong_error_indicators

# Extracted seams (pure moves). Re-imported here so that:
#   * existing ``from ...content_router import X`` imports keep resolving,
#   * the package lazy-export in ``transforms/__init__.py`` keeps working,
#   * in-module callers reference these as module globals (so the test
#     suite's ``monkeypatch.setattr(content_router_module, "...", ...)`` on
#     ``is_mixed_content`` / ``split_into_sections`` still bites), and
#   * ``content_router_module.time`` patches still target the same ``time``
#     module object the cache uses.
from .router_cache import CompressionCache
from .router_ccr_mirror import CcrMirror
from .router_dispatch import StrategyDispatcher
from .router_policy import (
    CompressionStrategy,
    adaptive_min_ratio,
    content_type_from_strategy,
    strategy_from_detection,
    strategy_from_detection_type,
)
from .router_split import (
    _CODE_FENCE_PATTERN,
    _JSON_BLOCK_START,
    _PROSE_PATTERN,
    _SEARCH_RESULT_PATTERN,
    ContentSection,
    _extract_json_block,  # noqa: F401 — re-exported for backward-compatible imports/tests
    is_mixed_content,
    split_into_sections,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouterRuntime:
    """Frozen per-request routing options threaded by argument.

    The pipeline reuses ONE :class:`ContentRouter` across every concurrent
    ``compress()`` call (the MCP server runs ``compress()`` on a
    ``ThreadPoolExecutor``). These four options are per-request, not
    per-router. Carrying them as an immutable value passed down the call
    chain — rather than mutable thread-local state — isolates each request
    *structurally*: two concurrent calls hold distinct ``RouterRuntime``
    instances, so neither can observe the other's options. Worker threads
    receive the same frozen instance by value (no thread-local to replay).

    Fields:
        target_ratio: Per-request ML target ratio (``None`` = compressor default).
        force_kompress: Force the KOMPRESS strategy regardless of detection.
        kompress_model: Override Kompress model id (``"disabled"`` skips ML).
    """

    target_ratio: float | None = None
    force_kompress: bool = False
    kompress_model: str | None = None

    @classmethod
    def from_kwargs(cls, kwargs: dict[str, Any]) -> RouterRuntime:
        """Build a RouterRuntime from ``apply()`` kwargs.

        Mirrors the historical defaults exactly: ``target_ratio`` /
        ``kompress_model`` default to ``None`` and ``force_kompress``
        coerces to ``bool`` (``False`` when absent).
        """
        return cls(
            target_ratio=kwargs.get("target_ratio"),
            force_kompress=bool(kwargs.get("force_kompress", False)),
            kompress_model=kwargs.get("kompress_model"),
        )


# Shared default for the no-options path. Frozen + immutable, so it is safe to
# share as a module-level singleton across every call that omits ``runtime``
# (direct ``compress()`` callers, tests, hand-written pipelines).
_DEFAULT_RUNTIME = RouterRuntime()


@dataclass(frozen=True)
class ServeOriginal:
    """Serve the original message/block unchanged — the two-tier cache says this
    content will not compress: a Tier-1 skip hit, or a Tier-2 entry whose ratio
    no longer clears ``min_ratio`` and was relocated to the skip set."""


@dataclass(frozen=True)
class ServeCached:
    """Serve a live cached compression whose ``<<ccr:HASH>>`` sentinels (if any)
    are confirmed still backed. The caller swaps in ``compressed`` and formats
    the transform string — the flat (string-path) and label-threaded
    (block-path) formats differ, so formatting stays in the caller."""

    compressed: str
    strategy: str
    ratio: float


@dataclass(frozen=True)
class Recompute:
    """Cache miss, or a stale Tier-2 entry whose CCR backing has expired and was
    evicted. The caller (re)compresses — inline on the block path, deferred to
    the batched parallel pass on the string path."""


# A two-tier cache lookup resolves to exactly one of three dispositions. The two
# empty variants carry no data, so they are shared module singletons: the hot
# path resolves one per message and must not allocate for the common
# serve-original / recompute cases. ``ServeCached`` holds per-entry payload and
# stays fresh.
CacheDisposition = ServeOriginal | ServeCached | Recompute
_SERVE_ORIGINAL = ServeOriginal()
_RECOMPUTE = Recompute()


def _router_debug_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _log_router_debug(event: str, **payload: Any) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return
    payload = {"event": event, **payload}
    logger.debug("event=%s %s", event, _router_debug_dumps(payload))


def _json_shape(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except Exception as exc:
        return {"is_json": False, "error": type(exc).__name__}
    if isinstance(parsed, dict):
        return {
            "is_json": True,
            "kind": "object",
            "keys": list(parsed.keys()),
            "length": len(parsed),
        }
    if isinstance(parsed, list):
        return {"is_json": True, "kind": "array", "length": len(parsed)}
    return {"is_json": True, "kind": type(parsed).__name__}


def _mixed_indicators(content: str) -> dict[str, bool]:
    return {
        "has_code_fences": bool(_CODE_FENCE_PATTERN.search(content)),
        "has_json_blocks": bool(_JSON_BLOCK_START.search(content)),
        "has_prose": len(_PROSE_PATTERN.findall(content)) > 5,
        "has_search_results": bool(_SEARCH_RESULT_PATTERN.search(content)),
    }


def _section_debug(section: ContentSection, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "content_type": section.content_type.value,
        "language": getattr(section, "language", None),
        "start_line": getattr(section, "start_line", None),
        "end_line": getattr(section, "end_line", None),
        "is_code_fence": getattr(section, "is_code_fence", False),
        "chars": len(section.content),
        "bytes": len(section.content.encode("utf-8", errors="replace")),
        "tokens_estimate": len(section.content.split()),
        "json_shape": _json_shape(section.content),
        "content": section.content,
    }


def _detect_content(content: str) -> DetectionResult:
    """Detect content type via a two-stage Rust-primary / Python-backstop chain.

    Stage 1 (primary): `headroom._core.detect_content_type` (the Rust
    detection chain) classifies the content. Its non-``PLAIN_TEXT``
    verdicts are authoritative and are never overridden.

    Stage 2 (backstop): when — and only when — Rust returns
    ``PLAIN_TEXT``, the Python regex detector
    (`content_detector.detect_content_type`) gets a second look. If it
    recognises a structured type that Rust read as plain text (e.g. a
    ripgrep block, or a JSON array the Rust tier let through as text),
    the Python result wins and changes routing. This is a parity
    backstop for the cases where the two regex engines (`re` vs Rust's
    `regex` crate) disagree on a boundary input — it is a real, live
    parallel path, not a retired one.

    The consensus rule (Rust primary; Python backstop ONLY on a
    Rust-``PLAIN_TEXT`` divergence) is pinned by the parity tests in
    ``test_transforms_content_router.py``. Removing the Stage-2 branch
    below silently re-routes every input Rust under-classifies as plain
    text — keep those tests green if you touch it.

    The Rust binding returns the legacy `DetectionResult` shape with
    `confidence=1.0` and an empty metadata dict. Downstream callers only
    consume `.content_type`; the strategy mapping in
    `_strategy_from_detection` keys off that field alone.
    """
    from headroom._core import detect_content_type as _rust_detect

    rust_result = _rust_detect(content)
    # Rust's `content_type` is the lowercase string tag (e.g.
    # "json_array"); translate to the Python `ContentType` enum so
    # downstream mapping keys match. An unrecognised tag (version skew
    # between the Rust detector and this enum) maps to PLAIN_TEXT — the safe
    # routing default — rather than raising a ValueError mid-pipeline. Total
    # function: no exception-as-control-flow across the FFI boundary.
    try:
        content_type = ContentType(rust_result.content_type)
    except ValueError:
        logger.warning(
            "unknown content_type tag %r from Rust detector; routing as PLAIN_TEXT",
            rust_result.content_type,
        )
        content_type = ContentType.PLAIN_TEXT
    if content_type is ContentType.PLAIN_TEXT:
        regex_result = _regex_detect_content_type(content)
        if regex_result.content_type is not ContentType.PLAIN_TEXT:
            return regex_result
    return DetectionResult(
        content_type=content_type,
        confidence=rust_result.confidence,
        metadata={},
    )


def _create_content_signature(
    content_type: str,
    content: str,
    language: str | None = None,
) -> ToolSignature | None:
    """Create a ToolSignature for non-JSON content types.

    This allows TOIN to track compression patterns for code, search results,
    logs, and text - not just JSON arrays.

    Args:
        content_type: The type of content (e.g., "code_aware", "search", "log", "text").
        content: The content being compressed (for structural hints).
        language: Optional language hint for code.

    Returns:
        A ToolSignature for TOIN tracking.
    """
    try:
        from ..telemetry.models import ToolSignature

        # Create a deterministic structure hash based on content type
        # This groups similar content types together for pattern learning
        if language:
            hash_input = f"content:{content_type}:{language}"
        else:
            hash_input = f"content:{content_type}"

        # Add a structural hint from the content (first 100 chars, hashed)
        # This helps differentiate tool outputs of the same type
        content_sample = content[:100] if content else ""
        structure_hint = hashlib.sha256(content_sample.encode()).hexdigest()[:8]
        hash_input = f"{hash_input}:{structure_hint}"

        # Keep SHA256: structure_hash feeds into TOIN which persists to disk.
        # Changing hash function would invalidate all learned patterns.
        structure_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:24]

        return ToolSignature(
            structure_hash=structure_hash,
            field_count=0,  # Not applicable for non-JSON
            has_nested_objects=False,
            has_arrays=False,
            max_depth=0,
        )
    except ImportError:
        return None


@dataclass
class RoutingDecision:
    """Record of a single routing decision."""

    content_type: ContentType
    strategy: CompressionStrategy
    original_tokens: int
    compressed_tokens: int
    confidence: float = 1.0
    section_index: int = 0

    @property
    def compression_ratio(self) -> float:
        if self.original_tokens == 0:
            return 1.0
        return self.compressed_tokens / self.original_tokens


@dataclass
class RouterCompressionResult:
    """Result from ContentRouter with routing metadata.

    Attributes:
        compressed: The compressed content.
        original: Original content before compression.
        strategy_used: Primary strategy used for compression.
        routing_log: List of routing decisions made.
        sections_processed: Number of content sections processed.
        strategy_chain: Every strategy attempted in order. For a direct
            hit it's a single entry; for the SMART_CRUSHER → KOMPRESS →
            LOG fallback chain it's three. Lets log readers see *how*
            we got to the final compressor without parsing the
            decision_reason string.
    """

    compressed: str
    original: str
    strategy_used: CompressionStrategy
    routing_log: list[RoutingDecision] = field(default_factory=list)
    sections_processed: int = 1
    strategy_chain: list[str] = field(default_factory=list)

    @property
    def total_original_tokens(self) -> int:
        """Total tokens before compression."""
        return sum(r.original_tokens for r in self.routing_log)

    @property
    def total_compressed_tokens(self) -> int:
        """Total tokens after compression."""
        return sum(r.compressed_tokens for r in self.routing_log)

    @property
    def compression_ratio(self) -> float:
        """Overall compression ratio."""
        if self.total_original_tokens == 0:
            return 1.0
        return self.total_compressed_tokens / self.total_original_tokens

    @property
    def tokens_saved(self) -> int:
        """Number of tokens saved."""
        return max(0, self.total_original_tokens - self.total_compressed_tokens)

    @property
    def savings_percentage(self) -> float:
        """Percentage of tokens saved."""
        if self.total_original_tokens == 0:
            return 0.0
        return (self.tokens_saved / self.total_original_tokens) * 100

    def summary(self) -> str:
        """Human-readable routing summary."""
        if self.strategy_used == CompressionStrategy.MIXED:
            strategies = {r.strategy.value for r in self.routing_log}
            return (
                f"Mixed content: {self.sections_processed} sections, "
                f"routed to {strategies}. "
                f"{self.total_original_tokens:,}→{self.total_compressed_tokens:,} tokens "
                f"({self.savings_percentage:.0f}% saved)"
            )
        else:
            return (
                f"Pure {self.strategy_used.value}: "
                f"{self.total_original_tokens:,}→{self.total_compressed_tokens:,} tokens "
                f"({self.savings_percentage:.0f}% saved)"
            )


@dataclass
class ContentRouterConfig:
    """Configuration for intelligent content routing.

    Attributes:
        enable_smart_crusher: Enable JSON array compression.
        enable_search_compressor: Enable search result compression.
        enable_log_compressor: Enable build/test log compression.
        prefer_code_aware_for_code: Use CodeAware over Kompress for code.
        mixed_content_threshold: Min distinct types to consider "mixed".
        min_section_tokens: Minimum tokens for a section to compress.
        fallback_strategy: Strategy when no compressor matches.
        skip_user_messages: Never compress user messages (they're the subject).
        skip_recent_messages: Don't compress last N messages (likely the subject).
        protect_analysis_context: Detect "analyze/review" intent, skip compression.
    """

    # Enable/disable specific compressors
    enable_kompress: bool = True  # Kompress: ModernBERT token compressor
    enable_smart_crusher: bool = True
    enable_search_compressor: bool = True
    enable_log_compressor: bool = True
    enable_html_extractor: bool = True  # HTML content extraction

    # Routing preferences
    prefer_code_aware_for_code: bool = False  # Disabled: let code pass through unmangled
    mixed_content_threshold: int = 2  # Min types to consider mixed
    min_section_tokens: int = 20  # Min tokens to compress a section

    # Fallback: Kompress handles unknown/mixed content instead of passing through
    fallback_strategy: CompressionStrategy = CompressionStrategy.KOMPRESS

    # Protection: Don't compress content that's likely the subject of analysis
    skip_user_messages: bool = True  # User messages contain what they want analyzed
    protect_recent_code: int = 4  # Don't compress CODE in last N messages (0 = disabled)
    protect_analysis_context: bool = True  # Detect "analyze/review" intent, protect code

    # Protection: failed tool calls / error outputs stay verbatim.
    # The model needs exact tracebacks and error text to recover; compressing
    # them measurably hurts agent recovery. Outputs above the size cap still
    # compress — LogCompressor preserves error lines in big logs, so the two
    # features stay complementary.
    protect_error_outputs: bool = True
    error_protection_max_chars: int = 8000  # ~2K tokens; larger errors compress

    # Cache safety: assistant text-block compression.
    # Default OFF. Assistant content is echoed back by the client in
    # subsequent turns and becomes part of the upstream provider's
    # prefix cache (Anthropic cache_control, DeepSeek/OpenAI auto).
    # Compressing it changes the bytes that must match for a cache
    # hit on the next turn. The hash-keyed result cache makes the
    # compressed output deterministic *within* a process, but cache
    # eviction or process restart can re-compress with a different
    # output for stochastic compressors — and that miss costs the
    # whole prefix discount. Enable only for deployments routed to
    # backends that don't honor cache_control AND whose compressors
    # are byte-deterministic.
    compress_assistant_text_blocks: bool = False

    # Minimum content length (in chars) at which a text or tool_result
    # block is considered for compression. Below this, the overhead of
    # routing/detecting/caching exceeds any savings, so the block is
    # passed through verbatim.
    min_chars_for_block_compression: int = 500

    # Adaptive Read protection: fraction of total messages to protect from
    # compression.  At 10 msgs, protects ~5 Reads.  At 100 msgs, protects ~10.
    # Old Reads beyond this window become compressible even though they are
    # in DEFAULT_EXCLUDE_TOOLS.  0.0 = always exclude all (old behavior).
    protect_recent_reads_fraction: float = (
        0.0  # 0.0 = protect ALL excluded-tool outputs (safest for coding agents)
    )

    # Adaptive compression ratio: scales with context pressure.
    # A compression is ACCEPTED when its ratio is strictly below min_ratio
    # (see the `ratio < min_ratio` gate); a higher min_ratio therefore accepts
    # MORE compressions (including marginal ones).
    # At low pressure (mostly-empty context), use the relaxed threshold — keep
    # accepting only worthwhile compressions (reject marginal).
    # At high pressure (nearly-full context), use the aggressive threshold —
    # accept anything that helps, so the agent doesn't overflow exactly when
    # context is tightest. Aggressive is therefore the HIGHER (more-permissive)
    # threshold — an aggressive value below the relaxed one would REJECT
    # marginal compressions at high pressure, the opposite of the intent.
    min_ratio_relaxed: float = 0.85  # when context is mostly empty (stricter)
    min_ratio_aggressive: float = 0.95  # when context is nearly full (permissive)

    # CCR (Compress-Cache-Retrieve) settings for SmartCrusher
    ccr_enabled: bool = True  # Enable CCR marker injection for reversible compression
    ccr_inject_marker: bool = True  # Add retrieval markers to compressed content
    smart_crusher_max_items_after_crush: int | None = None
    smart_crusher_with_compaction: bool = True
    # Routing policy for the lossless-vs-lossy-recoverable choice (both
    # recoverable, so no information is lost). ``"min-tokens"`` (default)
    # ships whichever render is fewer tokens; ``"lossless-first"`` keeps
    # the legacy lossless-wins-on-byte-ratio behavior.
    smart_crusher_routing_policy: str = "min-tokens"

    # Tag protection: preserve custom/workflow XML tags from text compression.
    # When False (default), entire <custom-tag>content</custom-tag> blocks are
    # protected verbatim.  When True, only the tag markers are protected and
    # the content between them can be compressed.
    compress_tagged_content: bool = False

    # Tools to exclude from compression (output passed through unmodified)
    # Set to None to use DEFAULT_EXCLUDE_TOOLS, or provide custom set
    exclude_tools: set[str] | None = None

    # Read lifecycle management (stale/superseded detection)
    read_lifecycle: ReadLifecycleConfig = field(default_factory=ReadLifecycleConfig)

    # Per-tool compression profiles (tool_name → CompressionProfile)
    # Set to None to use DEFAULT_TOOL_PROFILES from config
    tool_profiles: dict[str, Any] | None = None


# Strict allow-list for ``ContentRouter.apply(**kwargs)``. A key absent here is
# rejected with a TypeError so a typo (e.g. ``protect_recents`` for
# ``protect_recent``) fails loudly instead of being silently dropped.
#
# The set is the union of TWO sources:
#   1. Keys apply() itself READS — directly via ``kwargs.get(...)`` and
#      indirectly via ``RouterRuntime.from_kwargs`` (``target_ratio`` /
#      ``force_kompress`` / ``kompress_model``).
#   2. Keys a real caller PASSES but apply() never reads. The pipeline
#      broadcasts the SAME ``**kwargs`` to every transform
#      (``pipeline.py``: ``transform.apply(..., **kwargs)``), so apply()
#      legitimately RECEIVES keys destined for the pipeline's public surface
#      or for sibling transforms — e.g. ``model_limit`` / ``output_buffer`` /
#      ``tool_profiles`` / ``request_id`` (documented in
#      ``TransformPipeline.apply``) and ``record_metrics`` /
#      ``model`` / ``messages`` / ``tokenizer`` (positionals / dry-run marker).
#      These are valid, just not consumed here.
_APPLY_ALLOWED_KWARGS: frozenset[str] = frozenset(
    {
        # --- read by apply() directly ---
        "compression_store",
        "frozen_message_count",
        "compress_user_messages",
        "compress_system_messages",
        "protect_recent",
        "protect_analysis_context",
        "compress_request",
        "min_tokens_to_compress",
        "compress_assistant_text_blocks",
        "min_chars_for_block_compression",
        "context",
        "biases",
        "model_limit",
        "read_protection_window",
        # --- read by apply() via RouterRuntime.from_kwargs ---
        "target_ratio",
        "force_kompress",
        "kompress_model",
        # --- received via the pipeline broadcast but not read here ---
        # (pipeline public surface + sibling transforms + positionals) ---
        "model",
        "messages",
        "tokenizer",
        "output_buffer",
        "tool_profiles",
        "request_id",
        "record_metrics",
    }
)


class ContentRouter(Transform):
    """Intelligent router that selects optimal compression strategy.

    ContentRouter is the recommended entry point for Headroom's compression.
    It analyzes content and routes it to the most appropriate compressor,
    handling mixed content by splitting and reassembling.

    Key Features:
    - Automatic content type detection
    - Source hint support for high-confidence routing
    - Mixed content handling (split → route → reassemble)
    - Graceful fallback when compressors unavailable
    - Rich routing metadata for debugging

    Example:
        >>> router = ContentRouter()
        >>>
        >>> # Source code routes to Kompress (ML-based)
        >>> result = router.compress(python_code)
        >>> print(result.strategy_used)  # CompressionStrategy.KOMPRESS
        >>>
        >>> # Automatically uses SmartCrusher
        >>> result = router.compress(json_array)
        >>> print(result.strategy_used)  # CompressionStrategy.SMART_CRUSHER
        >>>
        >>> # Splits and routes each section
        >>> result = router.compress(readme_with_code)
        >>> print(result.strategy_used)  # CompressionStrategy.MIXED

    Pipeline Integration:
        >>> pipeline = TransformPipeline([
        ...     ContentRouter(),   # Handles ALL content types
        ... ])
    """

    name: str = "content_router"

    def __init__(
        self,
        config: ContentRouterConfig | None = None,
        observer: Any = None,
    ):
        """Initialize content router.

        Args:
            config: Router configuration. Uses defaults if None.
            observer: Optional `CompressionObserver` (see
                `headroom.transforms.observability`) called once per
                routing decision after `compress()` finishes.
                `PrometheusMetrics` is the production
                implementation — it increments per-strategy counters
                so silent regressions become visible. `None` disables
                observation; pick one explicitly per the no-fallback
                rule in the audit doc.
        """
        self.config = config or ContentRouterConfig()
        self._observer = observer

        # Lazy-loaded compressors.
        #
        # The five SELF-CONTAINED factories (SmartCrusher, Search, Log, Diff,
        # HTML) read only ``self.config`` and cache their instance — they live
        # in ``CompressorRegistry`` now. The ``_get_*`` methods below delegate
        # to it. ``_kompress`` STAYS here: ``_get_kompress`` takes the
        # per-request ``model_id`` (from ``runtime.kompress_model``), so it is
        # not self-contained and must not move into the registry.
        self._registry = CompressorRegistry(self.config)
        # Per-strategy dispatch + no-savings fallback chain. Holds no router
        # reference: the compressor getters and the two router-bound callables
        # (``_try_kompress`` / ``_record_to_toin``) are passed per-call by
        # the ``_apply_strategy_to_content`` delegator, so monkeypatching those
        # router methods still takes effect. Only the lifetime-stable deps
        # (config + the module-level debug helpers/logger) ride the constructor.
        self._dispatcher = StrategyDispatcher(
            self.config,
            logger=logger,
            log_router_debug=_log_router_debug,
            json_shape=_json_shape,
        )
        # CCR-backing seam for the result-cache HIT path. Holds no router
        # reference: the SmartCrusher getter is passed per-call by the
        # ``_ensure_ccr_backed`` delegator (resolving ``self._get_smart_crusher``
        # fresh), so monkeypatching that getter still bites. Only the
        # lifetime-stable ``logger`` rides the constructor.
        self._ccr_mirror = CcrMirror(logger=logger)
        self._kompress: Any = None

        # TOIN integration for cross-strategy learning
        self._toin: Any = None

        # Per-request runtime options (target_ratio / force_kompress /
        # kompress_model) are NOT router state. They are
        # carried per call as a frozen ``RouterRuntime`` value passed down the
        # call chain (``compress(..., runtime=...)``), so concurrent requests
        # on this shared instance are isolated structurally — no thread-local,
        # no main->worker replay. Worker threads receive the same frozen
        # instance by value.
        self._cache = CompressionCache()

    def _record_to_toin(
        self,
        strategy: CompressionStrategy,
        content: str,
        compressed: str,
        original_tokens: int,
        compressed_tokens: int,
        language: str | None = None,
        context: str = "",
    ) -> None:
        """Record compression to TOIN for cross-user learning.

        This allows TOIN to track compression patterns for ALL content types,
        not just JSON arrays. When the LLM retrieves original content via CCR,
        TOIN learns which compressions users need to expand.

        Args:
            strategy: The compression strategy used.
            content: Original content (for signature generation).
            compressed: Compressed content.
            original_tokens: Token count before compression.
            compressed_tokens: Token count after compression.
            language: Optional language hint for code.
            context: Query context for pattern learning.
        """
        # Skip SmartCrusher - it handles its own TOIN recording
        if strategy == CompressionStrategy.SMART_CRUSHER:
            return

        # Skip if no actual compression happened
        if original_tokens <= compressed_tokens:
            return

        try:
            # Lazy load TOIN
            if self._toin is None:
                from ..telemetry.toin import get_toin

                self._toin = get_toin()

            # Create a content-type signature
            signature = _create_content_signature(
                content_type=strategy.value,
                content=content,
                language=language,
            )

            if signature is None:
                return

            # Record the compression
            self._toin.record_compression(
                tool_signature=signature,
                original_count=1,  # Single content block
                compressed_count=1,
                original_tokens=original_tokens,
                compressed_tokens=compressed_tokens,
                strategy=strategy.value,
                query_context=context if context else None,
            )

            logger.debug(
                "TOIN: Recorded %s compression: %d → %d tokens",
                strategy.value,
                original_tokens,
                compressed_tokens,
            )

        except Exception as e:
            # TOIN recording should never break compression
            logger.debug("TOIN recording failed (non-fatal): %s", e)

    def _timed_compress(
        self,
        content: str,
        context: str,
        bias: float,
        runtime: RouterRuntime = _DEFAULT_RUNTIME,
    ) -> tuple[RouterCompressionResult, float]:
        """Compress with wall-clock timing.  Used by parallel executor.

        Per-request options ride the frozen ``runtime`` value, passed by
        argument from the main thread. Worker threads receive the SAME
        immutable instance by value — there is no thread-local to replay, so
        ``force_kompress`` / ``target_ratio`` / ``kompress_model`` reach
        every worker structurally.
        """
        t0 = time.perf_counter()
        result = self.compress(content, context=context, bias=bias, runtime=runtime)
        return result, (time.perf_counter() - t0) * 1000

    def compress(
        self,
        content: str,
        context: str = "",
        question: str | None = None,
        bias: float = 1.0,
        *,
        runtime: RouterRuntime = _DEFAULT_RUNTIME,
    ) -> RouterCompressionResult:
        """Compress content using optimal strategy based on content detection.

        Args:
            content: Content to compress.
            context: Optional context for relevance-aware compression.
            question: Optional question for QA-aware compression. When provided,
                tokens relevant to answering this question are preserved.
            bias: Compression bias multiplier (>1 = keep more, <1 = keep fewer).
            runtime: Frozen per-request options (``target_ratio`` /
                ``force_kompress`` / ``kompress_model``). Defaults to the
                shared ``_DEFAULT_RUNTIME`` for direct callers that pass no
                options.

        Returns:
            RouterCompressionResult with compressed content and routing metadata.
        """
        debug_enabled = logger.isEnabledFor(logging.DEBUG)
        request_debug = (
            {
                "chars": len(content),
                "bytes": len(content.encode("utf-8", errors="replace")),
                "tokens_estimate": len(content.split()),
                "json_shape": _json_shape(content),
                "mixed_indicators": _mixed_indicators(content),
                "context_chars": len(context),
                "question": question,
                "bias": bias,
                "content": content,
                "context": context,
            }
            if debug_enabled
            else {}
        )
        if not content or not content.strip():
            if debug_enabled:
                _log_router_debug(
                    "content_router_input",
                    **request_debug,
                    selected_strategy=CompressionStrategy.PASSTHROUGH.value,
                    selection_reason="empty_or_whitespace",
                )
            result = RouterCompressionResult(
                compressed=content,
                original=content,
                strategy_used=CompressionStrategy.PASSTHROUGH,
                routing_log=[],
            )
        else:
            # Determine strategy from content analysis. ``_determine_strategy``
            # already runs ``is_mixed_content`` + ``_detect_content`` (a Rust
            # FFI round-trip) internally — on the hot path (debug off) we do NOT
            # recompute them here. The detection locals below are built only for
            # the debug log, so the per-call detection cost is paid once.
            force_kompress = bool(runtime.force_kompress)
            strategy = (
                CompressionStrategy.KOMPRESS
                if force_kompress
                else self._determine_strategy(content)
            )
            if debug_enabled:
                mixed = is_mixed_content(content)
                detection = _detect_content(content)
                _log_router_debug(
                    "content_router_input",
                    **request_debug,
                    detected_content_type=detection.content_type.value,
                    detection_confidence=detection.confidence,
                    selected_strategy=strategy.value,
                    selection_reason=(
                        "runtime_force_kompress"
                        if force_kompress
                        else "mixed_content"
                        if mixed
                        else "content_detection"
                    ),
                )

            if strategy == CompressionStrategy.MIXED:
                result = self._compress_mixed(
                    content, context, question, bias=bias, runtime=runtime
                )
            else:
                result = self._compress_pure(
                    content, strategy, context, question, bias=bias, runtime=runtime
                )

        # Empty-output guard: compression must NEVER blank out non-empty input.
        # An empty user-message content makes Anthropic reject the whole request
        # with 400 ("messages.N: user messages must have non-empty content").
        # If any transform yields empty/whitespace from non-empty input, fall
        # back to the original content (passthrough) instead of emitting empty.
        if (
            content
            and content.strip()
            and (result.compressed is None or not str(result.compressed).strip())
        ):
            logger.warning(
                "content_router: compression produced EMPTY output from non-empty "
                "input (%d chars, strategy=%s); falling back to original to avoid 400.",
                len(content),
                getattr(result.strategy_used, "value", result.strategy_used),
            )
            result.compressed = content
            # This is a PASSTHROUGH — the output is the full original.
            # The metrics (tokens_saved / compression_ratio / savings_percentage)
            # are computed by summing routing_log[].compressed_tokens, so leaving
            # the empty-output decisions in place reported phantom savings
            # (tokens_saved=N, ratio 0.0) for content we did NOT actually shrink.
            # Rewrite each decision to passthrough (compressed == original) so the
            # routing_log and every derived metric honestly report saved=0,
            # ratio=1.0.
            result.routing_log = [
                replace(decision, compressed_tokens=decision.original_tokens)
                for decision in result.routing_log
            ]

        # One observer call per routing decision; the observer is the
        # forcing function for catching strategy-level regressions.
        # Empty routing_log (passthrough fast path) → no calls.
        self._observe(result)
        if debug_enabled:
            _log_router_debug(
                "content_router_output",
                selected_strategy=result.strategy_used.value,
                sections_processed=result.sections_processed,
                total_original_tokens=result.total_original_tokens,
                total_compressed_tokens=result.total_compressed_tokens,
                tokens_saved=result.tokens_saved,
                savings_percentage=result.savings_percentage,
                compression_ratio=result.compression_ratio,
                routing_log=[
                    {
                        "content_type": decision.content_type.value,
                        "strategy": decision.strategy.value,
                        "original_tokens": decision.original_tokens,
                        "compressed_tokens": decision.compressed_tokens,
                        "confidence": decision.confidence,
                        "section_index": decision.section_index,
                        "compression_ratio": decision.compression_ratio,
                    }
                    for decision in result.routing_log
                ],
                original=result.original,
                compressed=result.compressed,
            )
        return result

    def _observe(self, result: RouterCompressionResult) -> None:
        """Forward each `RoutingDecision` in `result.routing_log` to the
        configured `CompressionObserver`. No-op when no observer is set.

        Observers MUST NOT raise per the protocol contract; if one does
        anyway, swallow at debug level. Compression already succeeded;
        a buggy observer must not turn a 200 into a 500.
        """
        if self._observer is None:
            return
        for d in result.routing_log:
            try:
                self._observer.record_compression(
                    strategy=d.strategy.value,
                    original_tokens=d.original_tokens,
                    compressed_tokens=d.compressed_tokens,
                )
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("CompressionObserver raised (non-fatal): %s", e)

    def _determine_strategy(self, content: str) -> CompressionStrategy:
        """Determine the compression strategy from content analysis.

        Args:
            content: Content to analyze.

        Returns:
            Selected compression strategy.
        """
        # 1. Check for mixed content
        if is_mixed_content(content):
            return CompressionStrategy.MIXED

        # 2. Detect content type from content itself
        detection = _detect_content(content)
        return self._strategy_from_detection(detection)

    def _strategy_from_detection(self, detection: Any) -> CompressionStrategy:
        """Get strategy from content detection result.

        Thin delegator to the pure :func:`router_policy.strategy_from_detection`.
        """
        return strategy_from_detection(self.config, detection)

    def _compress_mixed(
        self,
        content: str,
        context: str,
        question: str | None = None,
        bias: float = 1.0,
        *,
        runtime: RouterRuntime = _DEFAULT_RUNTIME,
    ) -> RouterCompressionResult:
        """Compress mixed content by splitting and routing sections.

        Args:
            content: Mixed content to compress.
            context: User context for relevance.
            question: Optional question for QA-aware compression.
            bias: Compression bias multiplier.

        Returns:
            RouterCompressionResult with reassembled content.
        """
        sections = split_into_sections(content)
        if logger.isEnabledFor(logging.DEBUG):
            _log_router_debug(
                "content_router_mixed_sections",
                section_count=len(sections),
                sections=[_section_debug(section, idx) for idx, section in enumerate(sections)],
                content=content,
            )

        if not sections:
            return RouterCompressionResult(
                compressed=content,
                original=content,
                strategy_used=CompressionStrategy.PASSTHROUGH,
            )

        compressed_sections: list[str] = []
        routing_log: list[RoutingDecision] = []

        for i, section in enumerate(sections):
            # Get strategy for this section
            strategy = self._strategy_from_detection_type(section.content_type)

            # Compress section
            original_tokens = len(section.content.split())
            compressed_content, compressed_tokens, _section_chain = self._apply_strategy_to_content(
                section.content,
                strategy,
                context,
                section.language,
                question,
                bias=bias,
                runtime=runtime,
            )

            # Preserve code fence markers
            if section.is_code_fence and section.language:
                compressed_content = f"```{section.language}\n{compressed_content}\n```"

            compressed_sections.append(compressed_content)
            routing_log.append(
                RoutingDecision(
                    content_type=section.content_type,
                    strategy=strategy,
                    original_tokens=original_tokens,
                    compressed_tokens=compressed_tokens,
                    section_index=i,
                )
            )

        return RouterCompressionResult(
            compressed="\n\n".join(compressed_sections),
            original=content,
            strategy_used=CompressionStrategy.MIXED,
            routing_log=routing_log,
            sections_processed=len(sections),
        )

    def _compress_pure(
        self,
        content: str,
        strategy: CompressionStrategy,
        context: str,
        question: str | None = None,
        bias: float = 1.0,
        *,
        runtime: RouterRuntime = _DEFAULT_RUNTIME,
    ) -> RouterCompressionResult:
        """Compress pure (non-mixed) content.

        Args:
            content: Content to compress.
            strategy: Selected strategy.
            context: User context.
            question: Optional question for QA-aware compression.
            bias: Compression bias multiplier.

        Returns:
            RouterCompressionResult.
        """
        original_tokens = len(content.split())

        compressed, compressed_tokens, strategy_chain = self._apply_strategy_to_content(
            content, strategy, context, question=question, bias=bias, runtime=runtime
        )

        return RouterCompressionResult(
            compressed=compressed,
            original=content,
            strategy_used=strategy,
            strategy_chain=strategy_chain,
            routing_log=[
                RoutingDecision(
                    content_type=self._content_type_from_strategy(strategy),
                    strategy=strategy,
                    original_tokens=original_tokens,
                    compressed_tokens=compressed_tokens,
                )
            ],
        )

    def _apply_strategy_to_content(
        self,
        content: str,
        strategy: CompressionStrategy,
        context: str,
        language: str | None = None,
        question: str | None = None,
        bias: float = 1.0,
        *,
        runtime: RouterRuntime = _DEFAULT_RUNTIME,
    ) -> tuple[str, int, list[str]]:
        """Apply a compression strategy to content.

        Thin delegator to :meth:`StrategyDispatcher.apply`. The compressor
        getters and the two router-bound callables (``_try_kompress`` /
        ``_record_to_toin``) are resolved fresh here on every call and passed
        in, so monkeypatching those router methods still takes effect (a
        construction-time capture in the dispatcher would have been stale).

        The per-request ``runtime`` is bound into the ``try_kompress``
        closure here rather than added to the dispatcher's signature: the
        dispatcher stays a pure leaf that calls it as an opaque callable, and
        ``runtime`` rides the closure to the site that reads it
        (``target_ratio`` / ``kompress_model`` for ML). Because the closures
        forward to ``self._try_kompress`` / ``self._record_to_toin``,
        monkeypatching those methods still bites.

        Args:
            content: Content to compress.
            strategy: Strategy to use.
            context: User context.
            language: Language hint for code.
            question: Optional question for QA-aware compression.
            bias: Compression bias multiplier (>1 = keep more, <1 = keep fewer).
            runtime: Frozen per-request options bound into the ML/TOIN closures.

        Returns:
            Tuple of (compressed_content, compressed_token_count,
            strategy_chain). The chain lists every strategy attempted
            in order — first the requested one, then any fallbacks.
            Single-entry chain means a direct hit; multi-entry means
            the fallback chain fired (e.g. ``[smart_crusher, kompress,
            log]``). Log readers use this to see *how* we got to the
            final compressor without parsing decision_reason strings.
        """

        def try_kompress(
            ml_content: str, ml_context: str, ml_question: str | None
        ) -> tuple[str, int]:
            return self._try_kompress(ml_content, ml_context, ml_question, runtime=runtime)

        def record_to_toin(**kwargs: Any) -> None:
            self._record_to_toin(**kwargs)

        return self._dispatcher.apply(
            content,
            strategy,
            context,
            language,
            question,
            bias,
            get_smart_crusher=self._get_smart_crusher,
            get_search_compressor=self._get_search_compressor,
            get_log_compressor=self._get_log_compressor,
            get_diff_compressor=self._get_diff_compressor,
            get_html_extractor=self._get_html_extractor,
            try_kompress=try_kompress,
            record_to_toin=record_to_toin,
        )

    def _try_kompress(
        self,
        content: str,
        context: str,
        question: str | None = None,
        *,
        runtime: RouterRuntime = _DEFAULT_RUNTIME,
    ) -> tuple[str, int]:
        """ML-based compression using Kompress.

        Kompress (ModernBERT, trained on 330K structured tool outputs)
        auto-downloads from HuggingFace on first use. No heuristic fallback.

        Custom/workflow XML tags (<system-reminder>, <tool_call>, <thinking>)
        are protected before compression and restored after.  Standard HTML
        tags are left alone (HTMLExtractor handles those separately).

        Args:
            content: Content to compress.
            context: User context.
            question: Optional question for QA-aware compression.

        Returns:
            Tuple of (compressed, token_count).
        """
        from .kompress_compressor import _MODEL_UNAVAILABLE_ERRORS
        from .tag_protector import protect_tags, restore_tags

        # Protect custom tags before any ML compression
        cleaned, protected = protect_tags(
            content,
            compress_tagged_content=self.config.compress_tagged_content,
        )

        # If the entire content is custom tags with nothing to compress
        if protected and not cleaned.strip():
            return content, len(content.split())

        # Use the cleaned (tag-free) text for compression
        text_to_compress = cleaned if protected else content
        compressed: str | None = None
        compressed_tokens: int | None = None

        # Primary: Kompress — downloads from chopratejas/kompress-v2-base on first use
        if self.config.enable_kompress:
            compressor = self._get_kompress(runtime.kompress_model)
            if compressor:
                try:
                    result = compressor.compress(
                        text_to_compress,
                        context=context,
                        question=question,
                        target_ratio=runtime.target_ratio,
                    )
                    compressed = result.compressed
                    compressed_tokens = result.compressed_tokens
                except _MODEL_UNAVAILABLE_ERRORS as e:
                    # Model/runtime unavailable — a legitimate graceful
                    # passthrough. Any other exception is a Kompress bug (#4)
                    # and must propagate so it is not silently swallowed here.
                    # The tuple is single-sourced from kompress_compressor so it
                    # cannot drift from compress()'s own passthrough contract.
                    logger.warning("Kompress unavailable, passthrough: %s", e)

        if compressed is None:
            return content, len(content.split())

        # Restore protected tag blocks into the compressed text
        if protected:
            compressed = restore_tags(compressed, protected)
            compressed_tokens = len(compressed.split())

        return compressed, compressed_tokens or len(compressed.split())

    def _strategy_from_detection_type(self, content_type: ContentType) -> CompressionStrategy:
        """Get strategy from ContentType enum.

        Thin delegator to :func:`router_policy.strategy_from_detection_type`.
        """
        return strategy_from_detection_type(self.config, content_type)

    def _content_type_from_strategy(self, strategy: CompressionStrategy) -> ContentType:
        """Get ContentType from strategy.

        Thin delegator to :func:`router_policy.content_type_from_strategy`.
        """
        return content_type_from_strategy(strategy)

    # Lazy compressor getters

    def _get_smart_crusher(self) -> Any:
        """Get SmartCrusher (lazy load) with CCR config.

        Thin delegator to :meth:`CompressorRegistry.get_smart_crusher`.
        """
        return self._registry.get_smart_crusher()

    def _ensure_ccr_backed(self, cached_compressed: str, context: str) -> bool:
        """Ensure every ``<<ccr:HASH>>`` pointer in *cached_compressed* resolves
        in the Python ``compression_store`` (the store ``/v1/retrieve`` reads).

        Thin delegator to :meth:`CcrMirror.ensure_ccr_backed`. The SmartCrusher
        getter is resolved fresh here on every call and passed in, so
        monkeypatching ``self._get_smart_crusher`` (or the underlying registry)
        still takes effect — a construction-time capture in the mirror would
        have been stale. Kept as an instance method for the test/back-compat
        seam: the single result-cache HIT path (in ``_lookup_cached_disposition``)
        calls ``self._ensure_ccr_backed``.
        """
        return self._ccr_mirror.ensure_ccr_backed(
            cached_compressed,
            context,
            get_smart_crusher=self._get_smart_crusher,
        )

    @staticmethod
    def _extract_ccr_hashes(text: str) -> set[str]:
        """Collect every distinct ``<<ccr:HASH...>>`` hash in *text*.

        Thin delegator to :meth:`CcrMirror.extract_ccr_hashes` (kept as a
        static back-compat seam).
        """
        return CcrMirror.extract_ccr_hashes(text)

    def _get_search_compressor(self) -> Any:
        """Get SearchCompressor (lazy load).

        Thin delegator to :meth:`CompressorRegistry.get_search_compressor`.
        """
        return self._registry.get_search_compressor()

    def _get_log_compressor(self) -> Any:
        """Get LogCompressor (lazy load).

        Thin delegator to :meth:`CompressorRegistry.get_log_compressor`.
        """
        return self._registry.get_log_compressor()

    def _get_diff_compressor(self) -> Any:
        """Get DiffCompressor (lazy load).

        Thin delegator to :meth:`CompressorRegistry.get_diff_compressor`.
        """
        return self._registry.get_diff_compressor()

    def _get_html_extractor(self) -> Any:
        """Get HTMLExtractor (lazy load).

        Thin delegator to :meth:`CompressorRegistry.get_html_extractor`.
        """
        return self._registry.get_html_extractor()

    def _get_kompress(self, model_id: str | None = None) -> KompressCompressor | None:
        """Get KompressCompressor (lazy load). Downloads from HuggingFace on first use.

        Respects the per-request ``model_id`` (from ``runtime.kompress_model``):
        - None: use default (chopratejas/kompress-v2-base) — cached on self
        - "disabled": return None (skip ML compression entirely)
        - any model ID string: create compressor with that model
          (model weights are cached at module level in kompress_compressor.py,
          so repeated calls with the same model_id are cheap)
        """
        # Explicitly disabled — no ML compression
        if model_id == "disabled":
            return None

        # Custom model — don't touch self._kompress (that's the default cache)
        if model_id:
            try:
                from .kompress_compressor import (
                    KompressCompressor,
                    KompressConfig,
                    is_kompress_available,
                )

                if is_kompress_available():
                    return KompressCompressor(config=KompressConfig(model_id=model_id))
            except ImportError:
                pass
            return None

        # Default path — exactly as before, cached on self
        if self._kompress is None:
            try:
                from .kompress_compressor import KompressCompressor, is_kompress_available

                if is_kompress_available():
                    self._kompress = KompressCompressor()
            except ImportError:
                logger.debug("Kompress dependencies not available")
        return self._kompress

    # Transform interface

    def _build_tool_name_map(self, messages: list[dict[str, Any]]) -> dict[str, str]:
        """Build mapping from tool_call_id to tool_name.

        Scans assistant messages to find tool calls and extract their names.
        Supports both OpenAI and Anthropic message formats.
        """
        mapping: dict[str, str] = {}

        for msg in messages:
            if msg.get("role") != "assistant":
                continue

            # OpenAI format: tool_calls array
            for tc in msg.get("tool_calls", []):
                if isinstance(tc, dict):
                    tc_id = tc.get("id", "")
                    name = tc.get("function", {}).get("name", "")
                    if tc_id and name:
                        mapping[tc_id] = name

            # Anthropic format: content blocks with type=tool_use
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tc_id = block.get("id", "")
                        name = block.get("name", "")
                        if tc_id and name:
                            mapping[tc_id] = name

        return mapping

    def _adaptive_min_ratio(self, context_pressure: float) -> float:
        """Compression-acceptance threshold scaled by context pressure.

        Thin delegator to the pure :func:`router_policy.adaptive_min_ratio`.
        """
        return adaptive_min_ratio(self.config, context_pressure)

    def apply(
        self,
        messages: list[dict[str, Any]],
        tokenizer: Tokenizer,
        **kwargs: Any,
    ) -> TransformResult:
        """Apply intelligent routing to messages.

        Args:
            messages: Messages to transform.
            tokenizer: Tokenizer for counting.
            **kwargs: Additional arguments (context).

        Returns:
            TransformResult with routed and compressed messages.

        Raises:
            TypeError: If ``kwargs`` contains a key not in
                ``_APPLY_ALLOWED_KWARGS``. Catches silent typos (e.g.
                ``protect_recents``) that would otherwise be dropped by the
                ``kwargs.get(...)`` reads below.
        """
        # Reject unknown kwargs up front so a typo fails loudly instead of
        # being silently ignored. See _APPLY_ALLOWED_KWARGS for the two
        # sources of the allow-list (keys read here ∪ keys broadcast by the
        # pipeline to every transform).
        for k in kwargs:
            if k not in _APPLY_ALLOWED_KWARGS:
                raise TypeError(f"ContentRouter.apply() got an unexpected keyword argument {k!r}")

        # Pre-process: Read lifecycle management (stale/superseded detection)
        if self.config.read_lifecycle.enabled:
            from .read_lifecycle import ReadLifecycleManager

            lifecycle_mgr = ReadLifecycleManager(
                self.config.read_lifecycle,
                compression_store=kwargs.get("compression_store"),
            )
            lifecycle_result = lifecycle_mgr.apply(
                messages,
                frozen_message_count=kwargs.get("frozen_message_count", 0),
            )
            messages = lifecycle_result.messages
            # lifecycle transforms tracked separately, merged at the end
            lifecycle_transforms = lifecycle_result.transforms_applied
            lifecycle_ccr_hashes = lifecycle_result.ccr_hashes
        else:
            lifecycle_transforms = []
            lifecycle_ccr_hashes = []

        # Runtime overrides from CompressConfig (via kwargs from compress())
        # These override self.config defaults for this call only.
        skip_user = (
            kwargs.get("compress_user_messages") is not True and self.config.skip_user_messages
        )
        skip_system = kwargs.get("compress_system_messages") is not True
        protect_recent = kwargs.get("protect_recent", self.config.protect_recent_code)
        protect_analysis = kwargs.get(
            "protect_analysis_context", self.config.protect_analysis_context
        )
        # Read the per-request min-token floor from the typed CompressRequest
        # built once at the TransformPipeline boundary. That boundary unifies
        # the two PUBLIC entry paths — compress() and
        # TransformPipeline.apply(**kwargs) — to one default (250), fixing the
        # divergence where direct-pipeline callers silently got 50.
        compress_request = kwargs.get("compress_request")
        if isinstance(compress_request, CompressRequest):
            min_tokens = compress_request.min_tokens_to_compress
        else:
            # Raw ContentRouter.apply() (no pipeline boundary, e.g. low-level
            # tests): preserve the historical direct-caller floor of 50. This
            # path is behavior-identical to before — the worker-options pinning
            # test compresses 122-token fixtures through it and depends on the
            # 50 floor letting compression happen.
            min_tokens = kwargs.get("min_tokens_to_compress", 50)
        # Cache-safety knobs for content-block (Anthropic-format) handling:
        compress_assistant_text_blocks = kwargs.get(
            "compress_assistant_text_blocks",
            self.config.compress_assistant_text_blocks,
        )
        min_chars_for_block_compression = kwargs.get(
            "min_chars_for_block_compression",
            self.config.min_chars_for_block_compression,
        )
        # Build the frozen per-request runtime once. It is threaded by
        # argument into every compress() call below (block handler, inline,
        # and parallel workers), so concurrent apply() calls on this shared
        # router stay isolated by value — no thread-local, no replay.
        runtime = RouterRuntime.from_kwargs(kwargs)

        tokens_before = sum(tokenizer.count_text(str(m.get("content", ""))) for m in messages)
        context = kwargs.get("context", "")
        hook_biases: dict[int, float] = kwargs.get("biases") or {}

        # Build tool name map for exclusion checking
        tool_name_map = self._build_tool_name_map(messages)

        # Compute excluded tool IDs based on config
        exclude_tools = (
            self.config.exclude_tools
            if self.config.exclude_tools is not None
            else DEFAULT_EXCLUDE_TOOLS
        )
        excluded_tool_ids = {
            tool_id for tool_id, name in tool_name_map.items() if name in exclude_tools
        }

        # --- Adaptive parameters based on context pressure ---
        num_messages = len(messages)
        model_limit = kwargs.get("model_limit", 0)

        # Adaptive Read protection: protect a fraction of recent messages
        if self.config.protect_recent_reads_fraction > 0:
            # Scale: at 10 msgs protect 5, at 50 msgs protect 25, at 200 msgs protect 100
            # But cap at a reasonable floor so very short convos still protect everything
            read_protection_window = max(
                4,  # always protect at least last 4 messages
                int(num_messages * self.config.protect_recent_reads_fraction),
            )
        else:
            read_protection_window = num_messages  # 0.0 = protect all (old behavior)
        runtime_read_protection_window = kwargs.get("read_protection_window")
        if runtime_read_protection_window is not None:
            read_protection_window = max(0, int(runtime_read_protection_window))

        # Adaptive compression ratio: scale with context pressure
        if model_limit > 0:
            context_pressure = min(1.0, tokens_before / model_limit)
        else:
            context_pressure = 0.5  # default: moderate

        min_ratio = self._adaptive_min_ratio(context_pressure)

        if context_pressure > 0.3:
            logger.debug(
                "content_router adaptive: pressure=%.2f, min_ratio=%.2f, "
                "read_protect_window=%d/%d msgs",
                context_pressure,
                min_ratio,
                read_protection_window,
                num_messages,
            )

        transformed_messages: list[dict[str, Any]] = []
        transforms_applied: list[str] = []
        warnings: list[str] = []
        compressor_timing: dict[str, float] = {}  # strategy → cumulative ms

        # Routing reason counters for summary logging
        route_counts: dict[str, int] = {
            "excluded_tool": 0,
            "user_msg": 0,
            "small": 0,
            "recent_code": 0,
            "analysis_ctx": 0,
            "ratio_too_high": 0,
            "non_string": 0,
            "content_blocks": 0,
        }
        compressed_details: list[str] = []  # e.g. ["code_aware:0.72", "kompress:0.65"]

        # Check for analysis intent in the most recent user message
        analysis_intent = False
        if self.config.protect_analysis_context:
            analysis_intent = self._detect_analysis_intent(messages)

        frozen_message_count = kwargs.get("frozen_message_count", 0)

        # ------------------------------------------------------------------
        # Two-pass parallel compression.
        #
        # Pass 1 (sequential): categorise every message — frozen, protected,
        #   cached, small, etc. are resolved immediately.  Cache-miss messages
        #   that need full compression are collected into *pending_tasks*.
        #
        # Pass 2 (parallel): all cache-miss compressions run concurrently in
        #   a thread pool.  Each self.compress() call is independent.
        #
        # Pass 3 (sequential): results are stitched back into message order,
        #   caches updated, and counters incremented.
        # ------------------------------------------------------------------

        # Pre-allocate result slots — None means "pending compression".
        result_slots: list[dict[str, Any] | None] = [None] * num_messages

        # Tasks: list of (slot_index, content, context, bias, content_key)
        _PendingTask = tuple[int, str, str, float, int]
        pending_tasks: list[_PendingTask] = []

        for i, message in enumerate(messages):
            # Skip frozen messages (in provider's prefix cache).
            # Modifying these would invalidate the cache, replacing a 90%
            # read discount with a 25% write penalty (Anthropic).
            if i < frozen_message_count:
                result_slots[i] = message
                continue

            role = message.get("role", "")
            content = message.get("content", "")
            bias = 1.0  # Default bias, may be overridden for tool messages

            messages_from_end = num_messages - i

            # Handle list content (Anthropic format with content blocks)
            if isinstance(content, list):
                transformed_message = self._process_content_blocks(
                    message,
                    content,
                    context,
                    transforms_applied,
                    excluded_tool_ids,
                    tool_name_map=tool_name_map,
                    route_counts=route_counts,
                    compressed_details=compressed_details,
                    min_ratio=min_ratio,
                    read_protection_window=read_protection_window,
                    messages_from_end=messages_from_end,
                    compressor_timing=compressor_timing,
                    min_chars=min_chars_for_block_compression,
                    skip_user=skip_user,
                    skip_system=skip_system,
                    compress_assistant_text_blocks=compress_assistant_text_blocks,
                    runtime=runtime,
                )
                result_slots[i] = transformed_message
                route_counts["content_blocks"] += 1
                continue

            # Skip non-string content (other types)
            if not isinstance(content, str):
                result_slots[i] = message
                route_counts["non_string"] += 1
                continue

            # Skip OpenAI-style tool messages for excluded tools
            # BUT: allow compression of old excluded-tool outputs beyond the
            # adaptive protection window (age-based decay).
            if role == "tool":
                tool_call_id = message.get("tool_call_id", "")
                if tool_call_id in excluded_tool_ids:
                    if messages_from_end <= read_protection_window:
                        # Recent — protect as before
                        result_slots[i] = message
                        transforms_applied.append("router:excluded:tool")
                        route_counts["excluded_tool"] += 1
                        continue
                    # Old excluded-tool output — fall through to compression
                    # (the LLM is unlikely to need exact content from this far back,
                    # and CCR provides retrieval if it does)
                # Look up tool-specific compression bias for OpenAI tool messages
                tool_name = tool_name_map.get(tool_call_id, "")
                bias = self._get_tool_bias(tool_name) if tool_name else 1.0

            # Protection 1: Never compress user messages (unless overridden)
            if skip_user and role == "user":
                result_slots[i] = message
                transforms_applied.append("router:protected:user_message")
                route_counts["user_msg"] += 1
                continue

            # Protection 1b: Never compress system/developer messages unless
            # explicitly opted in. These are cache-hot instruction bytes.
            if skip_system and role in {"system", "developer"}:
                result_slots[i] = message
                transforms_applied.append(f"router:protected:{role}_message")
                route_counts.setdefault("system_msg", 0)
                route_counts["system_msg"] += 1
                continue

            if not content or tokenizer.count_text(content) < min_tokens:
                # Skip small content
                result_slots[i] = message
                route_counts["small"] += 1
                continue

            # Protection: failed tool calls / error outputs stay verbatim.
            # The model needs exact tracebacks to recover.
            # Strong (>=2 distinct indicators) match only — a single
            # keyword false-positives on benign outputs that mention
            # errors. Above the size cap, fall through — LogCompressor
            # preserves error lines in big logs.
            if (
                self.config.protect_error_outputs
                and role == "tool"
                and len(content) <= self.config.error_protection_max_chars
                and content_has_strong_error_indicators(content)
            ):
                result_slots[i] = message
                transforms_applied.append("router:protected:error_output")
                route_counts.setdefault("error_protected", 0)
                route_counts["error_protected"] += 1
                continue

            # Detect content type for protection decisions
            detection = _detect_content(content)
            is_code = detection.content_type == ContentType.SOURCE_CODE

            # Protection 2: Don't compress recent CODE
            messages_from_end = num_messages - i
            if protect_recent > 0 and messages_from_end <= protect_recent and is_code:
                result_slots[i] = message
                transforms_applied.append("router:protected:recent_code")
                route_counts["recent_code"] += 1
                continue

            # Protection 3: Don't compress CODE when analysis intent detected
            if protect_analysis and analysis_intent and is_code:
                result_slots[i] = message
                transforms_applied.append("router:protected:analysis_context")
                route_counts["analysis_ctx"] += 1
                continue

            # Compression pinning: if this message was already compressed
            # (contains a CCR retrieval marker), skip recompression.
            # Recompressing would change byte content and break provider
            # prefix caching with no meaningful further reduction.
            if "Retrieve more: hash=" in content or "Retrieve original: hash=" in content:
                result_slots[i] = message
                route_counts.setdefault("already_compressed", 0)
                route_counts["already_compressed"] += 1
                continue

            # Route and compress based on content detection
            # Merge tool-specific bias with hook-provided bias (multiplicative)
            msg_bias = bias if role == "tool" else 1.0
            if i in hook_biases:
                msg_bias *= hook_biases[i]

            # Two-tier compression cache. The lookup DECISION — Tier-1 skip,
            # Tier-2 ratio-gate, CCR-backing check, plus every cache mutation and
            # routing-counter bump — is shared with the content-block path in
            # _lookup_cached_disposition. Only what genuinely differs stays here:
            # this path formats a flat ``router:{strategy}:{ratio}`` transform and
            # DEFERS recompute to the batched ThreadPoolExecutor pass below
            # (pending_tasks → Pass 2/3), whereas _compress_content_block threads a
            # ``router:{label}:{strategy}`` format and recompresses inline. The
            # match is the last statement in the loop body, so each arm falls
            # through to the next iteration (no ``continue`` needed). Outcomes
            # pinned in test_content_router_cache_lookup_paths.py +
            # test_result_cache_ccr_divergence.py.
            content_key = hash(content)
            match self._lookup_cached_disposition(content_key, context, min_ratio, route_counts):
                case ServeOriginal():
                    result_slots[i] = message
                case ServeCached(compressed=served, strategy=strategy, ratio=ratio):
                    result_slots[i] = {**message, "content": served}
                    transforms_applied.append(f"router:{strategy}:{ratio:.2f}")
                    compressed_details.append(f"{strategy}:{ratio:.2f}")
                case Recompute():
                    # Defer to the parallel compression pass (Pass 2/3).
                    pending_tasks.append((i, content, context, msg_bias, content_key))
                case other:
                    raise RuntimeError(
                        f"_lookup_cached_disposition returned unexpected CacheDisposition {other!r}"
                    )

        # --- Pass 2: Parallel compression of all cache-miss messages ---
        if pending_tasks:
            raw_workers = os.environ.get("HEADROOM_COMPRESS_WORKERS", "4")
            try:
                configured_workers = int(raw_workers)
            except ValueError:
                logger.warning("Invalid HEADROOM_COMPRESS_WORKERS=%r; using default 4", raw_workers)
                configured_workers = 4
            max_workers = min(len(pending_tasks), configured_workers)
            t_parallel_start = time.perf_counter()

            if max_workers <= 1 or len(pending_tasks) == 1:
                # Single task or parallelism disabled — compress inline
                task_results = []
                for _, task_content, task_ctx, task_bias, _ in pending_tasks:
                    t0 = time.perf_counter()
                    r = self.compress(
                        task_content, context=task_ctx, bias=task_bias, runtime=runtime
                    )
                    task_results.append((r, (time.perf_counter() - t0) * 1000))
            else:
                # Parallel compression via thread pool. The frozen ``runtime``
                # is passed by value to every worker — there is no thread-local
                # to replay, so each worker compresses under the same immutable
                # per-request options the main thread holds.
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = []
                    for _, task_content, task_ctx, task_bias, _ in pending_tasks:
                        futures.append(
                            executor.submit(
                                self._timed_compress,
                                task_content,
                                task_ctx,
                                task_bias,
                                runtime,
                            )
                        )
                    task_results = [f.result() for f in futures]

            parallel_ms = (time.perf_counter() - t_parallel_start) * 1000
            compressor_timing["parallel_compress_total"] = parallel_ms

            # --- Pass 3: Merge results back (sequential, updates caches) ---
            for (slot_idx, _, _, _, content_key), (result, compress_ms) in zip(
                pending_tasks, task_results
            ):
                message = messages[slot_idx]
                strategy_key = f"compressor:{result.strategy_used.value}"
                compressor_timing[strategy_key] = (
                    compressor_timing.get(strategy_key, 0.0) + compress_ms
                )

                if result.compression_ratio < min_ratio:
                    # Compressed — store in result cache
                    self._cache.put(
                        content_key,
                        result.compressed,
                        result.compression_ratio,
                        result.strategy_used.value,
                    )
                    result_slots[slot_idx] = {**message, "content": result.compressed}
                    transforms_applied.append(
                        f"router:{result.strategy_used.value}:{result.compression_ratio:.2f}"
                    )
                    compressed_details.append(
                        f"{result.strategy_used.value}:{result.compression_ratio:.2f}"
                    )
                else:
                    # Didn't compress — add to skip set
                    self._cache.mark_skip(content_key)
                    result_slots[slot_idx] = message
                    route_counts["ratio_too_high"] += 1

        # Build final message list from slots
        transformed_messages = [m for m in result_slots if m is not None]

        tokens_after = sum(
            tokenizer.count_text(str(m.get("content", ""))) for m in transformed_messages
        )

        # Log routing summary
        parts = []
        if compressed_details:
            parts.append(f"{len(compressed_details)} compressed ({', '.join(compressed_details)})")
        if route_counts["excluded_tool"]:
            parts.append(f"{route_counts['excluded_tool']} excluded (Read/Glob)")
        if route_counts["user_msg"]:
            parts.append(f"{route_counts['user_msg']} skipped (user)")
        if route_counts["small"]:
            parts.append(f"{route_counts['small']} skipped (<50 words)")
        if route_counts["recent_code"]:
            parts.append(f"{route_counts['recent_code']} protected (recent code)")
        if route_counts["analysis_ctx"]:
            parts.append(f"{route_counts['analysis_ctx']} protected (analysis ctx)")
        if route_counts.get("already_compressed"):
            parts.append(f"{route_counts['already_compressed']} pinned (already compressed)")
        if route_counts.get("error_protected"):
            parts.append(f"{route_counts['error_protected']} protected (error output)")
        if route_counts["ratio_too_high"]:
            parts.append(f"{route_counts['ratio_too_high']} unchanged (ratio>={min_ratio:.2f})")
        if route_counts["content_blocks"]:
            parts.append(f"{route_counts['content_blocks']} content-block msgs")
        if route_counts["non_string"]:
            parts.append(f"{route_counts['non_string']} non-string")
        if route_counts.get("cache_hit"):
            parts.append(f"{route_counts['cache_hit']} cache hits")
        if route_counts.get("cache_miss"):
            parts.append(f"{route_counts['cache_miss']} cache misses")
        cs = self._cache.stats
        if cs["cache_size"] > 0 or cs["cache_skip_size"] > 0:
            parts.append(
                f"cache[{cs['cache_size']} results, {cs['cache_skip_size']} skips, "
                f"{cs['cache_avg_lookup_ns']:.0f}ns avg]"
            )
        if parts:
            logger.info(
                "content_router: %d msgs — %s",
                num_messages,
                ", ".join(parts),
            )

        # Forward route_counts to the observer so `/stats` can surface a
        # session-level protection breakdown. The observer
        # may not implement this method on older versions; ignore
        # AttributeError so a non-conforming observer doesn't poison
        # routing.
        if self._observer is not None and route_counts:
            try:
                self._observer.record_router_route_counts(route_counts)
            except AttributeError:
                pass
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("Router observer raised (non-fatal): %s", e)

        all_transforms = lifecycle_transforms + transforms_applied
        return TransformResult(
            messages=transformed_messages,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            transforms_applied=all_transforms if all_transforms else ["router:noop"],
            markers_inserted=lifecycle_ccr_hashes,
            warnings=warnings,
            timing=compressor_timing,
        )

    def _get_tool_bias(self, tool_name: str) -> float:
        """Look up compression bias for a tool name.

        Checks user-configured profiles first, then DEFAULT_TOOL_PROFILES.
        Returns 1.0 (moderate) if no profile is configured.
        """
        from ..config import DEFAULT_TOOL_PROFILES

        # Check user-configured profiles
        if self.config.tool_profiles:
            profile = self.config.tool_profiles.get(tool_name)
            if profile:
                return float(profile.bias)

        # Check default profiles
        profile = DEFAULT_TOOL_PROFILES.get(tool_name)
        if profile:
            return profile.bias

        return 1.0  # Default: moderate

    def _lookup_cached_disposition(
        self,
        content_key: int,
        context: str,
        min_ratio: float,
        route_counts: dict[str, int] | None,
    ) -> CacheDisposition:
        """Resolve one content key against the two-tier compression cache.

        The single home of the lookup decision tree that the string path
        (``apply``) and the content-block path (``_compress_content_block``)
        both run. Returns WHAT to do; each caller owns the HOW — the
        transform-string format and the serve-vs-defer recompute mechanism
        genuinely differ between the two and stay in the callers.

        Every cache mutation and routing-counter bump lives HERE, so the
        data-loss guard — never serve a ``<<ccr:HASH>>`` sentinel whose CCR
        backing has expired — is provable in one place. The five outcomes and
        their counter effects (identical on both former copies):

          * Tier-1 skip hit            → ServeOriginal (ratio_too_high, cache_hit)
          * Tier-2 tightened→skip      → ServeOriginal (ratio_too_high, cache_hit)
          * Tier-2 live, CCR-backed    → ServeCached   (cache_hit)
          * Tier-2 unbackable sentinel → Recompute     (cache_stale_recompute, cache_miss)
          * cache miss                 → Recompute     (cache_miss)

        ``route_counts`` is ``None`` only when the block-path caller opts out of
        routing summaries; the bumps are then skipped.
        """

        def bump(*keys: str) -> None:
            if route_counts is not None:
                for k in keys:
                    route_counts[k] = route_counts.get(k, 0) + 1

        # Tier 1: skip set — instant rejection.
        if self._cache.is_skipped(content_key):
            bump("ratio_too_high", "cache_hit")
            return _SERVE_ORIGINAL

        # Tier 2: result cache — reuse compressed output.
        cached = self._cache.get(content_key)
        if cached is not None:
            cached_compressed, cached_ratio, cached_strategy = cached
            if cached_ratio >= min_ratio:
                # Threshold tightened — no longer qualifies. Relocate to skip.
                self._cache.move_to_skip(content_key)
                bump("ratio_too_high", "cache_hit")
                return _SERVE_ORIGINAL
            # Invariant: every <<ccr:HASH>> in a served output must be backed by
            # a live CCR store entry. Both CCR stores (Rust + Python, 300s TTL)
            # expire independently of this result cache (30-min TTL). Re-mirror
            # to refresh the backing; if a sentinel is unbackable (both CCR
            # stores expired), DO NOT serve the stale dead pointer — evict and
            # recompute (which re-creates + re-stores the CCR backing).
            if self._ensure_ccr_backed(cached_compressed, context):
                bump("cache_hit")
                return ServeCached(cached_compressed, cached_strategy, cached_ratio)
            self._cache.invalidate(content_key)
            bump("cache_stale_recompute", "cache_miss")
            return _RECOMPUTE

        # Cache miss.
        bump("cache_miss")
        return _RECOMPUTE

    def _compress_content_block(
        self,
        block: dict[str, Any],
        text: str,
        *,
        block_key: str,
        label: str,
        detail_prefix: str,
        context: str,
        min_ratio: float,
        bias: float,
        runtime: RouterRuntime,
        transforms_applied: list[str],
        route_counts: dict[str, int] | None,
        compressed_details: list[str] | None,
        compressor_timing: dict[str, float] | None,
    ) -> tuple[dict[str, Any], bool]:
        """Compress one cacheable content block (Anthropic ``tool_result`` or
        ``text``). Returns ``(new_block, did_compress)``.

        Single source for the two-tier-cache + ``_ensure_ccr_backed`` + ratio-gate
        logic that the ``tool_result`` and ``text`` branches of
        ``_process_content_blocks`` ran near-identically — they differ only in the
        block payload key (``content`` vs ``text``) and the log labels, threaded in
        as ``block_key`` / ``label`` / ``detail_prefix``. The caller has already
        done role-gating, error protection, already-compressed pinning, and the
        ``min_chars`` check before calling this.
        """

        def bump(*keys: str) -> None:
            if route_counts is not None:
                for k in keys:
                    route_counts[k] = route_counts.get(k, 0) + 1

        content_key = hash(text)
        match self._lookup_cached_disposition(content_key, context, min_ratio, route_counts):
            case ServeOriginal():
                return block, False
            case ServeCached(compressed=served, strategy=strategy, ratio=ratio):
                transforms_applied.append(f"router:{label}:{strategy}")
                if compressed_details is not None:
                    compressed_details.append(f"{detail_prefix}:{strategy}:{ratio:.2f}")
                return {**block, block_key: served}, True
            case Recompute():
                pass  # fall through to full compression below
            case other:
                raise RuntimeError(
                    f"_lookup_cached_disposition returned unexpected CacheDisposition {other!r}"
                )

        # Recompute (cache miss or evicted stale sentinel). All cache bookkeeping
        # — skip/stale/miss counters and any eviction — already happened inside
        # _lookup_cached_disposition; here we only (re)compress and store.
        t0 = time.perf_counter()
        result = self.compress(text, context=context, bias=bias, runtime=runtime)
        compress_ms = (time.perf_counter() - t0) * 1000
        if compressor_timing is not None:
            key = f"compressor:{result.strategy_used.value}"
            compressor_timing[key] = compressor_timing.get(key, 0.0) + compress_ms
        if result.compression_ratio < min_ratio:
            self._cache.put(
                content_key,
                result.compressed,
                result.compression_ratio,
                result.strategy_used.value,
            )
            transforms_applied.append(f"router:{label}:{result.strategy_used.value}")
            if compressed_details is not None:
                compressed_details.append(
                    f"{detail_prefix}:{result.strategy_used.value}:{result.compression_ratio:.2f}"
                )
            return {**block, block_key: result.compressed}, True
        # Didn't compress — add to skip set.
        self._cache.mark_skip(content_key)
        bump("ratio_too_high")
        return block, False

    def _process_content_blocks(
        self,
        message: dict[str, Any],
        content_blocks: list[Any],
        context: str,
        transforms_applied: list[str],
        excluded_tool_ids: set[str],
        tool_name_map: dict[str, str] | None = None,
        route_counts: dict[str, int] | None = None,
        compressed_details: list[str] | None = None,
        min_ratio: float = 0.85,
        read_protection_window: int = 8,
        messages_from_end: int = 0,
        compressor_timing: dict[str, float] | None = None,
        min_chars: int = 500,
        skip_user: bool = True,
        skip_system: bool = True,
        compress_assistant_text_blocks: bool = False,
        runtime: RouterRuntime = _DEFAULT_RUNTIME,
    ) -> dict[str, Any]:
        """Process content blocks (Anthropic format) for compression.

        Cache-safety contract:
          1. Any block carrying `cache_control` is the client's explicit
             cache breakpoint. Modifying any byte of such a block changes
             the cache key the upstream provider matches against, turning
             a 90% read discount into a 25% write penalty (Anthropic).
             We never modify cache_control'd blocks, regardless of role
             or block type.
          2. Assistant text blocks are echoed back by the client in
             subsequent turns and become part of the upstream provider's
             auto-prefix cache (DeepSeek, OpenAI). Default-skip; opt in
             via `compress_assistant_text_blocks` when the deployment
             knows the backend doesn't honor cache_control AND
             compression is byte-deterministic.
          3. User and system blocks carry the prompt the model is acting
             on; compressing them silently mutates the request. Always
             skipped per `skip_user` / `skip_system`.
          4. Tool / function blocks are tool outputs — semantically safe
             to compress (the model references them once, then moves on).

        Args:
            message: The original message.
            content_blocks: List of content blocks.
            context: Context for compression.
            transforms_applied: List to append transform names to.
            excluded_tool_ids: Tool IDs to skip compression for.
            tool_name_map: Mapping from tool_call_id to tool_name for profile lookup.
            route_counts: Optional routing reason counters to update.
            compressed_details: Optional list to append compression details to.
            min_ratio: Adaptive compression ratio threshold.
            read_protection_window: Messages from end within which excluded tools are protected.
            messages_from_end: How far this message is from the end of the conversation.
            min_chars: Minimum block content length (chars) to consider for compression.
            skip_user: If True, never compress text blocks in user-role messages.
            skip_system: If True, never compress text blocks in system-role messages.
            compress_assistant_text_blocks: If True, allow compressing text blocks in
                assistant-role messages. Default False (cache-safe).

        Returns:
            Transformed message with compressed content blocks.
        """
        new_blocks = []
        any_compressed = False
        role = message.get("role", "")

        # Role-based gate for `text` blocks. Tool/function roles are tool
        # outputs and compress freely; assistant defaults to skip (cache
        # safety) with explicit opt-in; unknown roles default to skip.
        if (skip_user and role == "user") or (skip_system and role in {"system", "developer"}):
            protect_text_blocks = True
        elif role == "assistant" and not compress_assistant_text_blocks:
            protect_text_blocks = True
        elif role not in ("assistant", "tool", "function"):
            protect_text_blocks = True
        else:
            protect_text_blocks = False

        for block in content_blocks:
            if not isinstance(block, dict):
                new_blocks.append(block)
                continue

            # Defense in depth: cache_control marker is the client's
            # cache breakpoint. Frozen-message-count is a coarse
            # message-level approximation; this is the per-block
            # guarantee that we never bust an explicit cache key.
            if "cache_control" in block:
                new_blocks.append(block)
                if route_counts is not None:
                    route_counts.setdefault("cache_control_protected", 0)
                    route_counts["cache_control_protected"] += 1
                continue

            block_type = block.get("type")

            # Handle tool_result blocks
            if block_type == "tool_result":
                # Check if tool is excluded from compression
                tool_use_id = block.get("tool_use_id", "")
                if tool_use_id in excluded_tool_ids:
                    if messages_from_end <= read_protection_window:
                        # Recent — protect as before
                        new_blocks.append(block)
                        transforms_applied.append("router:excluded:tool")
                        if route_counts is not None:
                            route_counts["excluded_tool"] += 1
                        continue
                    # Old excluded-tool output — fall through to compression

                # Look up tool-specific compression bias
                tool_name = (tool_name_map or {}).get(tool_use_id, "")
                bias = self._get_tool_bias(tool_name) if tool_name else 1.0

                tool_content = block.get("content", "")

                # Protection: failed tool calls / error outputs stay verbatim.
                # `is_error` is Anthropic's explicit failure
                # flag and suffices alone; the indicator scan catches error
                # text without the flag but requires >=2 distinct keywords
                # so benign outputs mentioning errors don't skip compression.
                # Above the size cap, fall through — LogCompressor preserves
                # error lines in big logs.
                if (
                    self.config.protect_error_outputs
                    and isinstance(tool_content, str)
                    and len(tool_content) <= self.config.error_protection_max_chars
                    and (
                        block.get("is_error") is True
                        or content_has_strong_error_indicators(tool_content)
                    )
                ):
                    new_blocks.append(block)
                    transforms_applied.append("router:protected:error_output")
                    if route_counts is not None:
                        route_counts.setdefault("error_protected", 0)
                        route_counts["error_protected"] += 1
                    continue

                # Only process string content
                if isinstance(tool_content, str) and len(tool_content) > min_chars:
                    # Compression pinning: skip already-compressed content
                    if (
                        "Retrieve more: hash=" in tool_content
                        or "Retrieve original: hash=" in tool_content
                    ):
                        new_blocks.append(block)
                        if route_counts is not None:
                            route_counts.setdefault("already_compressed", 0)
                            route_counts["already_compressed"] += 1
                        continue

                    new_block, did = self._compress_content_block(
                        block,
                        tool_content,
                        block_key="content",
                        label="tool_result",
                        detail_prefix="tool",
                        context=context,
                        min_ratio=min_ratio,
                        bias=bias,
                        runtime=runtime,
                        transforms_applied=transforms_applied,
                        route_counts=route_counts,
                        compressed_details=compressed_details,
                        compressor_timing=compressor_timing,
                    )
                    new_blocks.append(new_block)
                    any_compressed = any_compressed or did
                    continue
                else:
                    if route_counts is not None:
                        route_counts["small"] += 1

            # Handle text blocks — compress for non-Anthropic clients (e.g.
            # OpenAI/DeepSeek via Cline) whose SDK normalizes content to
            # block-list form. Roles are gated above (user/system always
            # skipped; assistant default-skipped, opt-in via
            # `compress_assistant_text_blocks`).
            elif block_type == "text" and not protect_text_blocks:
                text_content = block.get("text", "")
                if isinstance(text_content, str) and len(text_content) > min_chars:
                    # Pinning: skip already-compressed content
                    if (
                        "Retrieve more: hash=" in text_content
                        or "Retrieve original: hash=" in text_content
                    ):
                        new_blocks.append(block)
                        if route_counts is not None:
                            route_counts.setdefault("already_compressed", 0)
                            route_counts["already_compressed"] += 1
                        continue

                    new_block, did = self._compress_content_block(
                        block,
                        text_content,
                        block_key="text",
                        label="text_block",
                        detail_prefix="text",
                        context=context,
                        min_ratio=min_ratio,
                        bias=1.0,
                        runtime=runtime,
                        transforms_applied=transforms_applied,
                        route_counts=route_counts,
                        compressed_details=compressed_details,
                        compressor_timing=compressor_timing,
                    )
                    new_blocks.append(new_block)
                    any_compressed = any_compressed or did
                    continue
                else:
                    if route_counts is not None:
                        route_counts["small"] += 1

            # Keep block unchanged
            new_blocks.append(block)

        if any_compressed:
            return {**message, "content": new_blocks}
        return message

    def _detect_analysis_intent(self, messages: list[dict[str, Any]]) -> bool:
        """Detect if user wants to analyze/review code.

        Looks at the most recent user message for analysis keywords.

        Args:
            messages: Conversation messages.

        Returns:
            True if analysis intent detected.
        """
        # Analysis keywords that suggest user wants full code details
        analysis_keywords = {
            "analyze",
            "analyse",
            "review",
            "audit",
            "inspect",
            "security",
            "vulnerability",
            "bug",
            "issue",
            "problem",
            "explain",
            "understand",
            "how does",
            "what does",
            "debug",
            "fix",
            "error",
            "wrong",
            "broken",
            "refactor",
            "improve",
            "optimize",
            "clean up",
        }

        # Find most recent user message
        for message in reversed(messages):
            if message.get("role") == "user":
                content = message.get("content", "")
                if isinstance(content, str):
                    content_lower = content.lower()
                    for keyword in analysis_keywords:
                        if keyword in content_lower:
                            return True
                break

        return False

    def should_apply(
        self,
        messages: list[dict[str, Any]],
        tokenizer: Tokenizer,
        **kwargs: Any,
    ) -> bool:
        """Check if routing should be applied.

        Always returns True - the router handles all content types.
        """
        return True
