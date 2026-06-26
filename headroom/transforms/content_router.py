"""Content router for intelligent compression strategy selection.

This module provides the ContentRouter, which analyzes content and routes it
to the optimal compressor. It handles mixed content by splitting, routing
each section to the appropriate compressor, and reassembling.

Supported Compressors:
- SmartCrusher: JSON arrays
- SearchCompressor: grep/ripgrep results
- LogCompressor: Build/test output
- KompressCompressor: Plain text (ML-based)
- Kompress: Plain text (ML-based, requires [ml] extra)

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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

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
    """Detect content type via the Rust detection chain.

    This runs through `headroom._core.detect_content_type`,
    which runs the magika→unidiff→PlainText chain. There is no Python-side
    Magika+regex fallback path — single detection
    surface, no parallel paths. The Rust extension is a hard dep
    (no Python fallback) per `feedback_no_silent_fallbacks.md`.

    The Rust binding returns the legacy `DetectionResult` shape with
    `confidence=1.0` and an empty metadata dict. Existing callers
    only consumed `.content_type` from it; the strategy mapping in
    `_strategy_from_detection` keys off that field alone.
    """
    from headroom._core import detect_content_type as _rust_detect

    rust_result = _rust_detect(content)
    # Rust's `content_type` is the lowercase string tag (e.g.
    # "json_array"); translate to the Python `ContentType` enum so
    # downstream mapping keys match.
    content_type = ContentType(rust_result.content_type)
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
) -> Any:
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
        cache_hit: True when this result came from the router's
            result_cache (no fresh compression ran). Currently the
            single-content compress() path doesn't populate the cache,
            so this is False in practice — placeholder for the
            cache-wire-up follow-up.
    """

    compressed: str
    original: str
    strategy_used: CompressionStrategy
    routing_log: list[RoutingDecision] = field(default_factory=list)
    sections_processed: int = 1
    strategy_chain: list[str] = field(default_factory=list)
    cache_hit: bool = False

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
        # to it. ``_kompress`` STAYS here: ``_get_kompress`` reads the
        # thread-local ``_runtime_kompress_model`` (#10 surface), so it is not
        # self-contained and must not move into the registry.
        self._registry = CompressorRegistry(self.config)
        self._kompress: Any = None

        # TOIN integration for cross-strategy learning
        self._toin: Any = None

        # Per-request runtime options (``_runtime_target_ratio``,
        # ``_runtime_force_kompress``, ``_runtime_kompress_model``,
        # ``_runtime_compression_policy``) are exposed as properties
        # backed by this ``threading.local``. The pipeline reuses ONE
        # ContentRouter across every ``compress()`` call, so storing
        # per-request state as plain instance attributes let two
        # concurrent calls with different configs clobber each other.
        # Thread-local storage isolates each in-flight request: the
        # property getters fall back to the documented defaults
        # (``None`` / ``False``) when the current thread hasn't set a
        # value, preserving single-threaded behaviour exactly. Because the
        # getters always exist and never raise, read sites use the property
        # directly (``self._runtime_*``), not a ``getattr(..., default)`` probe.
        self._tls = threading.local()

        self._cache = CompressionCache()

    # ------------------------------------------------------------------
    # Per-request runtime options (thread-local backed).
    #
    # ``_runtime_compression_policy`` carries the per-request
    # CompressionPolicy, set from ``kwargs["compression_policy"]`` at
    # the start of ``apply()`` and read by ``_record_to_toin`` to gate
    # TOIN writes when ``policy.toin_read_only`` is true (Subscription
    # mode). Defaults to ``None`` so direct ``compress()`` callers (e.g.
    # tests, hand-written pipelines that don't set a policy)
    # leave TOIN writes ungated.
    # ------------------------------------------------------------------
    @property
    def _runtime_target_ratio(self) -> float | None:
        return getattr(self._tls, "target_ratio", None)

    @_runtime_target_ratio.setter
    def _runtime_target_ratio(self, value: float | None) -> None:
        self._tls.target_ratio = value

    @property
    def _runtime_force_kompress(self) -> bool:
        return getattr(self._tls, "force_kompress", False)

    @_runtime_force_kompress.setter
    def _runtime_force_kompress(self, value: bool) -> None:
        self._tls.force_kompress = value

    @property
    def _runtime_kompress_model(self) -> str | None:
        return getattr(self._tls, "kompress_model", None)

    @_runtime_kompress_model.setter
    def _runtime_kompress_model(self, value: str | None) -> None:
        self._tls.kompress_model = value

    @property
    def _runtime_compression_policy(self) -> Any:
        return getattr(self._tls, "compression_policy", None)

    @_runtime_compression_policy.setter
    def _runtime_compression_policy(self, value: Any) -> None:
        self._tls.compression_policy = value

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

        # Read-only gate: when the active CompressionPolicy says
        # ``toin_read_only=True`` (Subscription auth mode), don't
        # mutate the TOIN learning pool from this request. Direct
        # ``compress()`` callers don't go through ``apply()`` and
        # have ``self._runtime_compression_policy is None`` — those
        # keep write-enabled behaviour.
        policy = self._runtime_compression_policy
        if policy is not None and policy.toin_read_only:
            logger.debug(
                "ContentRouter: skipping TOIN record_compression for %s "
                "— policy.toin_read_only=True (auth_mode resolved as "
                "Subscription, F2.2 gate)",
                strategy.value,
            )
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
        runtime_options: dict[str, Any] | None = None,
    ) -> tuple[RouterCompressionResult, float]:
        """Compress with wall-clock timing.  Used by parallel executor.

        Per-request runtime options live in ``self._tls`` (thread-local), which
        is set on the MAIN thread. Worker threads have their own empty
        thread-local, so they would read the DEFAULTS and silently drop the
        per-request options. The main thread snapshots the options and
        passes them in here; we replay them into the worker's thread-local
        before compressing so ``force_kompress`` / ``target_ratio`` /
        ``kompress_model`` / ``compression_policy`` reach every worker.
        """
        if runtime_options is not None:
            self._runtime_target_ratio = runtime_options["target_ratio"]
            self._runtime_force_kompress = runtime_options["force_kompress"]
            self._runtime_kompress_model = runtime_options["kompress_model"]
            self._runtime_compression_policy = runtime_options["compression_policy"]
        t0 = time.perf_counter()
        result = self.compress(content, context=context, bias=bias)
        return result, (time.perf_counter() - t0) * 1000

    def compress(
        self,
        content: str,
        context: str = "",
        question: str | None = None,
        bias: float = 1.0,
    ) -> RouterCompressionResult:
        """Compress content using optimal strategy based on content detection.

        Args:
            content: Content to compress.
            context: Optional context for relevance-aware compression.
            question: Optional question for QA-aware compression. When provided,
                tokens relevant to answering this question are preserved.
            bias: Compression bias multiplier (>1 = keep more, <1 = keep fewer).

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
            # Determine strategy from content analysis
            mixed = is_mixed_content(content)
            detection = _detect_content(content)
            force_kompress = bool(self._runtime_force_kompress)
            strategy = (
                CompressionStrategy.KOMPRESS
                if force_kompress
                else self._determine_strategy(content)
            )
            if debug_enabled:
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
                result = self._compress_mixed(content, context, question, bias=bias)
            else:
                result = self._compress_pure(content, strategy, context, question, bias=bias)

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
            content, strategy, context, question=question, bias=bias
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
    ) -> tuple[str, int, list[str]]:
        """Apply a compression strategy to content.

        Args:
            content: Content to compress.
            strategy: Strategy to use.
            context: User context.
            language: Language hint for code.
            question: Optional question for QA-aware compression.
            bias: Compression bias multiplier (>1 = keep more, <1 = keep fewer).

        Returns:
            Tuple of (compressed_content, compressed_token_count,
            strategy_chain). The chain lists every strategy attempted
            in order — first the requested one, then any fallbacks.
            Single-entry chain means a direct hit; multi-entry means
            the fallback chain fired (e.g. ``[smart_crusher, kompress,
            log]``). Log readers use this to see *how* we got to the
            final compressor without parsing decision_reason strings.
        """
        # Track original tokens for TOIN recording
        original_tokens = len(content.split())
        compressed: str | None = None
        compressed_tokens: int | None = None
        requested_strategy = strategy
        actual_strategy = strategy
        compressor_name = strategy.value
        decision_reason = "strategy_not_enabled_or_unavailable"
        strategy_chain: list[str] = [strategy.value]
        error: str | None = None

        try:
            if strategy == CompressionStrategy.CODE_AWARE:
                # The AST-based code compressor was retired; CODE_AWARE always
                # falls through to Kompress (source code keeps routing there).
                if compressed is None:
                    # Fallback to Kompress
                    compressed, compressed_tokens = self._try_ml_compressor(
                        content, context, question
                    )
                    strategy = CompressionStrategy.KOMPRESS  # Update for TOIN
                    actual_strategy = strategy
                    compressor_name = "KompressCompressor"
                    decision_reason = "code_aware_unavailable_fallback_kompress"
                    strategy_chain.append(CompressionStrategy.KOMPRESS.value)

            elif strategy == CompressionStrategy.SMART_CRUSHER:
                # SmartCrusher handles its own TOIN recording. The no-savings
                # Kompress (then Log) fallback is handled ONCE by the generic
                # post-dispatch fallback below (fallback_eligible_strategy
                # includes SMART_CRUSHER). There is no inner duplicate, so the
                # ML compressor never runs twice and 'kompress' is never
                # double-appended to strategy_chain.
                if self.config.enable_smart_crusher:
                    crusher = self._get_smart_crusher()
                    if crusher:
                        compressor_name = type(crusher).__name__
                        result = crusher.crush(content, query=context, bias=bias)
                        compressed, compressed_tokens = (
                            result.compressed,
                            len(result.compressed.split()),
                        )
                        decision_reason = "smart_crusher"

            elif strategy == CompressionStrategy.SEARCH:
                if self.config.enable_search_compressor:
                    compressor = self._get_search_compressor()
                    if compressor:
                        compressor_name = type(compressor).__name__
                        result = compressor.compress(content, context=context, bias=bias)
                        compressed, compressed_tokens = (
                            result.compressed,
                            len(result.compressed.split()),
                        )
                        decision_reason = "search_compressor"

            elif strategy == CompressionStrategy.LOG:
                if self.config.enable_log_compressor:
                    compressor = self._get_log_compressor()
                    if compressor:
                        compressor_name = type(compressor).__name__
                        result = compressor.compress(content, bias=bias)
                        # Use the same word-count metric the rest of the
                        # router uses; `compressed_line_count` is in
                        # lines, not tokens — recording it here made
                        # ratios meaningless against `original_tokens`.
                        compressed, compressed_tokens = (
                            result.compressed,
                            len(result.compressed.split()),
                        )
                        decision_reason = "log_compressor"

            elif strategy == CompressionStrategy.DIFF:
                compressor = self._get_diff_compressor()
                if compressor:
                    compressor_name = type(compressor).__name__
                    result = compressor.compress(content, context=context)
                    compressed, compressed_tokens = (
                        result.compressed,
                        len(result.compressed.split()),
                    )
                    decision_reason = "diff_compressor"

            elif strategy == CompressionStrategy.HTML:
                if self.config.enable_html_extractor:
                    extractor = self._get_html_extractor()
                    if extractor:
                        compressor_name = type(extractor).__name__
                        result = extractor.extract(content)
                        compressed = result.extracted
                        # Estimate tokens from extracted text (simple word count)
                        compressed_tokens = len(compressed.split()) if compressed else 0
                        decision_reason = "html_extractor"

            elif strategy == CompressionStrategy.KOMPRESS:
                compressed, compressed_tokens = self._try_ml_compressor(content, context, question)
                compressor_name = "KompressCompressor"
                decision_reason = "kompress"

            elif strategy == CompressionStrategy.TEXT:
                # Prefer Kompress ML compressor for text
                # Passes through unchanged if Kompress not available
                compressed, compressed_tokens = self._try_ml_compressor(content, context, question)
                compressor_name = "KompressCompressor"
                decision_reason = "text_uses_kompress"

            elif strategy == CompressionStrategy.PASSTHROUGH:
                compressed = content
                compressed_tokens = original_tokens
                compressor_name = "Passthrough"
                decision_reason = "explicit_passthrough"

        except Exception as e:  # noqa: BLE001
            from .kompress_compressor import _MODEL_UNAVAILABLE_ERRORS

            if not isinstance(e, _MODEL_UNAVAILABLE_ERRORS):
                # Real compressor bug: re-raise so callers see the failure.
                # Only model-unavailable errors (KompressModelNotCached /
                # ImportError / FileNotFoundError / OSError) are legitimate
                # "graceful passthrough" — everything else is a bug that must
                # propagate loud (#4-upstream).
                raise
            error = f"{type(e).__name__}: {e}"
            decision_reason = "model_unavailable_passthrough"
            logger.warning(
                "Compression with %s: model unavailable, passthrough: %s",
                strategy.value,
                e,
            )

        # If compression succeeded, record to TOIN
        if compressed is not None and compressed_tokens is not None:
            fallback_eligible_strategy = strategy in {
                CompressionStrategy.SMART_CRUSHER,
                CompressionStrategy.CODE_AWARE,
            }
            fallback_no_savings = compressed == content or compressed_tokens >= original_tokens
            if fallback_eligible_strategy and fallback_no_savings:
                strategy_chain.append(CompressionStrategy.KOMPRESS.value)
                fallback_compressed, fallback_tokens = self._try_ml_compressor(
                    content, context, question
                )
                if fallback_tokens < compressed_tokens:
                    compressed = fallback_compressed
                    compressed_tokens = fallback_tokens
                    actual_strategy = CompressionStrategy.KOMPRESS
                    compressor_name = "KompressCompressor"
                    decision_reason = f"{decision_reason}_fallback_kompress_after_no_savings"
                else:
                    # Last-ditch: line-structured compressors (log dumps
                    # land here — repetitive JSONL that
                    # Kompress can't shrink but the log compressor can).
                    # Only attempted when the strategy was SMART_CRUSHER so
                    # we don't reroute genuine code/diff content.
                    if (
                        strategy == CompressionStrategy.SMART_CRUSHER
                        and self.config.enable_log_compressor
                    ):
                        log_compressor = self._get_log_compressor()
                        if log_compressor is not None:
                            strategy_chain.append(CompressionStrategy.LOG.value)
                            try:
                                log_result = log_compressor.compress(content, bias=bias)
                            except Exception as exc:  # noqa: BLE001
                                logger.debug("Log fallback failed for SMART_CRUSHER: %s", exc)
                            else:
                                log_compressed_tokens = len(log_result.compressed.split())
                                if log_compressed_tokens < compressed_tokens:
                                    compressed = log_result.compressed
                                    compressed_tokens = log_compressed_tokens
                                    actual_strategy = CompressionStrategy.LOG
                                    compressor_name = type(log_compressor).__name__
                                    decision_reason = (
                                        f"{decision_reason}_fallback_log_after_no_savings"
                                    )

            # Re-narrow for mypy: all reassignments above produce str, but
            # mypy 1.14.x widens after nested try/except/else reassignments.
            assert compressed is not None
            if logger.isEnabledFor(logging.DEBUG):
                _log_router_debug(
                    "content_router_strategy_result",
                    requested_strategy=requested_strategy.value,
                    actual_strategy=actual_strategy.value,
                    strategy_chain=strategy_chain,
                    compressor=compressor_name,
                    reason=decision_reason,
                    language=language,
                    question=question,
                    bias=bias,
                    original_tokens=original_tokens,
                    compressed_tokens=compressed_tokens,
                    tokens_saved=max(0, original_tokens - compressed_tokens),
                    compression_ratio=compressed_tokens / original_tokens
                    if original_tokens
                    else 1.0,
                    json_shape=_json_shape(content),
                    input=content,
                    output=compressed,
                    error=error,
                )
            self._record_to_toin(
                strategy=strategy,
                content=content,
                compressed=compressed,
                original_tokens=original_tokens,
                compressed_tokens=compressed_tokens,
                language=language,
                context=context,
            )
            return compressed, compressed_tokens, strategy_chain

        # Fallback: return unchanged
        strategy_chain.append(CompressionStrategy.PASSTHROUGH.value)
        if logger.isEnabledFor(logging.DEBUG):
            _log_router_debug(
                "content_router_strategy_result",
                requested_strategy=requested_strategy.value,
                actual_strategy=CompressionStrategy.PASSTHROUGH.value,
                strategy_chain=strategy_chain,
                compressor=None,
                reason=decision_reason,
                language=language,
                question=question,
                bias=bias,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                tokens_saved=0,
                compression_ratio=1.0,
                json_shape=_json_shape(content),
                input=content,
                output=content,
                error=error,
            )
        return content, original_tokens, strategy_chain

    def _try_ml_compressor(
        self, content: str, context: str, question: str | None = None
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
            compressor = self._get_kompress()
            if compressor:
                try:
                    result = compressor.compress(
                        text_to_compress,
                        context=context,
                        question=question,
                        target_ratio=self._runtime_target_ratio,
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

        Returns ``True`` iff, after a best-effort re-mirror, EVERY referenced
        hash is backed by a live store entry. Returns ``False`` if any sentinel
        is unbackable — the caller MUST then refuse to serve the stale cached
        output and recompute instead.

        Why this is needed: the Tier-2 result cache (30-min TTL) and BOTH CCR
        stores (Rust + Python, each 300-s TTL) have INDEPENDENT lifetimes. A
        result-cache HIT short-circuits ``smart_crush_content``, so the normal
        Rust→Python mirror never runs. After ~5 minutes BOTH CCR stores expire
        while the result cache still holds the crushed output: serving it would
        emit a ``<<ccr:HASH>>`` pointing to nothing — a signalled-but-
        unrecoverable drop (silent data loss).

        The re-mirror (``SmartCrusher._mirror_ccr_to_python_store``) refreshes
        the Python store from the Rust store when the Rust entry is still live.
        If the Rust entry is ALSO gone, the re-mirror is a no-op and the hash
        stays unbacked — this method reports that via ``False`` so the caller
        can recompute (a fresh compress re-creates + re-stores the backing).

        Best-effort on errors: a failure to verify is treated as unbacked
        (``False``) so the caller falls back to the safe recompute path.
        """
        hashes = self._extract_ccr_hashes(cached_compressed)
        if not hashes:
            # No sentinels → nothing to back → trivially safe to serve.
            return True

        crusher = self._get_smart_crusher()
        if crusher is not None:
            try:
                crusher._mirror_ccr_to_python_store(
                    rendered=cached_compressed,
                    strategy="result_cache_hit",
                    query_context=context,
                    tool_name=None,
                )
            except Exception as e:  # pragma: no cover - best effort
                logger.debug("_ensure_ccr_backed: mirror raised (non-fatal): %s", e)

        # Verify against the authoritative Python store: a sentinel is "backed"
        # only if `/v1/retrieve` would resolve it right now.
        try:
            from ..cache.compression_store import get_compression_store

            store = get_compression_store()
        except Exception as e:  # pragma: no cover - defensive
            # Cannot verify → assume unbacked, force the safe recompute path.
            logger.debug("_ensure_ccr_backed: cannot get compression_store (%s)", e)
            return False

        for h in hashes:
            if store.retrieve(h) is None:
                logger.debug(
                    "_ensure_ccr_backed: hash %s unbackable after re-mirror "
                    "(both CCR stores expired) — recompute required",
                    h,
                )
                return False
        return True

    @staticmethod
    def _extract_ccr_hashes(text: str) -> set[str]:
        """Collect every distinct ``<<ccr:HASH...>>`` hash in *text*.

        Reuses SmartCrusher's marker scanner so the parse grammar stays in one
        place (no regex; tolerates the row-drop, opaque-blob, and bare marker
        forms).
        """
        if "<<ccr:" not in text:
            return set()
        from .smart_crusher import SmartCrusher

        hashes: set[str] = set()
        try:
            parsed = json.loads(text)
            SmartCrusher._collect_ccr_hashes(parsed, hashes)
        except (json.JSONDecodeError, ValueError):
            SmartCrusher._collect_ccr_hashes_from_string(text, hashes)
        return hashes

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

    def eager_load_compressors(self) -> dict[str, str]:
        """Pre-load compressors at startup to avoid first-request latency.

        Call this during startup to load models and parsers
        before any requests arrive. Eliminates cold-start latency spikes.

        Returns:
            Dict of component name -> status string for logging.
        """
        status: dict[str, str] = {}

        # 1. ML text compressor: Kompress.
        #
        # Eager preload is cache-only (allow_download=False): on a cold cache we
        # must NOT trigger a network download here, because this runs on the
        # blocking startup path. A slow download stalls startup, and a hard
        # crash in the native download/ML stack (uncatchable SIGABRT) kills the
        # interpreter before it finishes initializing — so the host process
        # never becomes ready. When the model isn't cached we defer to first
        # use instead.
        if self.config.enable_kompress:
            from .kompress_compressor import KompressModelNotCached

            compressor = self._get_kompress()
            if compressor:
                if not hasattr(compressor, "preload"):
                    status["kompress"] = "enabled"
                    status["kompress_backend"] = "unknown"
                else:
                    try:
                        backend = compressor.preload(allow_download=False)
                    except KompressModelNotCached:
                        logger.info(
                            "Kompress model not cached; deferring download to "
                            "first use to keep startup non-blocking"
                        )
                        status["kompress"] = "deferred"
                    else:
                        logger.info("Kompress model pre-loaded at startup backend=%s", backend)
                        status["kompress"] = "enabled"
                        status["kompress_backend"] = str(backend)
            else:
                status["kompress"] = "unavailable"

        # 2. Magika content detector (avoids 100-200ms on first content detection)
        try:
            from ..compression.detector import _get_magika, _magika_available

            if _magika_available():
                _get_magika()  # Initializes the singleton
                logger.info("Magika content detector pre-loaded at startup")
                status["magika"] = "enabled"
            else:
                status["magika"] = "not installed"
        except Exception as e:
            logger.debug("Magika pre-load skipped: %s", e)
            status["magika"] = "skipped"

        # 3. SmartCrusher (lightweight init, but ensures import + TOIN ready)
        smart_crusher = self._get_smart_crusher()
        if smart_crusher:
            status["smart_crusher"] = "ready"

        return status

    def _get_kompress(self) -> Any:
        """Get KompressCompressor (lazy load). Downloads from HuggingFace on first use.

        Respects runtime kompress_model kwarg:
        - None: use default (chopratejas/kompress-v2-base) — cached on self
        - "disabled": return None (skip ML compression entirely)
        - any model ID string: create compressor with that model
          (model weights are cached at module level in kompress_compressor.py,
          so repeated calls with the same model_id are cheap)
        """
        model_id = self._runtime_kompress_model

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
        """
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
        # Store runtime options for access by _route_and_compress_block.
        # These write through to thread-local storage (see the
        # ``_runtime_*`` properties on __init__), so concurrent
        # compress() calls with different configs stay isolated.
        self._runtime_target_ratio = kwargs.get("target_ratio")
        self._runtime_force_kompress = bool(kwargs.get("force_kompress", False))
        self._runtime_kompress_model = kwargs.get("kompress_model")
        # Capture the per-request CompressionPolicy so
        # ``_record_to_toin`` can gate TOIN writes on
        # ``policy.toin_read_only``. ``None`` when the caller didn't
        # pass a policy — ``_record_to_toin`` treats that as "no gate"
        # for callers that don't set a policy.
        self._runtime_compression_policy = kwargs.get("compression_policy")

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

            # Two-tier compression cache.
            # Tier 1 (skip): known won't-compress → instant skip.
            # Tier 2 (result): known compresses → reuse compressed text.
            content_key = hash(content)

            # Tier 1: skip set — instant rejection
            if self._cache.is_skipped(content_key):
                result_slots[i] = message
                route_counts["ratio_too_high"] += 1
                route_counts.setdefault("cache_hit", 0)
                route_counts["cache_hit"] += 1
                continue

            # Tier 2: result cache — reuse compressed output
            cached = self._cache.get(content_key)
            if cached is not None:
                cached_compressed, cached_ratio, cached_strategy = cached
                # Re-check ratio against current min_ratio (shifts with context pressure)
                if cached_ratio < min_ratio:
                    # Invariant: every <<ccr:HASH>> in a served output must be
                    # backed by a live CCR store entry.  Both CCR stores (Rust +
                    # Python, 300s TTL) expire independently of this result cache
                    # (30-min TTL).  Re-mirror to refresh the backing; if a
                    # sentinel is unbackable (both CCR stores already expired),
                    # DO NOT serve the stale dead pointer — evict and recompute.
                    if self._ensure_ccr_backed(cached_compressed, context):
                        result_slots[i] = {**message, "content": cached_compressed}
                        transforms_applied.append(f"router:{cached_strategy}:{cached_ratio:.2f}")
                        compressed_details.append(f"{cached_strategy}:{cached_ratio:.2f}")
                        route_counts.setdefault("cache_hit", 0)
                        route_counts["cache_hit"] += 1
                        continue
                    # Unbackable sentinel — evict and fall through to recompute,
                    # which re-creates + re-stores the CCR backing.
                    self._cache.invalidate(content_key)
                    route_counts.setdefault("cache_stale_recompute", 0)
                    route_counts["cache_stale_recompute"] += 1
                    route_counts.setdefault("cache_miss", 0)
                    route_counts["cache_miss"] += 1
                    pending_tasks.append((i, content, context, msg_bias, content_key))
                    continue
                else:
                    # Threshold tightened — no longer qualifies. Move to skip.
                    self._cache.move_to_skip(content_key)
                    result_slots[i] = message
                    route_counts["ratio_too_high"] += 1
                    route_counts.setdefault("cache_hit", 0)
                    route_counts["cache_hit"] += 1
                    continue

            # Cache miss — defer to parallel compression pass
            route_counts.setdefault("cache_miss", 0)
            route_counts["cache_miss"] += 1
            pending_tasks.append((i, content, context, msg_bias, content_key))

        # --- Pass 2: Parallel compression of all cache-miss messages ---
        if pending_tasks:
            raw_workers = os.environ.get("HEADROOM_COMPRESS_WORKERS", "4")
            try:
                configured_workers = int(raw_workers)
            except ValueError:
                logger.warning(
                    "Invalid HEADROOM_COMPRESS_WORKERS=%r; using default 4", raw_workers
                )
                configured_workers = 4
            max_workers = min(len(pending_tasks), configured_workers)
            t_parallel_start = time.perf_counter()

            if max_workers <= 1 or len(pending_tasks) == 1:
                # Single task or parallelism disabled — compress inline
                task_results = []
                for _, task_content, task_ctx, task_bias, _ in pending_tasks:
                    t0 = time.perf_counter()
                    r = self.compress(task_content, context=task_ctx, bias=task_bias)
                    task_results.append((r, (time.perf_counter() - t0) * 1000))
            else:
                # Parallel compression via thread pool. Snapshot the per-request
                # runtime options on THIS (main) thread — worker threads have an
                # empty thread-local and would otherwise read the defaults.
                runtime_options = {
                    "target_ratio": self._runtime_target_ratio,
                    "force_kompress": self._runtime_force_kompress,
                    "kompress_model": self._runtime_kompress_model,
                    "compression_policy": self._runtime_compression_policy,
                }
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = []
                    for _, task_content, task_ctx, task_bias, _ in pending_tasks:
                        futures.append(
                            executor.submit(
                                self._timed_compress,
                                task_content,
                                task_ctx,
                                task_bias,
                                runtime_options,
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

                    # Two-tier compression cache
                    content_key = hash(tool_content)

                    # Tier 1: skip set — instant rejection
                    if self._cache.is_skipped(content_key):
                        new_blocks.append(block)
                        if route_counts is not None:
                            route_counts["ratio_too_high"] += 1
                            route_counts.setdefault("cache_hit", 0)
                            route_counts["cache_hit"] += 1
                        continue

                    # Tier 2: result cache — reuse compressed output
                    cached = self._cache.get(content_key)
                    if cached is not None:
                        cached_compressed, cached_ratio, cached_strategy = cached
                        if cached_ratio >= min_ratio:
                            # Threshold tightened — move to skip
                            self._cache.move_to_skip(content_key)
                            new_blocks.append(block)
                            if route_counts is not None:
                                route_counts["ratio_too_high"] += 1
                                route_counts.setdefault("cache_hit", 0)
                                route_counts["cache_hit"] += 1
                            continue
                        # Re-mirror CCR entries so the independent CCR TTL never
                        # leaves a served <<ccr:HASH>> sentinel unbacked. If a
                        # sentinel is unbackable (both CCR stores expired), evict
                        # and fall through to recompute rather than serve a dead
                        # pointer.
                        if self._ensure_ccr_backed(cached_compressed, context):
                            new_blocks.append({**block, "content": cached_compressed})
                            transforms_applied.append(f"router:tool_result:{cached_strategy}")
                            if compressed_details is not None:
                                compressed_details.append(
                                    f"tool:{cached_strategy}:{cached_ratio:.2f}"
                                )
                            any_compressed = True
                            if route_counts is not None:
                                route_counts.setdefault("cache_hit", 0)
                                route_counts["cache_hit"] += 1
                            continue
                        # Unbackable — evict and recompute below.
                        self._cache.invalidate(content_key)
                        if route_counts is not None:
                            route_counts.setdefault("cache_stale_recompute", 0)
                            route_counts["cache_stale_recompute"] += 1

                    # Cache miss — run full compression
                    if route_counts is not None:
                        route_counts.setdefault("cache_miss", 0)
                        route_counts["cache_miss"] += 1
                    t0 = time.perf_counter()
                    result = self.compress(tool_content, context=context, bias=bias)
                    compress_ms = (time.perf_counter() - t0) * 1000
                    if compressor_timing is not None:
                        key = f"compressor:{result.strategy_used.value}"
                        compressor_timing[key] = compressor_timing.get(key, 0.0) + compress_ms
                    if result.compression_ratio < min_ratio:
                        # Compressed — store in result cache
                        self._cache.put(
                            content_key,
                            result.compressed,
                            result.compression_ratio,
                            result.strategy_used.value,
                        )
                        new_blocks.append({**block, "content": result.compressed})
                        transforms_applied.append(
                            f"router:tool_result:{result.strategy_used.value}"
                        )
                        if compressed_details is not None:
                            compressed_details.append(
                                f"tool:{result.strategy_used.value}:{result.compression_ratio:.2f}"
                            )
                        any_compressed = True
                        continue
                    else:
                        # Didn't compress — add to skip set
                        self._cache.mark_skip(content_key)
                        if route_counts is not None:
                            route_counts["ratio_too_high"] += 1
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

                    content_key = hash(text_content)

                    # Tier 1: skip set
                    if self._cache.is_skipped(content_key):
                        new_blocks.append(block)
                        if route_counts is not None:
                            route_counts["ratio_too_high"] += 1
                            route_counts.setdefault("cache_hit", 0)
                            route_counts["cache_hit"] += 1
                        continue

                    # Tier 2: result cache
                    cached = self._cache.get(content_key)
                    if cached is not None:
                        cached_compressed, cached_ratio, cached_strategy = cached
                        if cached_ratio >= min_ratio:
                            self._cache.move_to_skip(content_key)
                            new_blocks.append(block)
                            if route_counts is not None:
                                route_counts["ratio_too_high"] += 1
                                route_counts.setdefault("cache_hit", 0)
                                route_counts["cache_hit"] += 1
                            continue
                        # Re-mirror CCR entries; if a sentinel is unbackable
                        # (both CCR stores expired), evict and recompute rather
                        # than serve a dead pointer.
                        if self._ensure_ccr_backed(cached_compressed, context):
                            new_blocks.append({**block, "text": cached_compressed})
                            transforms_applied.append(f"router:text_block:{cached_strategy}")
                            if compressed_details is not None:
                                compressed_details.append(
                                    f"text:{cached_strategy}:{cached_ratio:.2f}"
                                )
                            any_compressed = True
                            if route_counts is not None:
                                route_counts.setdefault("cache_hit", 0)
                                route_counts["cache_hit"] += 1
                            continue
                        # Unbackable — evict and recompute below.
                        self._cache.invalidate(content_key)
                        if route_counts is not None:
                            route_counts.setdefault("cache_stale_recompute", 0)
                            route_counts["cache_stale_recompute"] += 1

                    # Cache miss — full compression
                    if route_counts is not None:
                        route_counts.setdefault("cache_miss", 0)
                        route_counts["cache_miss"] += 1
                    t0 = time.perf_counter()
                    result = self.compress(text_content, context=context, bias=1.0)
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
                        new_blocks.append({**block, "text": result.compressed})
                        transforms_applied.append(f"router:text_block:{result.strategy_used.value}")
                        if compressed_details is not None:
                            compressed_details.append(
                                f"text:{result.strategy_used.value}:{result.compression_ratio:.2f}"
                            )
                        any_compressed = True
                        continue
                    else:
                        self._cache.mark_skip(content_key)
                        if route_counts is not None:
                            route_counts["ratio_too_high"] += 1
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


def route_and_compress(
    content: str,
    context: str = "",
) -> str:
    """Convenience function for one-off routing and compression.

    Args:
        content: Content to compress.
        context: Optional context for relevance-aware compression.

    Returns:
        Compressed content.

    Example:
        >>> compressed = route_and_compress(mixed_content)
    """
    router = ContentRouter()
    result = router.compress(content, context=context)
    return result.compressed
