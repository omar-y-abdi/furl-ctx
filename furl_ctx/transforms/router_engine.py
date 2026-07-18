"""Content-level compression engine for the content router.

Extracted from ``content_router.py`` (§4.1 S5). :class:`ContentCompressionEngine`
owns compressing ONE string: strategy determination, the pure/mixed paths, the
per-strategy dispatch (via the owned :class:`StrategyDispatcher` +
:class:`CompressorRegistry`), the empty-output guard, the reversible CCR-offload
fallback, and the observer plumbing (the TOIN successor — one
``record_compression`` per routing decision). That class has zero message/dict
knowledge and is stateless per call. The engine's result types
(:class:`RoutingDecision` / :class:`RouterCompressionResult`) live here and are
re-exported by ``content_router`` for back-compat.

This module ALSO hosts :func:`run_router_passes` — the message-level ``apply()``
orchestration (the Pass-1 ``classify_message`` walk, the parallel Pass-2/3
executor call, and the result-assembly/summary tail). It is a sibling FREE
FUNCTION, deliberately NOT a method of :class:`ContentCompressionEngine`: it
takes the router facade as ``hooks`` and mutates message/dict/counter state, so
folding it into the engine would break that class's zero-message-knowledge
contract. Like the engine, it late-binds ``content_router`` module globals
(``_detect_content`` / ``_result_cache_key`` / ``_APPLY_ALLOWED_KWARGS`` /
``logger``) through :func:`_cr` so the test suite's module-level monkeypatches
keep biting.

Two injection planes keep every existing monkeypatch biting:

* **hooks** (per call) — the router facade passes ITSELF. Every call that used
  to be a ``self.<method>`` lookup on the router still resolves through the
  facade instance (``hooks._determine_strategy`` / ``_compress_mixed`` /
  ``_compress_pure`` / ``_apply_strategy_to_content`` / the ``_get_*``
  compressor getters / ``_observer``), so instance monkeypatches AND
  class-level ``patch.object(ContentRouter, ...)`` keep working. The facade
  delegators call back into the engine, forming facade → engine → facade →
  engine chains with no state held between hops.

* **module globals** (late-bound, at call time) — ``is_mixed_content``,
  ``split_into_sections``, ``_detect_content``, ``time``, the debug helpers,
  ``_word_count``, ``_looks_like_ccr_output``, and ``logger`` are resolved
  through the ``content_router`` MODULE at each use via :func:`_cr`, never
  imported here at module level. The test suite rebinds these as
  ``content_router`` module globals
  (``monkeypatch.setattr(content_router_module, "is_mixed_content", ...)``);
  a module-level ``from .content_router import is_mixed_content`` here would
  leave those patches silently non-biting (the §4.1 design hole, called out
  in the plan). The function-local import also breaks the load-time cycle:
  ``content_router`` imports this module at top level.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from ..config import (
    DEFAULT_EXCLUDE_TOOLS,
    CompressRequest,
    TransformResult,
    is_tool_excluded,
)
from ..tokenizer import Tokenizer
from .compressor_registry import CompressorRegistry
from .content_detector import ContentType, DetectionResult
from .net_mutation_gain import MutationContext, net_mutation_gain
from .router_cache import (
    CacheKey,
    Recompute,
    ServeCached,
    ServeOriginal,
)
from .router_dispatch import StrategyDispatcher
from .router_message_policy import (
    ALWAYS_EXCLUDE_TOOLS,
    AlreadyCompressed,
    Compressible,
    ContentBlocks,
    Frozen,
    NonString,
    ProtectedMsg,
    Small,
    classify_message,
)
from .router_policy import CompressionStrategy, noop_transform

if TYPE_CHECKING:
    from types import ModuleType

    from .base import CompressionObserver
    from .code_aware_compressor import CodeAwareCompressor
    from .content_router import ContentRouter, ContentRouterConfig
    from .diff_compressor import DiffCompressor
    from .log_compressor import LogCompressor
    from .search_compressor import SearchCompressor
    from .smart_crusher import SmartCrusher
    from .text_crusher import TextCrusher

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
# Error-line preservation for the plain-text head/tail preview (Bug-4 / Med-10):
# a head+tail preview drops the MIDDLE, so an ERROR/Traceback buried between the
# first and last lines vanished from the model-visible view (only recoverable if
# the agent already knew to retrieve). Surface up to _OFFLOAD_ERROR_LINES_MAX
# severity-matched lines FROM THE OMITTED MIDDLE, each capped at
# _OFFLOAD_ERROR_LINE_MAX_CHARS so one giant line can't bloat the preview. The
# pattern is a flat, anchored alternation of severity WORDS — no nested
# quantifiers, so it is linear (ReDoS-safe) on any line.
_OFFLOAD_ERROR_LINES_MAX = 15
_OFFLOAD_ERROR_LINE_MAX_CHARS = 240
_OFFLOAD_SEVERITY_RE = re.compile(
    r"\b(?:ERROR|FATAL|CRITICAL|SEVERE|PANIC|EXCEPTION|TRACEBACK)\b",
    re.IGNORECASE,
)
# Object-with-dominant-array preview (e.g. a Chrome trace: {"metadata": {...},
# "traceEvents": [...]}). Sample the leading/trailing elements of the inner
# array so the preview reflects the events, not the object's header boilerplate.
_OFFLOAD_PREVIEW_ARRAY_HEAD = 8
_OFFLOAD_PREVIEW_ARRAY_TAIL = 2
# Signal-aware summary budgets for the two JSON-array-of-dicts offload paths.
# Two sequential O(n) passes over the rows yield a compact ``_ccr_summary`` that
# answers aggregate (per-field value histograms) and anomaly (one concrete
# example row per notable value) questions INLINE, so the agent need not
# retrieve the full content. All bounds are hard output caps — the summary stays
# a few KB regardless of row count.
#
# A field is CATEGORICAL (gets a histogram + is a candidate for the example
# field) when its scalar values are low-cardinality: at most
# _SUMMARY_MAX_CATEGORICAL_DISTINCT distinct values, OR a distinct/row ratio
# below _SUMMARY_CATEGORICAL_RATIO. An all-unique field (e.g. a monotonic
# timestamp) fails both and is excluded, so it neither bloats value_counts nor
# is picked as the example ("type") field.
#
# _SUMMARY_TOP_VALUES bounds BOTH the per-field histogram and the example rows
# (one example per top value of the chosen field).
_SUMMARY_MAX_CATEGORICAL_DISTINCT = 50
_SUMMARY_CATEGORICAL_RATIO = 0.02
_SUMMARY_TOP_VALUES = 20
_SUMMARY_SAMPLE_ROWS = 6

# Sibling-field preview bounds (review F2). A dominant-array object's OTHER keys
# (everything besides the array) keep their VALUES, not just their names — a
# crash report's ``exception``/``termination`` blocks carry the single most
# important fact (the exception type/signal), and dropping them to bare key
# names made that fact invisible in the compressed view. Scalars pass through
# (tiny); small nested objects recurse to _SIBLING_MAX_DEPTH so their scalar
# leaves survive; a long list (the kind that SHOULD be offloaded) or a deeper
# structure is elided to a ``[… in CCR]`` note. The whole sibling blob is hard-
# capped at _SIBLING_FIELDS_MAX_CHARS so the preview stays a few KB.
_SIBLING_MAX_DEPTH = 4
_SIBLING_MAX_KEYS = 40
_SIBLING_MAX_LIST = 10
_SIBLING_FIELDS_MAX_CHARS = 4000
# Bytes held back from the char budget while accumulating (review RF4) so the
# trailing ``_more_keys`` note fits under _SIBLING_FIELDS_MAX_CHARS even in the
# worst case (a large omitted-count integer). Comfortably above the note's
# longest serialized form.
_SIBLING_MORE_KEYS_RESERVE = 64

# Slice guidance carried inside the ``_ccr_summary`` preview: tells the agent how
# to fetch a NARROW slice of the offloaded rows instead of the whole array back
# (the ``furl_retrieve`` row-select filters). Domain-agnostic on purpose — it
# names no field or value, pointing the agent at the fields it can already see in
# this same summary (``schema``/``value_counts`` for a category, ``ranges`` for a
# numeric window). The hash is NOT known here (it is minted later in
# ``_ccr_offload``), so the hint references "the hash in the marker below" rather
# than threading a value that does not yet exist.
_CCR_SUMMARY_RETRIEVE_HINT = (
    "To drill into these rows without pulling the whole array back, call "
    "furl_retrieve with the hash in the marker below plus a row-select: "
    "select_field=<a categorical field from 'schema'/'value_counts'>, "
    "select_equals=<one of its values> for just those rows; OR "
    "select_field=<a numeric field from 'ranges'>, select_min=…, select_max=… "
    "for a range window; add fields=[…] to project only some columns. Omit all "
    "of these to retrieve the full original."
)

# Byte ceiling above which a content block is offloaded immediately instead of
# run through the exact mixed/crush path — that path is super-linear (a real
# 33 MB Chrome trace took ~68 s), while CCR offload is O(n) and reversible.
_HUGE_CONTENT_BYTES_DEFAULT = 8 * 1024 * 1024


def _huge_content_bytes() -> int:
    """The size ceiling in bytes; override with ``FURL_MAX_COMPRESS_BYTES``."""
    import os

    raw = os.environ.get("FURL_MAX_COMPRESS_BYTES")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _HUGE_CONTENT_BYTES_DEFAULT


def _cr() -> ModuleType:
    """The ``content_router`` module, resolved AT CALL TIME.

    Late binding is the point (see module docstring): monkeypatches on
    ``content_router`` module globals must keep biting after the engine
    extraction. Function-local import — by the time any engine method runs,
    ``content_router`` is fully imported.
    """
    from . import content_router

    return content_router


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


class RouterHooks(Protocol):
    """The facade surface the engine calls back through (TYPE-3).

    The router passes ITSELF per call. Routing every former ``self.<method>``
    lookup through this protocol keeps instance and class-level monkeypatches
    on ``ContentRouter`` biting — the facade delegators re-enter the engine.
    """

    _observer: CompressionObserver | None

    def _determine_strategy(self, content: str) -> CompressionStrategy: ...

    def _strategy_from_detection(self, detection: Any) -> CompressionStrategy: ...

    def _strategy_from_detection_type(self, content_type: ContentType) -> CompressionStrategy: ...

    def _content_type_from_strategy(self, strategy: CompressionStrategy) -> ContentType: ...

    def _compress_mixed(
        self,
        content: str,
        context: str,
        question: str | None = ...,
        bias: float = ...,
        *,
        token_counter: Callable[[str], int] | None = ...,
    ) -> RouterCompressionResult: ...

    def _compress_pure(
        self,
        content: str,
        strategy: CompressionStrategy,
        context: str,
        question: str | None = ...,
        bias: float = ...,
        *,
        token_counter: Callable[[str], int] | None = ...,
    ) -> RouterCompressionResult: ...

    def _apply_strategy_to_content(
        self,
        content: str,
        strategy: CompressionStrategy,
        context: str,
        language: str | None = ...,
        question: str | None = ...,
        bias: float = ...,
        *,
        token_counter: Callable[[str], int] | None = ...,
    ) -> tuple[str, int, list[str]]: ...

    def _get_smart_crusher(self) -> SmartCrusher | None: ...

    def _get_search_compressor(self) -> SearchCompressor | None: ...

    def _get_log_compressor(self) -> LogCompressor | None: ...

    def _get_diff_compressor(self) -> DiffCompressor | None: ...

    def _get_text_crusher(self) -> TextCrusher | None: ...

    def _get_code_aware_compressor(self) -> CodeAwareCompressor | None: ...


class ContentCompressionEngine:
    """Compresses one string through the optimal strategy chain.

    Owns the lifetime-stable compression machinery — ``CompressorRegistry``
    and ``StrategyDispatcher`` — constructed once per router. Everything
    per-call (the hooks facade, token counters, bias) is passed in; the
    engine keeps no request state.
    """

    def __init__(self, config: ContentRouterConfig) -> None:
        self.config = config

        # Lazy-loaded compressors.
        #
        # The SELF-CONTAINED factories (SmartCrusher, Search, Log, Diff,
        # TextCrusher, CodeAware) read only ``config`` and cache their
        # instance — they live in ``CompressorRegistry``. The router's
        # ``_get_*`` delegators resolve through it.
        self._registry = CompressorRegistry(config)
        # Per-strategy dispatch + no-savings fallback chain. Holds no router
        # reference: the compressor getters are passed per-call by the
        # ``_apply_strategy_to_content`` delegator, so monkeypatching those
        # router methods still takes effect. Only the lifetime-stable deps
        # (config + the module-level debug helpers/logger) ride the
        # constructor — resolved once here, exactly when the router used to
        # resolve them in its own ``__init__``.
        cr = _cr()
        self._dispatcher = StrategyDispatcher(
            config,
            logger=cr.logger,
            log_router_debug=cr._log_router_debug,
            json_shape=cr._json_shape,
        )

    def compress(
        self,
        content: str,
        context: str = "",
        question: str | None = None,
        bias: float = 1.0,
        *,
        token_counter: Callable[[str], int] | None = None,
        detection: DetectionResult | None = None,
        hooks: RouterHooks,
    ) -> RouterCompressionResult:
        """Compress content using optimal strategy based on content detection.

        The body of ``ContentRouter.compress`` (a pure move); see the facade
        docstring for the public contract. ``hooks`` is the router facade —
        strategy selection and the pure/mixed paths resolve through it.

        ``detection`` (PERF-2c) is an optional PRECOMPUTED detection of
        exactly these content bytes — the facade threads the classify-time
        result in so the Rust detect round-trip is never paid twice for one
        message. ``None`` (every direct caller) keeps the historical path:
        strategy resolution through ``hooks._determine_strategy``, so
        monkeypatches on that facade method keep biting.
        """
        cr = _cr()
        debug_enabled = cr.logger.isEnabledFor(logging.DEBUG)
        request_debug = (
            {
                "chars": len(content),
                "bytes": len(content.encode("utf-8", errors="replace")),
                "tokens_estimate": len(content.split()),
                "json_shape": cr._json_shape(content),
                "mixed_indicators": cr._mixed_indicators(content),
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
                cr._log_router_debug(
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
            # recompute them here. With a PRECOMPUTED ``detection`` (PERF-2c)
            # the engine resolves strategy itself and skips the re-detect; the
            # ``detection is None`` path is byte-identical to before and keeps
            # ``hooks._determine_strategy`` monkeypatches biting. The detection
            # locals below are built only for the debug log, so the per-call
            # detection cost is paid at most once.
            if detection is None:
                strategy = hooks._determine_strategy(content)
            else:
                strategy = self._determine_strategy(content, hooks=hooks, detection=detection)
            if debug_enabled:
                mixed = cr.is_mixed_content(content)
                debug_detection = (
                    detection if detection is not None else cr._detect_content(content)
                )
                cr._log_router_debug(
                    "content_router_input",
                    **request_debug,
                    detected_content_type=debug_detection.content_type.value,
                    detection_confidence=debug_detection.confidence,
                    selected_strategy=strategy.value,
                    selection_reason=("mixed_content" if mixed else "content_detection"),
                )

            cfg = self.config
            # Byte ceiling (Bug-8): the threshold is in BYTES, so compare the
            # encoded byte length, not the character count — on multibyte content
            # a char count is up to ~4x short and lets an over-ceiling payload slip
            # past the guard. Cheap bounds avoid the encode for the common case:
            # chars are a lower bound on UTF-8 bytes and 4*chars an upper bound, so
            # only content in the ambiguous middle band is actually encoded.
            _byte_ceiling = _huge_content_bytes()
            _char_len = len(content)
            _is_huge = _char_len >= _byte_ceiling or (
                _char_len * 4 >= _byte_ceiling
                and len(content.encode("utf-8", errors="replace")) >= _byte_ceiling
            )
            if _is_huge and (
                cfg.ccr_offload_fallback
                and cfg.ccr_enabled
                and cfg.ccr_inject_marker
                and not cfg.lossless_only
            ):
                # Size ceiling: above the threshold the exact mixed/crush path is
                # super-linear (a 33 MB trace took ~68 s). Offload straight away —
                # O(n) and reversible (head/tail preview + full original in CCR) —
                # instead of paying the crush first and offloading afterwards.
                passthrough = RouterCompressionResult(
                    compressed=content,
                    original=content,
                    strategy_used=CompressionStrategy.PASSTHROUGH,
                )
                offloaded = self._ccr_offload(
                    content, context, passthrough, token_counter=token_counter, hooks=hooks
                )
                result = offloaded if offloaded is not None else passthrough
            elif strategy == CompressionStrategy.MIXED:
                result = hooks._compress_mixed(
                    content,
                    context,
                    question,
                    bias=bias,
                    token_counter=token_counter,
                )
            else:
                result = hooks._compress_pure(
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
            cr.logger.warning(
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
            offloaded = self._ccr_offload(
                content, context, result, token_counter=token_counter, hooks=hooks
            )
            if offloaded is not None:
                result = offloaded

        # One observer call per routing decision; the observer is the
        # forcing function for catching strategy-level regressions.
        # Empty routing_log (passthrough fast path) → no calls.
        self._observe(result, hooks._observer)
        if debug_enabled:
            cr._log_router_debug(
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

    def _observe(
        self, result: RouterCompressionResult, observer: CompressionObserver | None
    ) -> None:
        """Forward each `RoutingDecision` in `result.routing_log` to the
        given `CompressionObserver`. No-op when no observer is set.

        The observer reference is read from the FACADE per call
        (``hooks._observer``) — runtime-reassignable router state stays on the
        router; the engine owns only the forwarding plumbing.

        Observers MUST NOT raise per the protocol contract; if one does
        anyway, swallow at debug level. Compression already succeeded;
        a buggy observer must not turn a 200 into a 500.
        """
        if observer is None:
            return
        for d in result.routing_log:
            try:
                observer.record_compression(
                    strategy=d.strategy.value,
                    original_tokens=d.original_tokens,
                    compressed_tokens=d.compressed_tokens,
                )
            except Exception as e:  # pragma: no cover - defensive
                _cr().logger.debug("CompressionObserver raised (non-fatal): %s", e)

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
            and not _cr()._looks_like_ccr_output(content)
        )

    def _ccr_offload(
        self,
        content: str,
        context: str,
        prior: RouterCompressionResult,
        token_counter: Callable[[str], int] | None = None,
        *,
        hooks: RouterHooks,
    ) -> RouterCompressionResult | None:
        """Store *content* byte-exact in the CCR compression store and ship
        an identity preview + ``{"_ccr_dropped": "<<ccr:HASH>>"}`` sentinel +
        ``Retrieve more`` marker instead.

        Fail-open: returns ``None`` (caller keeps the uncompressed result)
        unless a verified store round-trip guarantees byte-exact recovery —
        the marker is never emitted for content the store cannot reproduce.
        """
        import json

        from ..ccr import marker_grammar

        cr = _cr()
        rows, n_items = self._build_offload_preview(content)
        preview = json.dumps(rows, ensure_ascii=False) if isinstance(rows, list) else rows
        # Bug-11: record REAL token counts on the stored entry, using the same
        # tokenizer the routing_log and the rest of the engine use. Storing
        # ``len(content.split())`` (a whitespace WORD count) as ``original_tokens``
        # silently mixed word counts with tokenizer counts in furl_stats
        # aggregation. ``token_counter`` is the model tokenizer on the normal
        # compress() path; ``cr._word_count`` is the same fallback used elsewhere.
        count = token_counter or cr._word_count
        try:
            from ..cache.compression_store import get_compression_store

            store = get_compression_store()
            ccr_hash = store.store(
                original=content,
                compressed=preview,
                original_tokens=count(content),
                compressed_tokens=count(preview),
                original_item_count=n_items,
                query_context=context or None,
                compression_strategy=CompressionStrategy.CCR_OFFLOAD.value,
                # A durable write that fell open to volatile storage raises
                # DurableWriteError → caught below, keeping the original (audit
                # #3). The round-trip verify alone cannot catch this: a volatile
                # write still resolves in-process.
                require_durable=True,
            )
            # Round-trip verification is an ENGINE-INTERNAL read — it must not
            # feed the retrieval-feedback loop as if the model asked for this
            # content back (Engine P2-13).
            entry = store.retrieve(ccr_hash, record_feedback_signal=False)
            if entry is None or entry.original_content != content:
                cr.logger.warning(
                    "ccr_offload: round-trip failed for %s; keeping original", ccr_hash
                )
                return None
        except Exception:
            cr.logger.warning("ccr_offload: store unavailable; keeping original", exc_info=True)
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

        cr.logger.info(
            "ccr_offload: %d chars (%d items) stored as %s", len(content), n_items, ccr_hash
        )
        return RouterCompressionResult(
            compressed=compressed,
            original=content,
            strategy_used=CompressionStrategy.CCR_OFFLOAD,
            strategy_chain=[*prior.strategy_chain, CompressionStrategy.CCR_OFFLOAD.value],
            routing_log=[
                RoutingDecision(
                    content_type=hooks._content_type_from_strategy(prior.strategy_used),
                    strategy=CompressionStrategy.CCR_OFFLOAD,
                    original_tokens=count(content),
                    compressed_tokens=count(compressed),
                )
            ],
        )

    @staticmethod
    def _truncate_row(item: dict[str, Any]) -> dict[str, Any]:
        """One preview row: long string fields truncated (paths/ids stay
        verbatim), non-scalar fields elided — the full value is in the CCR
        store. Shared by the top-level-array and dominant-array previews."""
        row: dict[str, Any] = {}
        for k, v in item.items():
            if isinstance(v, str) and len(v) > _OFFLOAD_PREVIEW_FIELD_CHARS:
                row[k] = v[:_OFFLOAD_PREVIEW_FIELD_CHARS] + f"… [{len(v)} chars, in CCR]"
            elif isinstance(v, (str, int, float, bool)) or v is None:
                row[k] = v
            else:
                row[k] = f"[{type(v).__name__} omitted, in CCR]"
        return row

    @classmethod
    def _compact_sibling(cls, value: Any, depth: int) -> Any:
        """One sibling value, compacted for the offload preview (review F2).

        Scalars pass through (long strings truncated to the shared field-char
        budget); a nested object recurses to ``depth`` levels then notes its
        remaining size; a short list is kept inline, a long one (the kind that
        SHOULD be offloaded) is elided. Never raises; anything large or deep
        becomes a ``[… in CCR]`` string (recovery is the byte-exact original)."""
        if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            if len(value) > _OFFLOAD_PREVIEW_FIELD_CHARS:
                return value[:_OFFLOAD_PREVIEW_FIELD_CHARS] + f"… [{len(value)} chars, in CCR]"
            return value
        if isinstance(value, dict):
            if depth <= 0:
                return f"[dict of {len(value)} keys, in CCR]"
            out: dict[str, Any] = {}
            for i, (k, v) in enumerate(value.items()):
                if i >= _SIBLING_MAX_KEYS:
                    out["_more_keys"] = f"[{len(value) - _SIBLING_MAX_KEYS} more, in CCR]"
                    break
                out[str(k)] = cls._compact_sibling(v, depth - 1)
            return out
        if isinstance(value, list):
            if depth <= 0 or len(value) > _SIBLING_MAX_LIST:
                return f"[list of {len(value)} items, in CCR]"
            return [cls._compact_sibling(v, depth - 1) for v in value]
        return f"[{type(value).__name__} omitted, in CCR]"

    @classmethod
    def _compact_sibling_fields(cls, fields: dict[str, Any]) -> dict[str, Any]:
        """Bounded ``{key: value}`` preview of a dominant-array object's sibling
        fields (review F2 / RF4). Keeps scalar values verbatim — they are tiny
        and usually the most important fact — and small nested objects to a
        bounded depth so their scalar leaves survive; large arrays / deep objects
        are elided.

        Bounded BY CONSTRUCTION, not by a soft after-check: the top-level key
        count is capped at _SIBLING_MAX_KEYS (the per-dict cap never applied to
        the sibling keys themselves) AND the running serialized length is
        accumulated as each key is added, eliding the remainder under a
        ``_more_keys`` note the moment another key would exceed
        _SIBLING_FIELDS_MAX_CHARS. The serialized result therefore never exceeds
        that cap however many or however large the siblings are — the previous
        scalar-only fallback still returned every scalar verbatim and could run
        to tens of KB. Recovery is the byte-exact stored original."""
        import json

        out: dict[str, Any] = {}
        kept = 0
        total = len(fields)
        for key, value in fields.items():
            if kept >= _SIBLING_MAX_KEYS:
                break
            candidate = {**out, str(key): cls._compact_sibling(value, _SIBLING_MAX_DEPTH)}
            serialized = json.dumps(candidate, ensure_ascii=False, default=str)
            # Stop before the blob would exceed the cap; the reserve leaves room
            # for the trailing _more_keys note so the FINAL result stays under it.
            if len(serialized) > _SIBLING_FIELDS_MAX_CHARS - _SIBLING_MORE_KEYS_RESERVE:
                break
            out = candidate
            kept += 1
        if kept < total:
            out["_more_keys"] = f"[{total - kept} more, in CCR]"
        return out

    @staticmethod
    def _dominant_array(parsed: Any) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | None:
        """A JSON object with EXACTLY one dominant inner array — a non-empty
        list of dicts (e.g. a Chrome trace's ``traceEvents``). Mirrors
        ``sniff_envelope``'s fail-open "exactly one, else None" rule without its
        wrapper-key allowlist. Returns ``(key, inner, other_fields)`` — where
        ``other_fields`` maps each sibling key to its VALUE (not just its name),
        so tiny scalar siblings (a crash report's exception type/signal, a
        trace's metadata) survive into the preview (review F2) — or ``None``
        when zero or more than one key qualifies (ambiguous)."""
        if not isinstance(parsed, dict):
            return None
        matches = [
            k
            for k, v in parsed.items()
            if isinstance(v, list) and v and all(isinstance(item, dict) for item in v)
        ]
        if len(matches) != 1:
            return None
        key = matches[0]
        other = {k: v for k, v in parsed.items() if k != key}
        return key, parsed[key], other

    @staticmethod
    def _dominant_type_name(type_counts: Counter[str]) -> str:
        """Most frequent value-type name for a field, ties broken by name for
        determinism. ``null`` only wins when a field is exclusively null."""
        if not type_counts:
            return "null"
        return max(sorted(type_counts), key=lambda name: type_counts[name])

    @staticmethod
    def _type_name(value: object) -> str:
        """Domain type label for a cell. ``bool`` is checked before ``int``
        because Python's ``bool`` is an ``int`` subclass — True/False must read
        as ``bool``, never ``int`` (and must never enter numeric ranges)."""
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if isinstance(value, str):
            return "str"
        if isinstance(value, dict):
            return "dict"
        if isinstance(value, list):
            return "list"
        return type(value).__name__

    @staticmethod
    def _is_hashable_scalar(value: object) -> bool:
        """Only hashable scalars are counted in histograms. ``None`` is elided
        (an absent/empty field is not a category); dict/list/unhashable values
        are elided (their full form is in the CCR store)."""
        return isinstance(value, (str, int, float, bool))

    def _summarize_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        key: str | None,
        other_fields: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], int]:
        """O(n) signal-aware summary of a list of dict rows.

        Two sequential O(n) passes over *rows*: the first tallies per-field type
        counts, hashable-scalar value counts, and numeric ranges; the second (in
        ``_build_examples``) attaches one concrete example row to each top value
        of the chosen "type" field once its top values are known. Output is
        bounded (per-field top-K histogram + <=_SUMMARY_TOP_VALUES example rows +
        _SUMMARY_SAMPLE_ROWS sample rows), so it stays a few KB for any *n*.

        Returns ``([{"_ccr_summary": {...}}], len(rows))``. Never mutates *rows*.
        Pure given *rows*: recovery is always the byte-exact stored original.
        """
        n = len(rows)
        # --- Pass 1: per-field aggregates (single scan over rows). ---
        type_counts: dict[str, Counter[str]] = {}
        value_counts: dict[str, Counter[Any]] = {}
        numeric_ranges: dict[str, tuple[float, float]] = {}
        for row in rows:
            for field_name, value in row.items():
                tc = type_counts.get(field_name)
                if tc is None:
                    tc = type_counts[field_name] = Counter()
                tc[self._type_name(value)] += 1
                if self._is_hashable_scalar(value):
                    vc = value_counts.get(field_name)
                    if vc is None:
                        vc = value_counts[field_name] = Counter()
                    vc[value] += 1
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    lo, hi = numeric_ranges.get(field_name, (value, value))
                    numeric_ranges[field_name] = (min(lo, value), max(hi, value))

        # --- Field classification (over fields, bounded by distinct values). ---
        schema = {
            field_name: self._dominant_type_name(tc) for field_name, tc in type_counts.items()
        }
        # Coverage = rows in which the field is present (a field may be missing
        # from heterogeneous rows). Drives the "type" field pick in examples.
        coverage = {field_name: tc.total() for field_name, tc in type_counts.items()}
        categorical: dict[str, Counter[Any]] = {
            field_name: vc
            for field_name, vc in value_counts.items()
            if self._is_categorical(len(vc), n)
        }
        summary_value_counts: dict[str, dict[str, Any]] = {
            field_name: self._top_value_counts(vc) for field_name, vc in categorical.items()
        }
        ranges = {
            field_name: {"min": lo, "max": hi} for field_name, (lo, hi) in numeric_ranges.items()
        }

        summary: dict[str, Any] = {
            "array": key,
            "count": n,
            "other_fields": self._compact_sibling_fields(other_fields),
            "schema": schema,
            "value_counts": summary_value_counts,
            "ranges": ranges,
            "examples": self._build_examples(rows, categorical, coverage),
            "sample_rows": [self._truncate_row(rows[i]) for i in self._sample_indices(n)],
            "retrieve": _CCR_SUMMARY_RETRIEVE_HINT,
        }
        return [{"_ccr_summary": summary}], n

    @staticmethod
    def _is_categorical(distinct: int, n: int) -> bool:
        """A scalar field is a categorical histogram when it is low-cardinality:
        few distinct values in absolute terms, OR a small distinct/row ratio (so
        a large trace with a bounded name set still qualifies). An all-unique
        field fails both and is excluded."""
        if distinct == 0:
            return False
        if distinct <= _SUMMARY_MAX_CATEGORICAL_DISTINCT:
            return True
        return n > 0 and (distinct / n) < _SUMMARY_CATEGORICAL_RATIO

    @staticmethod
    def _summary_key(value: object) -> str:
        """JSON-object key for a histogram value: stringified (a JSON key is
        always a string) and truncated to the same _OFFLOAD_PREVIEW_FIELD_CHARS
        budget _truncate_row applies, so value_counts can never re-embed a large
        field value verbatim (no full-content duplication; output stays bounded).
        Two long values sharing that prefix collapse to one key — a harmless
        preview artifact; their counts are summed so the histogram stays honest.
        Recovery is unaffected (it is the byte-exact stored original)."""
        text = str(value)
        if len(text) > _OFFLOAD_PREVIEW_FIELD_CHARS:
            return text[:_OFFLOAD_PREVIEW_FIELD_CHARS] + f"… [{len(text)} chars, in CCR]"
        return text

    def _top_value_counts(self, counts: Counter[Any]) -> dict[str, Any]:
        """Top-_SUMMARY_TOP_VALUES ``{value: count}`` for one categorical field
        plus ``_distinct`` (the true distinct-value count). Keys are bounded via
        ``_summary_key``; the count is the AGGREGATE answer. Ordered by count
        desc, value asc for determinism. Counts of keys that collapse under
        truncation are summed."""
        top = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))[:_SUMMARY_TOP_VALUES]
        histogram: dict[str, Any] = {}
        for value, count in top:
            key = self._summary_key(value)
            histogram[key] = histogram.get(key, 0) + count
        histogram["_distinct"] = len(counts)
        return histogram

    @staticmethod
    def _primary_categorical_field(
        categorical: dict[str, Counter[Any]],
        coverage: dict[str, int],
    ) -> str | None:
        """The "type" field for the example rows: among categorical fields, the
        one present in the MOST rows, then (tie) with the MOST distinct values —
        the axis that best partitions EVERY row into notable kinds.

        Coverage is the primary key so a sparse high-cardinality IDENTIFIER (a
        hex ``id`` on a few rows, a per-row duration) cannot win over the true
        type field: an event ``name`` / log level / status labels every row,
        whereas an id/quantity does not (and already gets ``ranges``). On a
        Chrome trace: ``name`` (covers all 140669 rows, 242 distinct) beats
        ``tdur``/``id`` (sparser) and ``cat``/``ph`` (full-coverage but coarser).
        Tie-broken by field name for determinism. Domain-agnostic — the choice
        is by coverage + cardinality, never by field name. ``None`` when there is
        no categorical field."""
        if not categorical:
            return None
        return max(
            sorted(categorical),
            key=lambda field_name: (coverage.get(field_name, 0), len(categorical[field_name])),
        )

    def _build_examples(
        self,
        rows: list[dict[str, Any]],
        categorical: dict[str, Counter[Any]],
        coverage: dict[str, int],
    ) -> dict[str, Any]:
        """The ANOMALY answer: one concrete example row per top value of the
        primary categorical field, so every notable event type (e.g. an
        infrequent ``DroppedFrame``) is surfaced INLINE with its own fields
        (incl. its ts).

        Picks the "type" field, takes its top-_SUMMARY_TOP_VALUES values by count,
        then a single O(n) scan captures the FIRST row for each wanted value
        (stable; ties keep the earliest). ``_truncate_row`` runs only on the <=K
        captured rows. Domain-agnostic — no hardcoded field or value names.
        Returns ``{}`` when there is no categorical field (sample_rows still
        conveys shape). Never mutates *rows*."""
        field_name = self._primary_categorical_field(categorical, coverage)
        if field_name is None:
            return {}
        wanted = {
            value for value, _count in categorical[field_name].most_common(_SUMMARY_TOP_VALUES)
        }
        by_value: dict[str, Any] = {}
        seen: set[Any] = set()
        for row in rows:
            value = row.get(field_name)
            # Guard membership: a categorical field can still hold an unhashable
            # value in some rows (str in most, list in a few) — ``value in set``
            # would raise on it. Only scalars are counted, so only scalars are
            # wanted example values.
            if not self._is_hashable_scalar(value) or value in seen or value not in wanted:
                continue
            seen.add(value)
            # First occurrence wins; ``_summary_key`` matches value_counts' keys
            # (two values sharing a truncated prefix keep the earliest row).
            by_value.setdefault(self._summary_key(value), self._truncate_row(row))
            if len(seen) == len(wanted):
                break
        return {"field": field_name, "by_value": by_value}

    @staticmethod
    def _sample_indices(n: int) -> list[int]:
        """Up to _SUMMARY_SAMPLE_ROWS evenly-spaced row indices (head/mid/tail)
        for a shape sample. Deterministic and de-duplicated for small *n*."""
        if n <= 0:
            return []
        count = min(_SUMMARY_SAMPLE_ROWS, n)
        if count == 1:
            return [0]
        seen: list[int] = []
        for step in range(count):
            index = step * (n - 1) // (count - 1)
            if index not in seen:
                seen.append(index)
        return seen

    @staticmethod
    def _extract_error_lines(omitted: list[str]) -> list[str]:
        """Severity-matched lines pulled from the OMITTED middle of a plain-text
        head/tail preview (Bug-4 / Med-10), bounded in count and per-line width.

        Total + linear: a flat, anchored severity alternation
        (``_OFFLOAD_SEVERITY_RE`` — no nested quantifiers) scanned line by line,
        so no single line can trigger catastrophic backtracking. Each surfaced
        line is clipped to ``_OFFLOAD_ERROR_LINE_MAX_CHARS`` and at most
        ``_OFFLOAD_ERROR_LINES_MAX`` are kept, so an error-dense middle can never
        blow the preview back up to the size the offload just avoided. Operates
        on already-redacted content (compress() redacts BEFORE offload), so a
        surfaced line can carry a masked ``[REDACTED:...]`` token but never a
        live secret.
        """
        surfaced: list[str] = []
        for line in omitted:
            if _OFFLOAD_SEVERITY_RE.search(line):
                surfaced.append(line[:_OFFLOAD_ERROR_LINE_MAX_CHARS])
                if len(surfaced) >= _OFFLOAD_ERROR_LINES_MAX:
                    break
        return surfaced

    def _build_offload_preview(self, content: str) -> tuple[list[Any] | str, int]:
        """Identity preview of *content*: for a JSON array of objects, and for a
        JSON object with one dominant inner array, a signal-aware ``_ccr_summary``
        (schema + per-field value histograms + numeric ranges + one example row
        per notable value + an evenly-spaced sample) computed in two O(n) passes
        — so aggregate and anomaly questions are answerable inline. The summary is
        fail-open: on any error it falls back to the leading/sampled rows. A
        non-array text falls back to head/tail lines. Returns ``(rows | text,
        n_items)``. Never reversible — recovery is the byte-exact stored original."""
        import json

        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, list) and parsed and all(isinstance(item, dict) for item in parsed):
            try:
                return self._summarize_rows(parsed, key=None, other_fields={})
            except Exception:  # noqa: BLE001 - fail-open to the head/tail preview
                _cr().logger.warning(
                    "ccr_offload: row summary failed; head/tail preview", exc_info=True
                )
            rows: list[Any] = [
                self._truncate_row(item) for item in parsed[:_OFFLOAD_PREVIEW_MAX_ROWS]
            ]
            if len(parsed) > len(rows):
                rows.append({"_preview": f"first {len(rows)} of {len(parsed)} rows"})
            return rows, len(parsed)

        dominant = self._dominant_array(parsed)
        if dominant is not None:
            key, inner, other_fields = dominant
            try:
                return self._summarize_rows(inner, key=key, other_fields=other_fields)
            except Exception:  # noqa: BLE001 - fail-open to the head/tail preview
                _cr().logger.warning(
                    "ccr_offload: array summary failed; head/tail preview", exc_info=True
                )
            head, tail = _OFFLOAD_PREVIEW_ARRAY_HEAD, _OFFLOAD_PREVIEW_ARRAY_TAIL
            if len(inner) <= head + tail:
                sample = [self._truncate_row(item) for item in inner]
            else:
                sample = [self._truncate_row(item) for item in inner[:head]]
                sample.append(
                    {"_preview": f"… {len(inner) - head - tail} of {len(inner)} omitted, in CCR …"}
                )
                sample.extend(self._truncate_row(item) for item in inner[-tail:])
            summary = {
                "_preview": f"'{key}': {len(inner)} items",
                "_other_fields": self._compact_sibling_fields(other_fields),
            }
            return [summary, *sample], len(inner)

        lines = content.splitlines()
        head, tail = _OFFLOAD_PREVIEW_HEAD_LINES, _OFFLOAD_PREVIEW_TAIL_LINES
        if len(lines) <= head + tail:
            return content, 1
        omitted_count = len(lines) - head - tail
        # Bug-4 / Med-10: surface ERROR/Traceback/severity lines from the omitted
        # MIDDLE so a buried error is not invisible in the model-facing preview
        # (previously only recoverable if the agent already knew to retrieve).
        error_lines = self._extract_error_lines(lines[head:-tail])
        if error_lines:
            capped = (
                " (first shown; retrieve for the rest)"
                if (len(error_lines) >= _OFFLOAD_ERROR_LINES_MAX)
                else ""
            )
            middle = (
                f"\n… [{omitted_count} lines omitted, in CCR; "
                f"{len(error_lines)} error/severity line(s) surfaced{capped}] …\n"
                + "\n".join(error_lines)
                + "\n… [end surfaced error lines; full log in CCR] …\n"
            )
        else:
            middle = f"\n… [{omitted_count} lines omitted, in CCR] …\n"
        return "\n".join(lines[:head]) + middle + "\n".join(lines[-tail:]), 1

    def _determine_strategy(
        self,
        content: str,
        *,
        hooks: RouterHooks,
        detection: DetectionResult | None = None,
    ) -> CompressionStrategy:
        """Determine the compression strategy from content analysis.

        ``is_mixed_content`` and ``_detect_content`` are resolved through the
        ``content_router`` module globals AT CALL TIME — the parity tests
        rebind both there. A precomputed ``detection`` (PERF-2c) skips only
        the ``_detect_content`` round-trip; the mixed-content check and the
        strategy mapping still resolve through the same rebindable seams.
        """
        cr = _cr()
        # 1. Check for mixed content
        if cr.is_mixed_content(content):
            return CompressionStrategy.MIXED

        # 2. Detect content type from content itself
        if detection is None:
            detection = cr._detect_content(content)
        return hooks._strategy_from_detection(detection)

    def _compress_mixed(
        self,
        content: str,
        context: str,
        question: str | None = None,
        bias: float = 1.0,
        *,
        token_counter: Callable[[str], int] | None = None,
        hooks: RouterHooks,
    ) -> RouterCompressionResult:
        """Compress mixed content by splitting and routing sections.

        ``split_into_sections`` is resolved through the ``content_router``
        module globals at call time (rebound by the mixed-path tests). When NO
        section actually changed, the ORIGINAL string is returned verbatim as
        PASSTHROUGH (COR-30): reassembly (``"\\n\\n"`` join, re-synthesized
        fences, dropped whitespace-only sections) is not byte-faithful, so
        shipping it at ~zero savings would mutate bytes for nothing.
        """
        cr = _cr()
        count = token_counter or cr._word_count
        sections = cr.split_into_sections(content)
        if cr.logger.isEnabledFor(logging.DEBUG):
            cr._log_router_debug(
                "content_router_mixed_sections",
                section_count=len(sections),
                sections=[cr._section_debug(section, idx) for idx, section in enumerate(sections)],
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
            strategy = hooks._strategy_from_detection_type(section.content_type)

            # Compress section
            original_tokens = count(section.content)
            compressed_content, compressed_tokens, _section_chain = (
                hooks._apply_strategy_to_content(
                    section.content,
                    strategy,
                    context,
                    section.language,
                    question,
                    bias=bias,
                    token_counter=token_counter,
                )
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
        hooks: RouterHooks,
    ) -> RouterCompressionResult:
        """Compress pure (non-mixed) content."""
        original_tokens = (token_counter or _cr()._word_count)(content)

        compressed, compressed_tokens, strategy_chain = hooks._apply_strategy_to_content(
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
                    content_type=hooks._content_type_from_strategy(strategy),
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
        hooks: RouterHooks,
    ) -> tuple[str, int, list[str]]:
        """Apply a compression strategy to content via the owned dispatcher.

        The compressor getters are resolved from the FACADE here on every
        call and passed in, so monkeypatching those router methods still
        takes effect (a construction-time capture in the dispatcher would
        have been stale).
        """
        return self._dispatcher.apply(
            content,
            strategy,
            context,
            language,
            question,
            bias,
            get_smart_crusher=hooks._get_smart_crusher,
            get_search_compressor=hooks._get_search_compressor,
            get_log_compressor=hooks._get_log_compressor,
            get_diff_compressor=hooks._get_diff_compressor,
            get_text_crusher=hooks._get_text_crusher,
            get_code_aware_compressor=hooks._get_code_aware_compressor,
            token_counter=token_counter,
        )


def run_router_passes(
    hooks: ContentRouter,
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
        if k not in _cr()._APPLY_ALLOWED_KWARGS:
            raise TypeError(f"ContentRouter.apply() got an unexpected keyword argument {k!r}")

    # Pre-process: Read lifecycle management (stale/superseded detection)
    if hooks.config.read_lifecycle.enabled:
        from .read_lifecycle import ReadLifecycleManager

        lifecycle_mgr = ReadLifecycleManager(
            hooks.config.read_lifecycle,
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
    # These override hooks.config defaults for this call only.
    skip_user = kwargs.get("compress_user_messages") is not True and hooks.config.skip_user_messages
    skip_system = kwargs.get("compress_system_messages") is not True
    protect_recent = kwargs.get("protect_recent", hooks.config.protect_recent_code)
    protect_analysis = kwargs.get("protect_analysis_context", hooks.config.protect_analysis_context)
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
        hooks.config.compress_assistant_text_blocks,
    )
    min_chars_for_block_compression = kwargs.get(
        "min_chars_for_block_compression",
        hooks.config.min_chars_for_block_compression,
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
    tool_name_map = hooks._build_tool_name_map(messages)

    # Compute excluded tool IDs based on config. The CCR retrieval tool
    # is unioned in UNCONDITIONALLY (retrieval-loop guard) — a caller
    # override, even ``exclude_tools=set()``, must not re-enable
    # compress→retrieve→compress ping-pong. New frozenset: the caller's
    # set is never mutated. Matching is case-insensitive with
    # fnmatch-style glob support (is_tool_excluded).
    exclude_tools = (
        frozenset(hooks.config.exclude_tools)
        if hooks.config.exclude_tools is not None
        else DEFAULT_EXCLUDE_TOOLS
    ) | ALWAYS_EXCLUDE_TOOLS
    excluded_tool_ids = {
        tool_id for tool_id, name in tool_name_map.items() if is_tool_excluded(name, exclude_tools)
    }

    # --- Adaptive parameters based on context pressure ---
    num_messages = len(messages)
    model_limit = kwargs.get("model_limit", 0)

    # net_mutation_gain (NR2-4): one reversed cumulative sum of
    # per-message token counts, computed ONLY when the gate is enabled
    # (zero cost when off). suffix_tokens[i] = tokens AFTER message i —
    # what loses its provider cache discount if message i's bytes change.
    suffix_tokens: list[int] | None = None
    if hooks.config.enable_net_mutation_gate:
        per_msg = [tokenizer.count_messages([m]) for m in messages]
        suffix_tokens = [0] * num_messages
        running = 0
        for j in range(num_messages - 1, -1, -1):
            suffix_tokens[j] = running
            running += per_msg[j]

    # Adaptive Read protection: protect a fraction of recent messages
    if hooks.config.protect_recent_reads_fraction > 0:
        # Scale: at 10 msgs protect 5, at 50 msgs protect 25, at 200 msgs protect 100
        # But cap at a reasonable floor so very short convos still protect everything
        read_protection_window = max(
            4,  # always protect at least last 4 messages
            int(num_messages * hooks.config.protect_recent_reads_fraction),
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

    min_ratio = hooks._adaptive_min_ratio(context_pressure)

    if context_pressure > 0.3:
        _cr().logger.debug(
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
    if hooks.config.protect_analysis_context:
        analysis_intent = hooks._detect_analysis_intent(messages)

    frozen_message_count = kwargs.get("frozen_message_count", 0)

    # ------------------------------------------------------------------
    # Two-pass parallel compression.
    #
    # Pass 1 (sequential): categorise every message — frozen, protected,
    #   cached, small, etc. are resolved immediately.  Cache-miss messages
    #   that need full compression are collected into *pending_tasks*.
    #
    # Pass 2 (parallel): all cache-miss compressions run concurrently in
    #   a thread pool.  Each hooks.compress() call is independent.
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
            config=hooks.config,
            count_text=tokenizer.count_text,
            detect_content=_cr()._detect_content,
            get_tool_bias=hooks._get_tool_bias,
            get_feedback_hints=hooks._get_feedback_hints,
            result_cache_key=_cr()._result_cache_key,
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
                result_slots[i] = hooks._process_content_blocks(
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
                match hooks._lookup_cached_disposition(
                    content_key, context, min_ratio, route_counts
                ):
                    case ServeOriginal():
                        result_slots[i] = message
                    case ServeCached(compressed=served, strategy=strategy, ratio=ratio):
                        # net_mutation_gain gate ALSO applies to cache
                        # hits: the result cache is content-keyed but the
                        # gate is POSITION-dependent (same bytes, larger
                        # suffix → different economics), so it must be
                        # re-evaluated at every serve site.
                        gate_gain = (
                            net_mutation_gain(
                                tokenizer.count_text(content) - tokenizer.count_text(served),
                                MutationContext(suffix_tokens[i]),
                                hooks.config.cached_token_rate,
                            )
                            if suffix_tokens is not None
                            else None
                        )
                        if gate_gain is not None and gate_gain <= 0:
                            result_slots[i] = message
                            route_counts["net_mutation_gate"] += 1
                        else:
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
        hooks._compress_pending(
            pending_tasks,
            messages,
            result_slots,
            min_ratio=min_ratio,
            token_counter=tokenizer.count_text,
            transforms_applied=transforms_applied,
            compressed_details=compressed_details,
            route_counts=route_counts,
            compressor_timing=compressor_timing,
            suffix_tokens=suffix_tokens,
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
    if route_counts.get("net_mutation_gate"):
        parts.append(f"{route_counts['net_mutation_gate']} unchanged (net mutation gate)")
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
    cs = hooks._cache.stats
    if cs["cache_size"] > 0 or cs["cache_skip_size"] > 0:
        parts.append(
            f"cache[{cs['cache_size']} results, {cs['cache_skip_size']} skips, "
            f"{cs['cache_avg_lookup_ns']:.0f}ns avg]"
        )
    if parts:
        _cr().logger.info(
            "content_router: %d msgs — %s",
            num_messages,
            ", ".join(parts),
        )

    # Forward route_counts to the observer so `/stats` can surface a
    # session-level protection breakdown. The observer
    # may not implement this method on older versions; ignore
    # AttributeError so a non-conforming observer doesn't poison
    # routing.
    if hooks._observer is not None and route_counts:
        try:
            hooks._observer.record_router_route_counts(route_counts)
        except AttributeError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            _cr().logger.debug("Router observer raised (non-fatal): %s", e)

    all_transforms = lifecycle_transforms + transforms_applied
    return TransformResult(
        messages=transformed_messages,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        transforms_applied=all_transforms if all_transforms else [noop_transform(route_counts)],
        markers_inserted=lifecycle_ccr_hashes,
        warnings=warnings,
        timing=compressor_timing,
    )
