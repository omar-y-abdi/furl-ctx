"""Content-level compression engine for the content router.

Extracted from ``content_router.py`` (§4.1 S5). Owns compressing ONE string:
strategy determination, the pure/mixed paths, the per-strategy dispatch (via
the owned :class:`StrategyDispatcher` + :class:`CompressorRegistry`), the
empty-output guard, the reversible CCR-offload fallback, and the observer
plumbing (the TOIN successor — one ``record_compression`` per routing
decision). Zero message/dict knowledge; stateless per call. The engine's
result types (:class:`RoutingDecision` / :class:`RouterCompressionResult`)
live here and are re-exported by ``content_router`` for back-compat.

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
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from .compressor_registry import CompressorRegistry
from .content_detector import ContentType
from .router_dispatch import StrategyDispatcher
from .router_policy import CompressionStrategy

if TYPE_CHECKING:
    from types import ModuleType

    from .base import CompressionObserver
    from .code_aware_compressor import CodeAwareCompressor
    from .content_router import ContentRouterConfig
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
        hooks: RouterHooks,
    ) -> RouterCompressionResult:
        """Compress content using optimal strategy based on content detection.

        The body of ``ContentRouter.compress`` (a pure move); see the facade
        docstring for the public contract. ``hooks`` is the router facade —
        strategy selection and the pure/mixed paths resolve through it.
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
            # recompute them here. The detection locals below are built only for
            # the debug log, so the per-call detection cost is paid once.
            strategy = hooks._determine_strategy(content)
            if debug_enabled:
                mixed = cr.is_mixed_content(content)
                detection = cr._detect_content(content)
                cr._log_router_debug(
                    "content_router_input",
                    **request_debug,
                    detected_content_type=detection.content_type.value,
                    detection_confidence=detection.confidence,
                    selected_strategy=strategy.value,
                    selection_reason=("mixed_content" if mixed else "content_detection"),
                )

            if strategy == CompressionStrategy.MIXED:
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
        count = token_counter or cr._word_count
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
    def _build_offload_preview(content: str) -> tuple[list[Any] | str, int]:
        """Identity preview of *content*: for a JSON array of objects, the
        leading rows with long string fields truncated (paths/ids stay
        verbatim); otherwise the head/tail lines. Returns ``(rows | text,
        n_items)``. Never reversible — recovery is the stored original."""
        import json

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

    def _determine_strategy(self, content: str, *, hooks: RouterHooks) -> CompressionStrategy:
        """Determine the compression strategy from content analysis.

        ``is_mixed_content`` and ``_detect_content`` are resolved through the
        ``content_router`` module globals AT CALL TIME — the parity tests
        rebind both there.
        """
        cr = _cr()
        # 1. Check for mixed content
        if cr.is_mixed_content(content):
            return CompressionStrategy.MIXED

        # 2. Detect content type from content itself
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
