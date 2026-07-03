"""Content router for intelligent compression strategy selection.

This module is the FACADE of the router decomposition (§4.1): it owns the
configuration surface (``ContentRouterConfig`` / ``_APPLY_ALLOWED_KWARGS``),
the ``apply()`` message-walk orchestration, the two-tier cache gate
(``_lookup_cached_disposition`` / ``_store_disposition``), and thin delegators
for every public and monkeypatched name. The planes it orchestrates live in
sibling modules and are re-exported here for back-compat:

- ``router_engine``         — ContentCompressionEngine: compress ONE string
                              (strategy determination, pure/mixed paths,
                              empty-output guard, CCR-offload, observer).
- ``router_message_policy`` — the Pass-1 gate chain as ``classify_message()``
                              → ``MessageDisposition`` ADT, plus tool-map /
                              tool-bias / analysis-intent policy functions.
- ``router_blocks``         — ContentBlockWalker: the Anthropic content-block
                              walk (flat + nested tool_result, text blocks).
- ``router_cache``          — the two-tier result cache + CacheDisposition ADT.
- ``router_dispatch``       — per-strategy dispatch + no-savings fallback.
- ``router_policy``         — pure strategy/ratio mappings.
- ``router_split``          — mixed-content detection and sectioning.
- ``router_ccr_mirror``     — CCR re-backing for result-cache hits.
- ``router_debug``          — DEBUG-introspection helpers.
- ``compressor_registry``   — lazy per-compressor factories.

Supported Compressors:
- SmartCrusher: JSON arrays (lossless compaction tier + lossy row selection)
- SearchCompressor: grep/ripgrep results
- LogCompressor: build/test output
- DiffCompressor: git diffs
- TextCrusher: deterministic extractive prose compression (PLAIN_TEXT)
- CodeAwareCompressor: opt-in AST-verified code compression (SOURCE_CODE;
  default OFF — code ships unmangled)
- Large content nothing could shrink is offloaded reversibly via the CCR
  store (identity preview + retrieval marker).

Routing Strategy:
1. Check for mixed content (split and route sections)
2. Detect content type (JSON, code, search, logs, diff, text)
3. Route to appropriate compressor (protection gates first: excluded tools,
   user/system messages, error outputs, recent/analyzed code, CCR pinning)
4. Reassemble and return with routing metadata

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

import logging
import os
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..config import (
    DEFAULT_EXCLUDE_TOOLS,
    CompressRequest,
    ReadLifecycleConfig,
    TransformResult,
    is_tool_excluded,
)
from ..tokenizer import Tokenizer
from .base import CompressionObserver, Transform
from .content_detector import ContentType, DetectionResult
from .content_detector import detect_content_type as _regex_detect_content_type

# Extracted seams (pure moves — §4.1 S1-S6). Re-imported here so that:
#   * existing ``from ...content_router import X`` imports keep resolving,
#   * the package lazy-export in ``transforms/__init__.py`` keeps working,
#   * in-module callers reference these as module globals, and — load-bearing —
#     the ENGINE resolves ``is_mixed_content`` / ``split_into_sections`` /
#     ``_detect_content`` / the debug helpers / ``_word_count`` /
#     ``_looks_like_ccr_output`` through THIS module's globals at call time
#     (router_engine._cr), so the test suite's
#     ``monkeypatch.setattr(content_router_module, "...", ...)`` keeps biting, and
#   * ``content_router_module.time`` patches still target the same ``time``
#     module object the cache uses.
from .router_blocks import ContentBlockWalker
from .router_cache import (
    _RECOMPUTE,
    _SERVE_ORIGINAL,
    CacheDisposition,
    CacheKey,
    CompressionCache,
    Recompute,
    ServeCached,
    ServeOriginal,
)
from .router_ccr_mirror import CcrMirror
from .router_debug import (  # noqa: F401 — module globals: the engine late-binds these here
    _json_shape,
    _log_router_debug,
    _mixed_indicators,
    _router_debug_dumps,
    _section_debug,
)
from .router_dispatch import (
    StrategyDispatcher,  # noqa: F401 — re-exported for backward-compatible imports/tests
)
from .router_engine import (
    _OFFLOAD_MIN_CHARS,  # noqa: F401 — re-exported for backward-compatible imports/tests
    _OFFLOAD_TRIGGER_RATIO,  # noqa: F401 — re-exported for backward-compatible imports/tests
    ContentCompressionEngine,
    RouterCompressionResult,
    RoutingDecision,  # noqa: F401 — re-exported for backward-compatible imports/tests
)
from .router_message_policy import (
    _ANALYSIS_INTENT_KEYWORDS,  # noqa: F401 — re-exported for backward-compatible imports/tests
    _ANALYSIS_INTENT_PATTERN,  # noqa: F401 — re-exported for backward-compatible imports/tests
    _RETRIEVE_HINT_PATTERN,  # noqa: F401 — re-exported for backward-compatible imports/tests
    _TOOL_ROLES,  # noqa: F401 — re-exported for backward-compatible imports/tests
    ALWAYS_EXCLUDE_TOOLS,
    AlreadyCompressed,
    Compressible,
    ContentBlocks,
    Frozen,
    MessageDisposition,  # noqa: F401 — re-exported for backward-compatible imports/tests
    NonString,
    ProtectedMsg,
    Small,
    _is_retrieval_tool,  # noqa: F401 — re-exported for backward-compatible imports/tests
    _is_unstructured_error_output,  # noqa: F401 — re-exported for backward-compatible imports/tests
    _looks_like_ccr_output,  # noqa: F401 — module global: the engine late-binds it here
    build_tool_name_map,
    classify_message,
    detect_analysis_intent,
    get_tool_bias,
)
from .router_policy import (
    CompressionStrategy,
    adaptive_min_ratio,
    content_type_from_strategy,
    strategy_from_detection,
    strategy_from_detection_type,
)
from .router_split import (
    _extract_json_block,  # noqa: F401 — re-exported for backward-compatible imports/tests
    is_mixed_content,  # noqa: F401 — module global: the engine late-binds it here; tests rebind it
    split_into_sections,  # noqa: F401 — module global: the engine late-binds it here; tests rebind it
)

if TYPE_CHECKING:
    # Annotation-only: the runtime import stays lazy inside
    # ``_get_feedback_hints`` — content_router never imports the cache
    # package at module level (same deferred-import rule as the CCR store).
    from ..cache.retrieval_feedback import FeedbackHints

    # Annotation-only compressor types for the lazy `_get_*` delegators
    # (TYPE-3): the compressor modules stay lazily imported inside
    # `CompressorRegistry`; these imports never run at runtime.
    from .code_aware_compressor import CodeAwareCompressor
    from .diff_compressor import DiffCompressor
    from .log_compressor import LogCompressor
    from .search_compressor import SearchCompressor
    from .smart_crusher import SmartCrusher
    from .text_crusher import TextCrusher

logger = logging.getLogger(__name__)


def _word_count(text: str) -> int:
    """Whitespace word count — the compression plane's historical token proxy.

    The default unit for ``compress()`` when no ``token_counter`` is threaded
    in. ``apply()`` passes the request's real ``tokenizer.count_text`` so the
    acceptance gate (``compression_ratio < min_ratio``) compares like units —
    word-ratios systematically overstate savings on compaction outputs (CSV,
    comma-joined) that have few spaces (COR-17).
    """
    return len(text.split())


def _compress_worker_count() -> int:
    """Parse ``FURL_COMPRESS_WORKERS`` (default 4) — the ONE place (§4.1 S6).

    Read at call time on every ``apply()`` so a changed environment is
    honored; an unparsable value warns once per apply and falls back to 4.
    """
    raw_workers = os.environ.get("FURL_COMPRESS_WORKERS", "4")
    try:
        return int(raw_workers)
    except ValueError:
        logger.warning("Invalid FURL_COMPRESS_WORKERS=%r; using default 4", raw_workers)
        return 4


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
class ContentRouterConfig:
    """Configuration for intelligent content routing.

    Attributes:
        enable_smart_crusher: Enable JSON array compression.
        enable_search_compressor: Enable search result compression.
        enable_log_compressor: Enable build/test log compression.
        enable_text_crusher: Enable deterministic prose compression.
        mixed_content_threshold: Min distinct types to consider "mixed".
        min_section_tokens: Minimum tokens for a section to compress.
        fallback_strategy: Strategy when no compressor matches.
        skip_user_messages: Never compress user messages (they're the subject).
        protect_recent_code: Don't compress CODE in the last N messages
            (0 = disabled; overridable per call via the ``protect_recent``
            kwarg).
        protect_analysis_context: Detect "analyze/review" intent, skip compression.
    """

    # Enable/disable specific compressors
    enable_smart_crusher: bool = True
    enable_search_compressor: bool = True
    enable_log_compressor: bool = True
    # TextCrusher (Engine P2-11): deterministic extractive prose
    # compression for PLAIN_TEXT. Size floors live in the crusher
    # itself (600 chars / 15 segments → passthrough); every crush is
    # CCR-backed with a retrieval marker, and the compressor refuses
    # unmarked drops (store-failure vetoes to passthrough). Gated off
    # by `lossless_only` like the other line-dropping compressors.
    enable_text_crusher: bool = True
    # CodeAwareCompressor (Engine P2-12): OPT-IN AST-verified code
    # compression for SOURCE_CODE. Default OFF — code keeps shipping
    # unmangled (PASSTHROUGH), byte-identical to the pre-P2-12 engine.
    # When True, detected source code routes to the tree-sitter
    # compressor (optional `furl-ctx[code]` extra; missing dep →
    # passthrough + one WARN). Every ship is syntax-verified and
    # CCR-backed (full original persisted under the marker hash;
    # store failure vetoes to passthrough). The analysis-intent /
    # protect_recent_code protections run BEFORE routing and still
    # win; `lossless_only` gates the dispatch arm off.
    enable_code_aware: bool = False
    # Retrieval-feedback loop (Engine P2-13): OPT-IN adaptive routing driven
    # by the store's own retrieval bookkeeping. Default OFF — the feedback
    # aggregator is never consulted and routing is byte-identical to the
    # pre-P2-13 engine (pinned by test_retrieval_feedback_router.py). When
    # True, the router consults ``furl_ctx.cache.retrieval_feedback`` at
    # routing time: a content shape (tool name + detected content type) the
    # model recently retrieved from the CCR store gets a keep-budget bias
    # multiplier, and under sustained retrieval pressure a full compression
    # skip (``router:feedback:skip``). Signals are LOCAL-ONLY — an in-process
    # aggregator fed by ``CompressionStore.retrieve``/``search`` real hits
    # (COR-37-honest; engine-internal verification reads opt out). No
    # telemetry, no disk ledger.
    enable_retrieval_feedback: bool = False

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
#      or for sibling transforms — e.g. ``model_limit`` / ``request_id``
#      (documented in ``TransformPipeline.apply``),
#      ``previous_prefix_hash`` (CacheAligner's documented turn-to-turn
#      tracking kwarg, API-4), and ``model`` / ``messages`` / ``tokenizer``
#      (positionals). These are valid, just not consumed here.
#      (``record_metrics`` is NOT accepted: the pipeline pops it before the
#      broadcast, so it can never legitimately arrive here. ``output_buffer``
#      and ``tool_profiles`` were removed with their dead docstring bullets —
#      per-tool profiles are configured via ``ContentRouterConfig
#      .tool_profiles``; passing either kwarg now fails loudly instead of
#      being silently ignored, API-16.)
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
        "previous_prefix_hash",
        "request_id",
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
        observer: CompressionObserver | None = None,
    ):
        """Initialize content router.

        Args:
            config: Router configuration. Uses defaults if None.
            observer: Optional `CompressionObserver` (structural protocol,
                see `transforms.base`) called once per routing decision
                after `compress()` finishes. Must expose
                `record_compression(...)`; `record_router_route_counts(...)`
                is tolerated missing at runtime for older duck-typed
                observers. Exceptions it raises are swallowed so a buggy
                observer can't break compression. `None` disables
                observation.
        """
        self.config = config or ContentRouterConfig()
        self._observer = observer

        # Content-level compression engine (§4.1 S5): owns the lifetime-stable
        # machinery — the lazy ``CompressorRegistry`` and the
        # ``StrategyDispatcher`` — and the body of ``compress()``. It holds no
        # router reference between calls: every engine method takes
        # ``hooks=self`` per call, so instance and class-level monkeypatches
        # on this class keep biting, and the observer reference stays HERE
        # (``self._observer`` is runtime-reassignable router state the engine
        # reads through the hooks).
        self._engine = ContentCompressionEngine(self.config)
        # Back-compat aliases: the ``_get_*`` delegators resolve compressors
        # through ``self._registry`` exactly as before the extraction (the
        # registry/dispatcher OBJECTS are the engine's).
        self._registry = self._engine._registry
        self._dispatcher = self._engine._dispatcher
        # CCR-backing seam for the result-cache HIT path. Holds no router
        # reference: the SmartCrusher getter is passed per-call by the
        # ``_ensure_ccr_backed`` delegator (resolving ``self._get_smart_crusher``
        # fresh), so monkeypatching that getter still bites. Only the
        # lifetime-stable ``logger`` rides the constructor.
        self._ccr_mirror = CcrMirror(logger=logger)
        # Anthropic content-block walker. Holds no router reference: the cache
        # gate, ``compress``, and the per-tool policy callables are passed
        # per-call by the ``_process_content_blocks`` delegator, so
        # monkeypatching ``router.compress`` (as six suites do) still bites.
        # Only the lifetime-stable ``config`` rides the constructor.
        self._block_walker = ContentBlockWalker(self.config)

        self._cache = CompressionCache()

    def _timed_compress(
        self,
        content: str,
        context: str,
        bias: float,
        token_counter: Callable[[str], int] | None = None,
        detection: DetectionResult | None = None,
    ) -> tuple[RouterCompressionResult, float]:
        """Compress with wall-clock timing.  Used by parallel executor."""
        t0 = time.perf_counter()
        result = self.compress(
            content,
            context=context,
            bias=bias,
            token_counter=token_counter,
            detection=detection,
        )
        return result, (time.perf_counter() - t0) * 1000

    def _compress_pending(
        self,
        pending_tasks: list[tuple[int, str, str, float, CacheKey, DetectionResult | None]],
        messages: list[dict[str, Any]],
        result_slots: list[dict[str, Any] | None],
        *,
        min_ratio: float,
        token_counter: Callable[[str], int],
        transforms_applied: list[str],
        compressed_details: list[str],
        route_counts: Counter[str],
        compressor_timing: dict[str, float],
    ) -> None:
        """Pass 2/3 of ``apply()``: compress every cache-miss message and merge.

        Pass 2 runs all pending ``compress()`` calls concurrently in a thread
        pool (each call is independent; per-task inputs are passed by
        argument). ``FURL_COMPRESS_WORKERS`` is parsed here — the one place —
        at CALL time (the env var is re-read per apply, never captured at
        import). A single task, or a worker count <= 1, compresses inline.

        Pass 3 merges results back into ``result_slots`` in message order
        (``zip`` over the submit order — thread completion order never
        reorders output), updates the two-tier cache through
        ``_store_disposition``, and books the flat string-path transform
        format for accepted compressions.
        """
        max_workers = min(len(pending_tasks), _compress_worker_count())
        t_parallel_start = time.perf_counter()

        if max_workers <= 1 or len(pending_tasks) == 1:
            # Single task or parallelism disabled — compress inline
            task_results = []
            for _, task_content, task_ctx, task_bias, _, task_detection in pending_tasks:
                t0 = time.perf_counter()
                r = self.compress(
                    task_content,
                    context=task_ctx,
                    bias=task_bias,
                    token_counter=token_counter,
                    detection=task_detection,
                )
                task_results.append((r, (time.perf_counter() - t0) * 1000))
        else:
            # Parallel compression via thread pool. Each compress() call
            # is independent; per-task inputs are passed by argument.
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = []
                for _, task_content, task_ctx, task_bias, _, task_detection in pending_tasks:
                    futures.append(
                        executor.submit(
                            self._timed_compress,
                            task_content,
                            task_ctx,
                            task_bias,
                            token_counter,
                            task_detection,
                        )
                    )
                task_results = [f.result() for f in futures]

        parallel_ms = (time.perf_counter() - t_parallel_start) * 1000
        compressor_timing["parallel_compress_total"] = parallel_ms

        # --- Pass 3: Merge results back (sequential, updates caches) ---
        for (slot_idx, _, _, _, content_key, _), (result, compress_ms) in zip(
            pending_tasks, task_results
        ):
            message = messages[slot_idx]
            strategy_key = f"compressor:{result.strategy_used.value}"
            compressor_timing[strategy_key] = compressor_timing.get(strategy_key, 0.0) + compress_ms

            if self._store_disposition(content_key, result, min_ratio, route_counts):
                # Compressed — stored in the result cache; serve it with
                # the flat string-path transform format.
                result_slots[slot_idx] = {**message, "content": result.compressed}
                transforms_applied.append(
                    f"router:{result.strategy_used.value}:{result.compression_ratio:.2f}"
                )
                compressed_details.append(
                    f"{result.strategy_used.value}:{result.compression_ratio:.2f}"
                )
            else:
                # Didn't compress — key is in the skip set; serve original.
                result_slots[slot_idx] = message

    def compress(
        self,
        content: str,
        context: str = "",
        question: str | None = None,
        bias: float = 1.0,
        *,
        token_counter: Callable[[str], int] | None = None,
        detection: DetectionResult | None = None,
    ) -> RouterCompressionResult:
        """Compress content using optimal strategy based on content detection.

        Thin delegator to :meth:`ContentCompressionEngine.compress` — the
        engine calls back through ``hooks=self`` for strategy selection and
        the pure/mixed paths, so monkeypatching ``router._determine_strategy``
        / ``_compress_mixed`` / ``_compress_pure`` (instance or class level)
        still takes effect, and resolves ``is_mixed_content`` /
        ``split_into_sections`` / ``_detect_content`` through THIS module's
        globals at call time.

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
            detection: Optional PRECOMPUTED detection of exactly these
                content bytes (PERF-2c). ``apply()`` threads the
                classify-time result in so the engine never pays the Rust
                detect round-trip twice for one message; ``None`` (direct
                callers) keeps the historical strategy path, including the
                ``_determine_strategy`` monkeypatch seam.

        Returns:
            RouterCompressionResult with compressed content and routing metadata.
        """
        return self._engine.compress(
            content,
            context,
            question,
            bias,
            token_counter=token_counter,
            detection=detection,
            hooks=self,
        )

    def _determine_strategy(self, content: str) -> CompressionStrategy:
        """Determine the compression strategy from content analysis.

        Thin delegator to :meth:`ContentCompressionEngine._determine_strategy`.
        """
        return self._engine._determine_strategy(content, hooks=self)

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

        Thin delegator to :meth:`ContentCompressionEngine._compress_mixed`
        (COR-30 byte-faithful passthrough semantics documented there).
        """
        return self._engine._compress_mixed(
            content,
            context,
            question,
            bias,
            token_counter=token_counter,
            hooks=self,
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

        Thin delegator to :meth:`ContentCompressionEngine._compress_pure`.
        """
        return self._engine._compress_pure(
            content,
            strategy,
            context,
            question,
            bias,
            token_counter=token_counter,
            hooks=self,
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

        Thin delegator to :meth:`ContentCompressionEngine._apply_strategy_to_content`
        → :meth:`StrategyDispatcher.apply`. The compressor getters are resolved
        fresh from THIS instance on every call (via ``hooks=self``), so
        monkeypatching those router methods still takes effect (a
        construction-time capture in the dispatcher would have been stale).

        Returns:
            Tuple of (compressed_content, compressed_token_count,
            strategy_chain). The chain lists every strategy attempted
            in order — first the requested one, then any fallbacks.
            Single-entry chain means a direct hit; multi-entry means
            the fallback chain fired (e.g. ``[smart_crusher, log]``).
            Log readers use this to see *how* we got to the final
            compressor without parsing decision_reason strings.
        """
        return self._engine._apply_strategy_to_content(
            content,
            strategy,
            context,
            language,
            question,
            bias,
            token_counter=token_counter,
            hooks=self,
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

    def _get_smart_crusher(self) -> SmartCrusher | None:
        """Get SmartCrusher (lazy load) with CCR config.

        Thin delegator to :meth:`CompressorRegistry.get_smart_crusher`.
        """
        return self._registry.get_smart_crusher()

    def _ensure_ccr_backed(self, cached_compressed: str, context: str) -> bool:
        """Ensure every ``<<ccr:HASH>>`` pointer in *cached_compressed* resolves
        in the Python ``compression_store`` (the store the MCP ``furl_retrieve`` tool reads).

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

    def _get_search_compressor(self) -> SearchCompressor | None:
        """Get SearchCompressor (lazy load).

        Thin delegator to :meth:`CompressorRegistry.get_search_compressor`.
        """
        return self._registry.get_search_compressor()

    def _get_log_compressor(self) -> LogCompressor | None:
        """Get LogCompressor (lazy load).

        Thin delegator to :meth:`CompressorRegistry.get_log_compressor`.
        """
        return self._registry.get_log_compressor()

    def _get_diff_compressor(self) -> DiffCompressor:
        """Get DiffCompressor (lazy load).

        Thin delegator to :meth:`CompressorRegistry.get_diff_compressor`.
        """
        return self._registry.get_diff_compressor()

    def _get_text_crusher(self) -> TextCrusher:
        """Get TextCrusher (lazy load).

        Thin delegator to :meth:`CompressorRegistry.get_text_crusher`.
        """
        return self._registry.get_text_crusher()

    def _get_code_aware_compressor(self) -> CodeAwareCompressor:
        """Get CodeAwareCompressor (lazy load).

        Thin delegator to :meth:`CompressorRegistry.get_code_aware_compressor`.
        """
        return self._registry.get_code_aware_compressor()

    # Transform interface

    def _build_tool_name_map(self, messages: list[dict[str, Any]]) -> dict[str, str]:
        """Build mapping from tool_call_id to tool_name.

        Thin delegator to the pure :func:`router_message_policy.build_tool_name_map`.
        """
        return build_tool_name_map(messages)

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

        transforms_applied: list[str] = []
        warnings: list[str] = []
        compressor_timing: dict[str, float] = {}  # strategy → cumulative ms

        # Routing reason counters for summary logging (TYPE-4: a
        # ``Counter[str]`` makes every ``+=`` total — conditionally-booked
        # keys need no ``setdefault`` seeding). The eight hot lanes stay
        # PRE-SEEDED at zero: the observer receives this object (a dict
        # subclass) and the whole-dict route_counts pins depend on the zero
        # keys being present exactly as before.
        route_counts: Counter[str] = Counter(
            {
                "excluded_tool": 0,
                "user_msg": 0,
                "small": 0,
                "recent_code": 0,
                "analysis_ctx": 0,
                "ratio_too_high": 0,
                "non_string": 0,
                "content_blocks": 0,
            }
        )
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

        # Tasks: (slot_index, content, context, bias, content_key, detection)
        # — ``detection`` is the classify-time DetectionResult when a Pass-1
        # gate already paid for it, else None (PERF-2c).
        _PendingTask = tuple[int, str, str, float, CacheKey, DetectionResult | None]
        pending_tasks: list[_PendingTask] = []

        # Pass 1 dispatches on the MessageDisposition ADT: WHAT happens to a
        # message is decided by the pure gate chain in
        # ``router_message_policy.classify_message`` (order preserved verbatim
        # — it is behavior); HOW it happens (slot assignment, transform
        # strings, counters, the cache gate, Pass-2 deferral) stays here.
        # The injected callables are resolved fresh on every call so the test
        # suite's monkeypatches — ``content_router_module._detect_content``,
        # ``router._get_tool_bias`` — keep biting.
        for i, message in enumerate(messages):
            content = message.get("content", "")
            disposition = classify_message(
                message,
                index=i,
                num_messages=num_messages,
                frozen_message_count=frozen_message_count,
                tool_name_map=tool_name_map,
                excluded_tool_ids=excluded_tool_ids,
                exclude_tools=exclude_tools,
                read_protection_window=read_protection_window,
                skip_user=skip_user,
                skip_system=skip_system,
                min_tokens=min_tokens,
                protect_recent=protect_recent,
                protect_analysis=protect_analysis,
                analysis_intent=analysis_intent,
                hook_biases=hook_biases,
                config=self.config,
                count_text=tokenizer.count_text,
                detect_content=_detect_content,
                get_tool_bias=self._get_tool_bias,
                get_feedback_hints=self._get_feedback_hints,
                result_cache_key=_result_cache_key,
            )
            match disposition:
                case Frozen():
                    # In the provider's prefix cache: byte-identical, no
                    # bookkeeping of any kind.
                    result_slots[i] = message
                case ProtectedMsg(transform=transform, counter=counter):
                    result_slots[i] = message
                    transforms_applied.append(transform)
                    route_counts[counter] += 1
                case ContentBlocks():
                    # Anthropic-format block list — walk the blocks.
                    result_slots[i] = self._process_content_blocks(
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
                        messages_from_end=num_messages - i,
                        compressor_timing=compressor_timing,
                        min_chars=min_chars_for_block_compression,
                        skip_user=skip_user,
                        skip_system=skip_system,
                        compress_assistant_text_blocks=compress_assistant_text_blocks,
                        token_counter=tokenizer.count_text,
                    )
                    route_counts["content_blocks"] += 1
                case NonString():
                    result_slots[i] = message
                    route_counts["non_string"] += 1
                case Small():
                    result_slots[i] = message
                    route_counts["small"] += 1
                case AlreadyCompressed():
                    result_slots[i] = message
                    route_counts["already_compressed"] += 1
                case Compressible(bias=msg_bias, content_key=content_key, detection=detection):
                    # Two-tier compression cache. The lookup DECISION — Tier-1
                    # skip, Tier-2 ratio-gate, CCR-backing check, plus every
                    # cache mutation and routing-counter bump — is shared with
                    # the content-block path in _lookup_cached_disposition.
                    # Only what genuinely differs stays here: this path formats
                    # a flat ``router:{strategy}:{ratio}`` transform and DEFERS
                    # recompute to the batched ThreadPoolExecutor pass below
                    # (pending_tasks → Pass 2/3), whereas
                    # _compress_content_block threads a
                    # ``router:{label}:{strategy}`` format and recompresses
                    # inline. Outcomes pinned in
                    # test_content_router_cache_lookup_paths.py +
                    # test_result_cache_ccr_divergence.py. The key carries the
                    # per-request bias and a length guard — see
                    # _result_cache_key (COR-18).
                    match self._lookup_cached_disposition(
                        content_key, context, min_ratio, route_counts
                    ):
                        case ServeOriginal():
                            result_slots[i] = message
                        case ServeCached(compressed=served, strategy=strategy, ratio=ratio):
                            result_slots[i] = {**message, "content": served}
                            transforms_applied.append(f"router:{strategy}:{ratio:.2f}")
                            compressed_details.append(f"{strategy}:{ratio:.2f}")
                        case Recompute():
                            # Defer to the parallel compression pass (Pass 2/3).
                            pending_tasks.append(
                                (i, content, context, msg_bias, content_key, detection)
                            )
                        case other:
                            raise RuntimeError(
                                f"_lookup_cached_disposition returned unexpected "
                                f"CacheDisposition {other!r}"
                            )
                case other:
                    raise RuntimeError(
                        f"classify_message returned unexpected MessageDisposition {other!r}"
                    )

        # --- Pass 2/3: parallel compression of all cache-miss messages,
        # merged back in message order (extracted executor — §4.1 S6).
        if pending_tasks:
            self._compress_pending(
                pending_tasks,
                messages,
                result_slots,
                min_ratio=min_ratio,
                token_counter=tokenizer.count_text,
                transforms_applied=transforms_applied,
                compressed_details=compressed_details,
                route_counts=route_counts,
                compressor_timing=compressor_timing,
            )

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
        if route_counts.get("feedback_skip"):
            parts.append(f"{route_counts['feedback_skip']} protected (retrieval feedback)")
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

        Thin delegator to the pure :func:`router_message_policy.get_tool_bias`
        (user-configured profiles first, then DEFAULT_TOOL_PROFILES, else 1.0).
        """
        return get_tool_bias(self.config.tool_profiles, tool_name)

    def _get_feedback_hints(
        self,
        tool_name: str,
        content: str,
        content_type_tag: str | None = None,
    ) -> FeedbackHints:
        """Look up retrieval-feedback hints for one compression unit.

        The retrieval-side sibling of ``_get_tool_bias`` (Engine P2-13): where
        tool bias is static per-tool configuration, these hints are the
        adaptive signal from the model's own CCR retrievals, keyed by
        (tool name, content type). Default-NEUTRAL — with
        ``enable_retrieval_feedback`` off (the default) the aggregator is
        never consulted and this returns the shared neutral hints, so routing
        stays byte-identical.

        Args:
            tool_name: Tool that produced the content ("" when unknown).
            content: The compression unit; detected only when
                *content_type_tag* was not precomputed by the caller.
            content_type_tag: ``ContentType.value`` tag when the caller
                already ran detection (the string path did — don't pay the
                detect twice).
        """
        from ..cache.retrieval_feedback import (
            NEUTRAL_HINTS,
            get_retrieval_feedback,
            routing_shape_key,
        )

        if not self.config.enable_retrieval_feedback:
            return NEUTRAL_HINTS
        if content_type_tag is None:
            content_type_tag = _detect_content(content).content_type.value
        return get_retrieval_feedback().get_hints(routing_shape_key(tool_name, content_type_tag))

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

    def _store_disposition(
        self,
        content_key: CacheKey,
        result: RouterCompressionResult,
        min_ratio: float,
        route_counts: dict[str, int] | None,
    ) -> bool:
        """Store one freshly-computed compression result in the two-tier cache.

        The store-half twin of :meth:`_lookup_cached_disposition` — the single
        home of the accept/reject cache mutation that the string path (Pass-3
        merge in ``apply``) and the content-block path
        (``_compress_content_block``) both ran as duplicated copies:

          * ACCEPT (``compression_ratio`` strictly below ``min_ratio``) → the
            compressed bytes enter the Tier-2 result cache; returns ``True``.
          * REJECT → the key enters the Tier-1 skip set, ``ratio_too_high`` is
            bumped; returns ``False``.

        Exactly like the lookup seam, each caller owns the HOW: the serve
        mechanics and the transform-string formats (flat
        ``router:{strategy}:{ratio}`` vs label-threaded
        ``router:{label}:{strategy}``) genuinely differ and stay in the
        callers. ``route_counts`` is ``None`` only when the block-path caller
        opts out of routing summaries; the bump is then skipped. The key
        already carries the per-request bias and a length guard (COR-18 —
        see ``_result_cache_key``).
        """
        if result.compression_ratio < min_ratio:
            self._cache.put(
                content_key,
                result.compressed,
                result.compression_ratio,
                result.strategy_used.value,
            )
            return True
        self._cache.mark_skip(content_key)
        if route_counts is not None:
            route_counts["ratio_too_high"] = route_counts.get("ratio_too_high", 0) + 1
        return False

    def _process_content_blocks(
        self,
        message: dict[str, Any],
        content_blocks: list[Any],
        context: str,
        transforms_applied: list[str],
        excluded_tool_ids: set[str],
        *,
        min_ratio: float,
        read_protection_window: int,
        messages_from_end: int,
        min_chars: int,
        skip_user: bool,
        skip_system: bool,
        compress_assistant_text_blocks: bool,
        tool_name_map: dict[str, str] | None = None,
        route_counts: dict[str, int] | None = None,
        compressed_details: list[str] | None = None,
        compressor_timing: dict[str, float] | None = None,
        token_counter: Callable[[str], int] | None = None,
    ) -> dict[str, Any]:
        """Process content blocks (Anthropic format) for compression.

        Thin delegator to :meth:`ContentBlockWalker.process_content_blocks`.
        The cache gate (``_lookup_cached_disposition`` / ``_store_disposition``),
        ``compress``, and the per-tool policy callables are resolved fresh here
        on every call and passed in, so monkeypatching those router methods —
        ``router.compress`` above all — still takes effect (a construction-time
        capture in the walker would have been stale).

        The policy knobs (``min_ratio`` … ``compress_assistant_text_blocks``)
        are REQUIRED keywords: their old defaults silently duplicated
        ``ContentRouterConfig`` field defaults and drifted when the config
        changed (SIMP-9) — ``apply()`` always passed every one explicitly.
        """
        return self._block_walker.process_content_blocks(
            message,
            content_blocks,
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
            min_chars=min_chars,
            skip_user=skip_user,
            skip_system=skip_system,
            compress_assistant_text_blocks=compress_assistant_text_blocks,
            token_counter=token_counter,
            lookup_disposition=self._lookup_cached_disposition,
            store_disposition=self._store_disposition,
            compress_fn=self.compress,
            get_tool_bias=self._get_tool_bias,
            get_feedback_hints=self._get_feedback_hints,
            result_cache_key=_result_cache_key,
        )

    def _detect_analysis_intent(self, messages: list[dict[str, Any]]) -> bool:
        """Detect if user wants to analyze/review code (COR-16/COR-53).

        Thin delegator to the pure
        :func:`router_message_policy.detect_analysis_intent`; the router's
        logger rides along so the matched-keyword DEBUG record keeps its
        historical logger name.
        """
        return detect_analysis_intent(messages, logger=logger)

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
