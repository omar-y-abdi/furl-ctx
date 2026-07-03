"""Content router for intelligent compression strategy selection.

This module provides the ContentRouter, which analyzes content and routes it
to the optimal compressor. It handles mixed content by splitting, routing
each section to the appropriate compressor, and reassembling.

Supported Compressors:
- SmartCrusher: JSON arrays
- SearchCompressor: grep/ripgrep results
- LogCompressor: Build/test output
- (Plain text has no compressor and passes through; large uncompressible
  content is offloaded reversibly via the CCR store.)

Routing Strategy:
1. Use source hint if available (highest confidence)
2. Check for mixed content (split and route sections)
3. Detect content type (JSON, code, search, logs, text)
4. Route to appropriate compressor
5. Reassemble and return with routing metadata

Usage:
    >>> from furl_ctx.transforms import ContentRouter
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

import json
import logging
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

from ..ccr import marker_grammar
from ..ccr.tool_injection import CCR_TOOL_NAME
from ..config import (
    DEFAULT_EXCLUDE_TOOLS,
    CompressRequest,
    ReadLifecycleConfig,
    TransformResult,
    is_tool_excluded,
)
from ..tokenizer import Tokenizer
from ..utils import concat_text_parts
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
from .router_cache import CacheKey, CompressionCache
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

# CCR-offload fallback shape. Trigger: content at least _OFFLOAD_MIN_CHARS
# whose final ratio is >= _OFFLOAD_TRIGGER_RATIO (nothing meaningfully
# compressed). The preview constants are display budgets only — recovery is
# always the byte-exact original in the CCR store.
_OFFLOAD_MIN_CHARS = 4000
_OFFLOAD_TRIGGER_RATIO = 0.9
_OFFLOAD_PREVIEW_MAX_ROWS = 20
_OFFLOAD_PREVIEW_FIELD_CHARS = 120
_OFFLOAD_PREVIEW_HEAD_LINES = 12
_OFFLOAD_PREVIEW_TAIL_LINES = 4

# Engine-emitted retrieval hints always carry a real 12/24-hex hash; content
# that merely talks about the grammar (docs, this repo's own source read back
# as tool output) uses placeholders and never matches.
_RETRIEVE_HINT_PATTERN = re.compile(
    r"Retrieve (?:more|original): hash=[0-9a-fA-F]{12}(?:[0-9a-fA-F]{12})?(?![0-9a-fA-F])"
)

# Genuine analysis verbs (and analysis-question phrases) only, matched on
# word boundaries (COR-16). The previous substring scan over a much broader
# set — fix/error/bug/issue/problem/wrong/broken/improve/"clean up"/
# security/vulnerability — tripped on virtually every coding-agent message
# ("fix" matched *prefix*, "error" matched any mention), so analysis_intent
# was ~always true and SOURCE_CODE was ~never compressed (protection 3).
# refactor/optimize stay: precise code-work verbs with low ambient
# frequency whose requests need full code fidelity.
_ANALYSIS_INTENT_KEYWORDS: tuple[str, ...] = (
    "analyze",
    "analyse",
    "audit",
    "debug",
    "explain",
    "how does",
    "inspect",
    "optimize",
    "refactor",
    "review",
    "understand",
    "what does",
)

_ANALYSIS_INTENT_PATTERN = re.compile(
    r"\b(?:"
    + "|".join(
        r"\s+".join(map(re.escape, keyword.split())) for keyword in _ANALYSIS_INTENT_KEYWORDS
    )
    + r")\b"
)

# Roles whose message content is a tool output. OpenAI's current API uses
# ``tool``; the legacy function-calling API uses ``function`` (name carried on
# ``message["name"]``, no tool_call_id). Mirrors cross_message_dedup's
# eligibility set so the router's protection gates (excluded tools, error
# outputs, tool bias) fire for BOTH shapes (COR-48).
_TOOL_ROLES = frozenset({"tool", "function"})

# Retrieval-loop guard (P0-5): the CCR retrieval tool's outputs ARE the
# originals the engine previously compressed. Compressing them again would
# mint a fresh retrieval marker for content the model just asked to see —
# a compress → retrieve → compress ping-pong. These names are excluded
# UNCONDITIONALLY: unioned into every effective exclusion set (even a
# caller-supplied ``exclude_tools`` override, including ``set()``) and
# immune to the read-protection-window age decay. Covers both retrieval
# channels: direct tool injection (``furl_retrieve``) and the MCP server
# under any server alias (``mcp__<server>__furl_retrieve``).
ALWAYS_EXCLUDE_TOOLS: frozenset[str] = frozenset(
    {
        CCR_TOOL_NAME,
        f"mcp__*__{CCR_TOOL_NAME}",
    }
)


def _is_retrieval_tool(tool_name: str) -> bool:
    """True iff *tool_name* is the CCR retrieval tool on any channel."""
    return is_tool_excluded(tool_name, ALWAYS_EXCLUDE_TOOLS)


def _word_count(text: str) -> int:
    """Whitespace word count — the compression plane's historical token proxy.

    The default unit for ``compress()`` when no ``token_counter`` is threaded
    in. ``apply()`` passes the request's real ``tokenizer.count_text`` so the
    acceptance gate (``compression_ratio < min_ratio``) compares like units —
    word-ratios systematically overstate savings on compaction outputs (CSV,
    comma-joined) that have few spaces (COR-17).
    """
    return len(text.split())


def _result_cache_key(content: str, bias: float) -> CacheKey:
    """Build the two-tier result-cache key for one compression unit.

    Identity is (content, per-request options), not content alone (COR-18):

    * ``hash(content)`` + ``len(content)`` approximate content identity. The
      length rides IN the key, so dict key equality turns a 64-bit SipHash
      collision from silent byte-substitution (serving another message's
      compressed bytes) into a plain cache miss.
    * The rounded ``bias`` changes what ``compress()`` would produce, so a
      hit computed under one bias is never served under another.

    ``context`` is deliberately NOT in the key: it changes every turn in
    agent traffic (keying on it would collapse the hit rate the cache exists
    to provide), ``min_ratio`` is re-checked on every Tier-2 hit, and the
    CCR backing is re-verified against the CURRENT context — the served
    bytes remain a valid, recoverable compression of the same content;
    context only tunes relevance ranking. (CrossMessageDeduper sets the
    same precedent: identical bytes dedup identically regardless of query.)
    """
    return (hash(content), len(content), round(bias, 3))


def _is_unstructured_error_output(content: str) -> bool:
    """True when *content* is a raw error dump that must ship verbatim.

    Structured JSON is never a traceback: rows that merely mention errors
    (git logs full of ``fix:`` subjects) route to SmartCrusher, whose
    error-keyword preservation keeps genuine error rows visible.
    """
    if not content_has_strong_error_indicators(content):
        return False
    if content.lstrip()[:1] in ("[", "{"):
        try:
            json.loads(content)
            return False
        except (json.JSONDecodeError, ValueError):
            pass
    return True


def _looks_like_ccr_output(content: str) -> bool:
    """True when *content* carries a real engine-emitted CCR marker (strict
    grammar — content that merely mentions the marker text stays
    compressible)."""
    return bool(
        _RETRIEVE_HINT_PATTERN.search(content)
        or marker_grammar.DOUBLE_ANGLE_PATTERN.search(content)
    )


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

    Stage 1 (primary): `furl_ctx._core.detect_content_type` (the Rust
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
    from furl_ctx._core import detect_content_type as _rust_detect

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
            hit it's a single entry; for the SMART_CRUSHER → LOG
            fallback chain it's two. Lets log readers see *how*
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
        mixed_content_threshold: Min distinct types to consider "mixed".
        min_section_tokens: Minimum tokens for a section to compress.
        fallback_strategy: Strategy when no compressor matches.
        skip_user_messages: Never compress user messages (they're the subject).
        skip_recent_messages: Don't compress last N messages (likely the subject).
        protect_analysis_context: Detect "analyze/review" intent, skip compression.
    """

    # Enable/disable specific compressors
    enable_smart_crusher: bool = True
    enable_search_compressor: bool = True
    enable_log_compressor: bool = True

    # Routing preferences
    mixed_content_threshold: int = 2  # Min types to consider mixed
    min_section_tokens: int = 20  # Min tokens to compress a section

    # Fallback: unknown content types pass through unchanged (every current
    # ContentType has an explicit strategy mapping, so this is a safety net).
    fallback_strategy: CompressionStrategy = CompressionStrategy.PASSTHROUGH

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

    # CCR (Compress-Cache-Retrieve) retrieval-tool preference.
    #
    # HONEST SEMANTICS (these flags are NOT a data-loss switch): recovery
    # pointers are UNCONDITIONAL on every drop — the lossy row-drop
    # `<<ccr:HASH>>` sentinel, the opaque-substitution pointer, and the
    # log/search/diff `Retrieve …: hash=…` marker lines are all emitted
    # regardless of these flags, and the backing store writes happen
    # regardless too (a drop without its pointer is silent loss, which the
    # recovery invariant forbids — Defect 1, pinned by
    # tests/test_ccr_recovery_invariant.py). What the flags actually do:
    #   * gate the reversible CCR-offload fallback (`_should_ccr_offload`
    #     — offload is optional, so "CCR off" honestly disables it);
    #   * flow to `CCRConfig` / the Rust `enable_ccr_marker` field as the
    #     retrieval-tool advertisement preference, preserved for external
    #     embedders that read it back when deciding whether to inject the
    #     `furl_retrieve` tool.
    # To ship output with NO `<<ccr:` pointers at all, use
    # `lossless_only=True` below — that mode never drops, so there is
    # nothing to point at.
    ccr_enabled: bool = True
    ccr_inject_marker: bool = True
    smart_crusher_max_items_after_crush: int | None = None
    smart_crusher_with_compaction: bool = True
    # Routing policy for the lossless-vs-lossy-recoverable choice (both
    # recoverable, so no information is lost). ``"min-tokens"`` (default)
    # ships whichever render is fewer tokens; ``"lossless-first"`` keeps
    # the legacy lossless-wins-on-byte-ratio behavior.
    smart_crusher_routing_policy: str = "min-tokens"

    # STRICT lossless-or-passthrough mode (default OFF — behavior
    # unchanged). When True, only proven-lossless transforms run: JSON
    # arrays are either replaced by a decoder-verifiable, opaque-free
    # lossless render (SmartCrusher's compaction tier) or passed through
    # untouched, and the lossy compressor routes (search / log / diff —
    # all of which drop lines) plus the CCR-offload fallback resolve to
    # passthrough. Output carries NO ``<<ccr:`` pointers and no
    # ``Retrieve …: hash=…`` marker lines because nothing is ever
    # dropped or substituted. For users who cannot tolerate ANY visible
    # information reduction, even a CCR-recoverable one.
    lossless_only: bool = False

    # Last-resort reversible offload: large content no compressor could
    # shrink is stored byte-exact in the CCR store and ships as an identity
    # preview + retrieval marker (see _ccr_offload). Requires ccr_enabled —
    # offloading without the recovery plane would be silent loss.
    ccr_offload_fallback: bool = True

    # Tools to exclude from compression (output passed through unmodified)
    # Set to None to use DEFAULT_EXCLUDE_TOOLS, or provide custom set.
    # Entries match case-insensitively and may be fnmatch-style globs
    # (e.g. "mcp__*"). The CCR retrieval tool (ALWAYS_EXCLUDE_TOOLS) is
    # excluded unconditionally on top of whatever is configured here —
    # overriding this field cannot re-enable retrieval-output compression.
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
#   1. Keys apply() itself READS directly via ``kwargs.get(...)``.
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

    ContentRouter is the recommended entry point for Furl's compression.
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
        >>> # Source code ships unmangled (passthrough)
        >>> result = router.compress(python_code)
        >>> print(result.strategy_used)  # CompressionStrategy.PASSTHROUGH
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
            observer: Optional duck-typed observer called once per
                routing decision after `compress()` finishes. Must
                expose `record_compression(...)` (and may expose
                `record_router_route_counts(...)`); exceptions it
                raises are swallowed so a buggy observer can't break
                compression. `None` disables observation.
        """
        self.config = config or ContentRouterConfig()
        self._observer = observer

        # Lazy-loaded compressors.
        #
        # The four SELF-CONTAINED factories (SmartCrusher, Search, Log, Diff)
        # read only ``self.config`` and cache their instance — they live
        # in ``CompressorRegistry``. The ``_get_*`` methods below delegate
        # to it.
        self._registry = CompressorRegistry(self.config)
        # Per-strategy dispatch + no-savings fallback chain. Holds no router
        # reference: the compressor getters are passed per-call by the
        # ``_apply_strategy_to_content`` delegator, so monkeypatching those
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

        self._cache = CompressionCache()

    def _timed_compress(
        self,
        content: str,
        context: str,
        bias: float,
        token_counter: Callable[[str], int] | None = None,
    ) -> tuple[RouterCompressionResult, float]:
        """Compress with wall-clock timing.  Used by parallel executor."""
        t0 = time.perf_counter()
        result = self.compress(content, context=context, bias=bias, token_counter=token_counter)
        return result, (time.perf_counter() - t0) * 1000

    def compress(
        self,
        content: str,
        context: str = "",
        question: str | None = None,
        bias: float = 1.0,
        *,
        token_counter: Callable[[str], int] | None = None,
    ) -> RouterCompressionResult:
        """Compress content using optimal strategy based on content detection.

        Args:
            content: Content to compress.
            context: Optional context for relevance-aware compression.
            question: Optional question for QA-aware compression. When provided,
                tokens relevant to answering this question are preserved.
            bias: Compression bias multiplier (>1 = keep more, <1 = keep fewer).
            token_counter: Optional real token counter (COR-17). ``apply()``
                threads the request's ``tokenizer.count_text`` so routing-log
                token counts — and therefore ``compression_ratio``, the value
                the ``min_ratio`` acceptance gate reads — are measured in the
                same unit as the gate's threshold. ``None`` (direct callers)
                keeps the historical whitespace word count.

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
            strategy = self._determine_strategy(content)
            if debug_enabled:
                mixed = is_mixed_content(content)
                detection = _detect_content(content)
                _log_router_debug(
                    "content_router_input",
                    **request_debug,
                    detected_content_type=detection.content_type.value,
                    detection_confidence=detection.confidence,
                    selected_strategy=strategy.value,
                    selection_reason=("mixed_content" if mixed else "content_detection"),
                )

            if strategy == CompressionStrategy.MIXED:
                result = self._compress_mixed(
                    content,
                    context,
                    question,
                    bias=bias,
                    token_counter=token_counter,
                )
            else:
                result = self._compress_pure(
                    content,
                    strategy,
                    context,
                    question,
                    bias=bias,
                    token_counter=token_counter,
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

        # Last-resort reversible offload for content nothing above could shrink.
        if self._should_ccr_offload(content, result):
            offloaded = self._ccr_offload(content, context, result, token_counter=token_counter)
            if offloaded is not None:
                result = offloaded

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

    def _should_ccr_offload(self, content: str, result: RouterCompressionResult) -> bool:
        """Offload only large content the strategy chain left essentially
        uncompressed, when the CCR recovery plane is on, and never for
        content that already carries a real marker (its producer owns
        recovery). Never under ``lossless_only`` — the offload replaces
        visible content with a preview + pointer, which is exactly the
        (recoverable) information reduction strict mode forbids."""
        cfg = self.config
        return (
            cfg.ccr_offload_fallback
            and cfg.ccr_enabled
            and cfg.ccr_inject_marker
            and not cfg.lossless_only
            and len(content) >= _OFFLOAD_MIN_CHARS
            and result.compression_ratio >= _OFFLOAD_TRIGGER_RATIO
            and not _looks_like_ccr_output(content)
        )

    def _ccr_offload(
        self,
        content: str,
        context: str,
        prior: RouterCompressionResult,
        token_counter: Callable[[str], int] | None = None,
    ) -> RouterCompressionResult | None:
        """Store *content* byte-exact in the CCR compression store and ship
        an identity preview + ``{"_ccr_dropped": "<<ccr:HASH>>"}`` sentinel +
        ``Retrieve more`` marker instead.

        Fail-open: returns ``None`` (caller keeps the uncompressed result)
        unless a verified store round-trip guarantees byte-exact recovery —
        the marker is never emitted for content the store cannot reproduce.
        """
        rows, n_items = self._build_offload_preview(content)
        preview = json.dumps(rows, ensure_ascii=False) if isinstance(rows, list) else rows
        try:
            from ..cache.compression_store import get_compression_store

            store = get_compression_store()
            ccr_hash = store.store(
                original=content,
                compressed=preview,
                original_tokens=len(content.split()),
                compressed_tokens=len(preview.split()),
                original_item_count=n_items,
                query_context=context or None,
                compression_strategy=CompressionStrategy.CCR_OFFLOAD.value,
            )
            entry = store.retrieve(ccr_hash)
            if entry is None or entry.original_content != content:
                logger.warning("ccr_offload: round-trip failed for %s; keeping original", ccr_hash)
                return None
        except Exception:
            logger.warning("ccr_offload: store unavailable; keeping original", exc_info=True)
            return None

        # Both marker grammars: the <<ccr:HASH>> pointer every CCR consumer
        # walks, and the bracket form tool-injection describes to the LLM —
        # which also pins this output against recompression on later turns.
        sentinel = {
            "_ccr_dropped": (
                f"{marker_grammar.CCR_PREFIX}{ccr_hash}>> "
                f"[{n_items} items compressed to 0. Retrieve more: hash={ccr_hash}]"
            )
        }
        if isinstance(rows, list):
            compressed = json.dumps([*rows, sentinel], ensure_ascii=False)
        else:
            compressed = preview + "\n" + json.dumps(sentinel, ensure_ascii=False)

        logger.info(
            "ccr_offload: %d chars (%d items) stored as %s", len(content), n_items, ccr_hash
        )
        count = token_counter or _word_count
        return RouterCompressionResult(
            compressed=compressed,
            original=content,
            strategy_used=CompressionStrategy.CCR_OFFLOAD,
            strategy_chain=[*prior.strategy_chain, CompressionStrategy.CCR_OFFLOAD.value],
            routing_log=[
                RoutingDecision(
                    content_type=self._content_type_from_strategy(prior.strategy_used),
                    strategy=CompressionStrategy.CCR_OFFLOAD,
                    original_tokens=count(content),
                    compressed_tokens=count(compressed),
                )
            ],
        )

    @staticmethod
    def _build_offload_preview(content: str) -> tuple[list[Any] | str, int]:
        """Identity preview of *content*: for a JSON array of objects, the
        leading rows with long string fields truncated (paths/ids stay
        verbatim); otherwise the head/tail lines. Returns ``(rows | text,
        n_items)``. Never reversible — recovery is the stored original."""
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, list) and parsed and all(isinstance(item, dict) for item in parsed):
            rows: list[Any] = []
            for item in parsed[:_OFFLOAD_PREVIEW_MAX_ROWS]:
                row: dict[str, Any] = {}
                for k, v in item.items():
                    if isinstance(v, str) and len(v) > _OFFLOAD_PREVIEW_FIELD_CHARS:
                        row[k] = v[:_OFFLOAD_PREVIEW_FIELD_CHARS] + f"… [{len(v)} chars, in CCR]"
                    elif isinstance(v, (str, int, float, bool)) or v is None:
                        row[k] = v
                    else:
                        row[k] = f"[{type(v).__name__} omitted, in CCR]"
                rows.append(row)
            if len(parsed) > len(rows):
                rows.append({"_preview": f"first {len(rows)} of {len(parsed)} rows"})
            return rows, len(parsed)

        lines = content.splitlines()
        head, tail = _OFFLOAD_PREVIEW_HEAD_LINES, _OFFLOAD_PREVIEW_TAIL_LINES
        if len(lines) <= head + tail:
            return content, 1
        return (
            "\n".join(lines[:head])
            + f"\n… [{len(lines) - head - tail} lines omitted, in CCR] …\n"
            + "\n".join(lines[-tail:]),
            1,
        )

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
        token_counter: Callable[[str], int] | None = None,
    ) -> RouterCompressionResult:
        """Compress mixed content by splitting and routing sections.

        Args:
            content: Mixed content to compress.
            context: User context for relevance.
            question: Optional question for QA-aware compression.
            bias: Compression bias multiplier.
            token_counter: Optional real token counter (see ``compress``).

        Returns:
            RouterCompressionResult with reassembled content. When NO section
            actually changed, the ORIGINAL string is returned verbatim as
            PASSTHROUGH (COR-30): reassembly (``"\\n\\n"`` join, re-synthesized
            fences, dropped whitespace-only sections) is not byte-faithful, so
            shipping it at ~zero savings would mutate bytes for nothing.
        """
        count = token_counter or _word_count
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
        any_section_changed = False

        for i, section in enumerate(sections):
            # Get strategy for this section
            strategy = self._strategy_from_detection_type(section.content_type)

            # Compress section
            original_tokens = count(section.content)
            compressed_content, compressed_tokens, _section_chain = self._apply_strategy_to_content(
                section.content,
                strategy,
                context,
                section.language,
                question,
                bias=bias,
                token_counter=token_counter,
            )
            if compressed_content != section.content:
                any_section_changed = True

            # Preserve code fence markers. The fence bytes SHIP, so they are
            # counted AFTER wrapping (COR-30) — counting the bare section
            # undercounted fenced output and overstated savings.
            if section.is_code_fence and section.language:
                compressed_content = f"```{section.language}\n{compressed_content}\n```"
                compressed_tokens = count(compressed_content)

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

        # No section changed → reassembly would only mutate bytes (join
        # normalization, re-synthesized fences) at ~zero savings. Return the
        # original verbatim as PASSTHROUGH, with each decision rewritten to
        # passthrough metrics so derived savings honestly report 0 (COR-30;
        # same honesty rewrite as the empty-output guard in ``compress``).
        if not any_section_changed:
            return RouterCompressionResult(
                compressed=content,
                original=content,
                strategy_used=CompressionStrategy.PASSTHROUGH,
                routing_log=[
                    replace(decision, compressed_tokens=decision.original_tokens)
                    for decision in routing_log
                ],
                sections_processed=len(sections),
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
        token_counter: Callable[[str], int] | None = None,
    ) -> RouterCompressionResult:
        """Compress pure (non-mixed) content.

        Args:
            content: Content to compress.
            strategy: Selected strategy.
            context: User context.
            question: Optional question for QA-aware compression.
            bias: Compression bias multiplier.
            token_counter: Optional real token counter (see ``compress``).

        Returns:
            RouterCompressionResult.
        """
        original_tokens = (token_counter or _word_count)(content)

        compressed, compressed_tokens, strategy_chain = self._apply_strategy_to_content(
            content,
            strategy,
            context,
            question=question,
            bias=bias,
            token_counter=token_counter,
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
        token_counter: Callable[[str], int] | None = None,
    ) -> tuple[str, int, list[str]]:
        """Apply a compression strategy to content.

        Thin delegator to :meth:`StrategyDispatcher.apply`. The compressor
        getters are resolved fresh here on every call and passed in, so
        monkeypatching those router methods still takes effect (a
        construction-time capture in the dispatcher would have been stale).

        Args:
            content: Content to compress.
            strategy: Strategy to use.
            context: User context.
            language: Language hint for code.
            question: Optional question for QA-aware compression.
            bias: Compression bias multiplier (>1 = keep more, <1 = keep fewer).
            token_counter: Optional real token counter forwarded to the
                dispatcher (see ``compress``); ``None`` keeps word counts.

        Returns:
            Tuple of (compressed_content, compressed_token_count,
            strategy_chain). The chain lists every strategy attempted
            in order — first the requested one, then any fallbacks.
            Single-entry chain means a direct hit; multi-entry means
            the fallback chain fired (e.g. ``[smart_crusher, log]``).
            Log readers use this to see *how* we got to the final
            compressor without parsing decision_reason strings.
        """
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
            token_counter=token_counter,
        )

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

            # OpenAI format: tool_calls array. `or []`: the key is
            # present-but-None in openai-python model_dump() output —
            # iterating None would TypeError and fail-open every request.
            for tc in msg.get("tool_calls") or []:
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

        # Real message-shape counting (COR-39): ``count_messages`` handles
        # block-list content part-by-part (text payloads, image budgets,
        # tool_result payloads). The old ``count_text(str(content))`` tokenized
        # the Python repr of block lists — inflated fictions that also skewed
        # the context_pressure → min_ratio derivation below.
        tokens_before = tokenizer.count_messages(messages)
        context = kwargs.get("context", "")
        hook_biases: dict[int, float] = kwargs.get("biases") or {}

        # Build tool name map for exclusion checking
        tool_name_map = self._build_tool_name_map(messages)

        # Compute excluded tool IDs based on config. The CCR retrieval tool
        # is unioned in UNCONDITIONALLY (retrieval-loop guard) — a caller
        # override, even ``exclude_tools=set()``, must not re-enable
        # compress→retrieve→compress ping-pong. New frozenset: the caller's
        # set is never mutated. Matching is case-insensitive with
        # fnmatch-style glob support (is_tool_excluded).
        exclude_tools = (
            frozenset(self.config.exclude_tools)
            if self.config.exclude_tools is not None
            else DEFAULT_EXCLUDE_TOOLS
        ) | ALWAYS_EXCLUDE_TOOLS
        excluded_tool_ids = {
            tool_id
            for tool_id, name in tool_name_map.items()
            if is_tool_excluded(name, exclude_tools)
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
        compressed_details: list[str] = []  # e.g. ["smart_crusher:0.42", "log:0.65"]

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
        _PendingTask = tuple[int, str, str, float, CacheKey]
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
                # Message-level exclusion for OpenAI-style tool/function
                # messages whose content is a parts LIST (COR-48): the block
                # walker below checks exclusion only via block-level
                # ``tool_use_id``, which this shape doesn't carry — without
                # this gate, excluded-tool protection vanished for it.
                # Honors the same age-based decay as the string path.
                if role in _TOOL_ROLES:
                    tool_call_id = message.get("tool_call_id", "")
                    tool_name = tool_name_map.get(tool_call_id, "") or str(
                        message.get("name", "") or ""
                    )
                    if (
                        tool_call_id in excluded_tool_ids
                        or (tool_name and is_tool_excluded(tool_name, exclude_tools))
                    ) and (
                        messages_from_end <= read_protection_window
                        # Retrieval-loop guard: retrieval outputs never age
                        # out of protection (recompression re-opens the loop).
                        or _is_retrieval_tool(tool_name)
                    ):
                        result_slots[i] = message
                        transforms_applied.append("router:excluded:tool")
                        route_counts["excluded_tool"] += 1
                        continue
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
                    token_counter=tokenizer.count_text,
                )
                result_slots[i] = transformed_message
                route_counts["content_blocks"] += 1
                continue

            # Skip non-string content (other types)
            if not isinstance(content, str):
                result_slots[i] = message
                route_counts["non_string"] += 1
                continue

            # Skip OpenAI-style tool/function messages for excluded tools
            # BUT: allow compression of old excluded-tool outputs beyond the
            # adaptive protection window (age-based decay). Legacy
            # function-role messages carry no tool_call_id — their name rides
            # on ``message["name"]`` (COR-48), which also backstops tool-role
            # messages whose call id was never mapped.
            if role in _TOOL_ROLES:
                tool_call_id = message.get("tool_call_id", "")
                tool_name = tool_name_map.get(tool_call_id, "") or str(
                    message.get("name", "") or ""
                )
                if tool_call_id in excluded_tool_ids or (
                    tool_name and is_tool_excluded(tool_name, exclude_tools)
                ):
                    if messages_from_end <= read_protection_window or _is_retrieval_tool(tool_name):
                        # Recent — protect as before. Retrieval-tool outputs
                        # are protected regardless of age: recompressing them
                        # re-opens the compress→retrieve→compress loop.
                        result_slots[i] = message
                        transforms_applied.append("router:excluded:tool")
                        route_counts["excluded_tool"] += 1
                        continue
                    # Old excluded-tool output — fall through to compression
                    # (the LLM is unlikely to need exact content from this far back,
                    # and CCR provides retrieval if it does)
                # Look up tool-specific compression bias for tool/function messages
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
                and role in _TOOL_ROLES
                and len(content) <= self.config.error_protection_max_chars
                and _is_unstructured_error_output(content)
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
            # (carries a real engine-emitted CCR retrieval marker), skip
            # recompression. Recompressing would change byte content and
            # break provider prefix caching with no meaningful further
            # reduction. Strict grammar match — raw content that merely
            # MENTIONS the marker text (docs, or this engine's own source
            # read back as a tool output) is not pinned and stays
            # compressible.
            if _looks_like_ccr_output(content):
                result_slots[i] = message
                route_counts.setdefault("already_compressed", 0)
                route_counts["already_compressed"] += 1
                continue

            # Route and compress based on content detection
            # Merge tool-specific bias with hook-provided bias (multiplicative)
            msg_bias = bias if role in _TOOL_ROLES else 1.0
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
            # test_result_cache_ccr_divergence.py. The key carries the
            # per-request bias and a length guard — see _result_cache_key
            # (COR-18).
            content_key = _result_cache_key(content, msg_bias)
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
            raw_workers = os.environ.get("FURL_COMPRESS_WORKERS", "4")
            try:
                configured_workers = int(raw_workers)
            except ValueError:
                logger.warning("Invalid FURL_COMPRESS_WORKERS=%r; using default 4", raw_workers)
                configured_workers = 4
            max_workers = min(len(pending_tasks), configured_workers)
            t_parallel_start = time.perf_counter()

            if max_workers <= 1 or len(pending_tasks) == 1:
                # Single task or parallelism disabled — compress inline
                task_results = []
                for _, task_content, task_ctx, task_bias, _ in pending_tasks:
                    t0 = time.perf_counter()
                    r = self.compress(
                        task_content,
                        context=task_ctx,
                        bias=task_bias,
                        token_counter=tokenizer.count_text,
                    )
                    task_results.append((r, (time.perf_counter() - t0) * 1000))
            else:
                # Parallel compression via thread pool. Each compress() call
                # is independent; per-task inputs are passed by argument.
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = []
                    for _, task_content, task_ctx, task_bias, _ in pending_tasks:
                        futures.append(
                            executor.submit(
                                self._timed_compress,
                                task_content,
                                task_ctx,
                                task_bias,
                                tokenizer.count_text,
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

        tokens_after = tokenizer.count_messages(transformed_messages)

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
        if route_counts.get("nested_blocks"):
            parts.append(f"{route_counts['nested_blocks']} nested-block tool_results")
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
        content_key: CacheKey,
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
            # a live CCR store entry. Both CCR stores (Rust + Python, 1800s TTL)
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
        transforms_applied: list[str],
        route_counts: dict[str, int] | None,
        compressed_details: list[str] | None,
        compressor_timing: dict[str, float] | None,
        token_counter: Callable[[str], int] | None = None,
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

        content_key = _result_cache_key(text, bias)
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
        result = self.compress(text, context=context, bias=bias, token_counter=token_counter)
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

    def _compress_nested_tool_result(
        self,
        block: dict[str, Any],
        parts: list[Any],
        *,
        context: str,
        min_ratio: float,
        bias: float,
        transforms_applied: list[str],
        route_counts: dict[str, int] | None,
        compressed_details: list[str] | None,
        compressor_timing: dict[str, float] | None,
        min_chars: int,
        token_counter: Callable[[str], int] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Compress the inner ``type=="text"`` parts of a nested ``tool_result``
        (COR-47). Returns ``(new_block, did_compress)``.

        The canonical Anthropic/MCP tool_result shape carries its payload as
        ``content: [{"type": "text", "text": …}]``. Each qualifying inner text
        part routes through :meth:`_compress_content_block` with
        ``block_key="text"`` — the SAME two-tier cache the flat shapes use, so
        identical payloads share entries across shapes. Every part-level
        protection mirrors the string-content branch: the block-level
        ``is_error`` flag protects the whole block; per part, the error
        indicator scan, the ``min_chars`` floor, the already-compressed
        pinning, and a ``cache_control`` guard all apply. Non-text parts
        (images, …) always ship untouched. The caller has already run the
        block-level exclusion / cache_control gates.
        """

        def bump(*keys: str) -> None:
            if route_counts is not None:
                for k in keys:
                    route_counts[k] = route_counts.get(k, 0) + 1

        bump("nested_blocks")

        # Anthropic's explicit failure flag protects the whole block — the
        # string-content branch checks it before reaching compression; the
        # nested shape must not lose that protection.
        if self.config.protect_error_outputs and block.get("is_error") is True:
            transforms_applied.append("router:protected:error_output")
            bump("error_protected")
            return block, False

        new_parts: list[Any] = []
        any_did = False
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "text" or "cache_control" in part:
                new_parts.append(part)
                continue
            part_text = part.get("text", "")
            if not isinstance(part_text, str) or len(part_text) <= min_chars:
                new_parts.append(part)
                continue
            # Error indicator scan (mirror of the string-content branch).
            if (
                self.config.protect_error_outputs
                and len(part_text) <= self.config.error_protection_max_chars
                and _is_unstructured_error_output(part_text)
            ):
                new_parts.append(part)
                transforms_applied.append("router:protected:error_output")
                bump("error_protected")
                continue
            # Compression pinning (strict marker grammar).
            if _looks_like_ccr_output(part_text):
                new_parts.append(part)
                bump("already_compressed")
                continue
            new_part, did = self._compress_content_block(
                part,
                part_text,
                block_key="text",
                label="tool_result",
                detail_prefix="tool",
                context=context,
                min_ratio=min_ratio,
                bias=bias,
                transforms_applied=transforms_applied,
                route_counts=route_counts,
                compressed_details=compressed_details,
                compressor_timing=compressor_timing,
                token_counter=token_counter,
            )
            new_parts.append(new_part)
            any_did = any_did or did

        if any_did:
            return {**block, "content": new_parts}, True
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
        token_counter: Callable[[str], int] | None = None,
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
            token_counter: Optional real token counter threaded into
                ``compress()`` (see ``compress``); ``None`` keeps word counts.

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
                    if messages_from_end <= read_protection_window or _is_retrieval_tool(
                        (tool_name_map or {}).get(tool_use_id, "")
                    ):
                        # Recent — protect as before. Retrieval-tool outputs
                        # never age out (retrieval-loop guard).
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
                # AND an unstructured (non-JSON) shape, so benign row data
                # mentioning errors doesn't skip compression.
                # Above the size cap, fall through — LogCompressor preserves
                # error lines in big logs.
                if (
                    self.config.protect_error_outputs
                    and isinstance(tool_content, str)
                    and len(tool_content) <= self.config.error_protection_max_chars
                    and (
                        block.get("is_error") is True or _is_unstructured_error_output(tool_content)
                    )
                ):
                    new_blocks.append(block)
                    transforms_applied.append("router:protected:error_output")
                    if route_counts is not None:
                        route_counts.setdefault("error_protected", 0)
                        route_counts["error_protected"] += 1
                    continue

                # String content: the flat tool_result payload shape.
                if isinstance(tool_content, str) and len(tool_content) > min_chars:
                    # Compression pinning: skip already-compressed content
                    # (strict marker grammar — see _looks_like_ccr_output).
                    if _looks_like_ccr_output(tool_content):
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
                        transforms_applied=transforms_applied,
                        route_counts=route_counts,
                        compressed_details=compressed_details,
                        compressor_timing=compressor_timing,
                        token_counter=token_counter,
                    )
                    new_blocks.append(new_block)
                    any_compressed = any_compressed or did
                    continue
                elif isinstance(tool_content, list):
                    # Nested parts list — the canonical Anthropic/MCP
                    # ``content: [{"type":"text","text": …}]`` shape (COR-47).
                    # Route each inner text part through the same two-tier
                    # cache; non-text parts (images, …) ship untouched.
                    # Previously this shape was booked as route_counts["small"]
                    # and never compressed.
                    new_block, did = self._compress_nested_tool_result(
                        block,
                        tool_content,
                        context=context,
                        min_ratio=min_ratio,
                        bias=bias,
                        transforms_applied=transforms_applied,
                        route_counts=route_counts,
                        compressed_details=compressed_details,
                        compressor_timing=compressor_timing,
                        min_chars=min_chars,
                        token_counter=token_counter,
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
                    # Pinning: skip already-compressed content. The loose
                    # phrase substrings are kept for back-compat;
                    # _looks_like_ccr_output additionally pins engine-emitted
                    # ``<<ccr:HASH>>`` sentinels, which the smart-crusher path
                    # emits WITHOUT either phrase at default config (COR-31) —
                    # phrase-only pinning re-compressed those after result-cache
                    # expiry, and sentinel survival through a second crush is
                    # not contractual.
                    if (
                        "Retrieve more: hash=" in text_content
                        or "Retrieve original: hash=" in text_content
                        or _looks_like_ccr_output(text_content)
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
                        transforms_applied=transforms_applied,
                        route_counts=route_counts,
                        compressed_details=compressed_details,
                        compressor_timing=compressor_timing,
                        token_counter=token_counter,
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

        Looks at the most recent user message for genuine analysis verbs,
        matched on word boundaries against ``_ANALYSIS_INTENT_PATTERN``
        (COR-16 — the old substring scan over a broader set tripped on
        e.g. "fix" in "prefix" and left SOURCE_CODE ~never compressed).
        Both plain-string and block-format user content are scanned (text
        parts concatenated — COR-53). The matched keyword is DEBUG-logged
        so over-breadth stays visible.

        Args:
            messages: Conversation messages.

        Returns:
            True if analysis intent detected.
        """
        # Find most recent user message
        for message in reversed(messages):
            if message.get("role") == "user":
                content = concat_text_parts(message.get("content", ""))
                if content:
                    match = _ANALYSIS_INTENT_PATTERN.search(content.lower())
                    if match:
                        logger.debug(
                            "analysis_intent: keyword %r matched in latest user message",
                            match.group(0),
                        )
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
