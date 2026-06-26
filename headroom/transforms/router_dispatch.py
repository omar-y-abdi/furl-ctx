"""Per-strategy dispatch + fallback chain for the content router.

Owns the body of :meth:`ContentRouter._apply_strategy_to_content`: the
per-strategy compressor dispatch (SMART_CRUSHER / SEARCH / LOG / DIFF / HTML /
KOMPRESS / TEXT / PASSTHROUGH) and the no-savings fallback chain
(SMART_CRUSHER -> KOMPRESS, then -> LOG, then passthrough).

This is a TRUE leaf module: it imports nothing from ``content_router`` (so there
is no import cycle) and never receives the router. Every dependency is injected
explicitly:

* ``config`` and the debug helpers (``log_router_debug`` / ``json_shape``) plus
  the shared ``logger`` come in via the constructor — they are stable for the
  router's lifetime.
* The compressor getters (``get_smart_crusher`` etc.), ``try_ml_compressor`` and
  ``record_to_toin`` are passed to :meth:`apply` *per call*. The router's thin
  ``_apply_strategy_to_content`` delegator resolves them fresh on every
  invocation, so monkeypatching ``router._get_log_compressor`` /
  ``router._try_ml_compressor`` (as the test-suite does) still bites — a
  construction-time capture would have been stale.

The ML text path (``try_ml_compressor``) and TOIN recording
(``record_to_toin``) deliberately stay on the router: ``try_ml_compressor``
binds the per-request ``RouterRuntime`` (target_ratio / kompress_model) as a
closure via ``_apply_strategy_to_content`` so it is NOT self-contained, and
both forward to router methods the test-suite monkeypatches. The dispatcher
only ever calls them as opaque callables.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .router_policy import CompressionStrategy

# Type aliases for the injected callables (documentation, not enforcement).
_GetCompressor = Callable[[], Any]
_TryMlCompressor = Callable[[str, str, str | None], tuple[str, int]]
_RecordToToin = Callable[..., None]


class StrategyDispatcher:
    """Applies a compression strategy and runs the no-savings fallback chain.

    Holds no reference to the :class:`ContentRouter`. Constructed once with the
    router's ``config`` and the (module-level) debug helpers; the per-strategy
    compressor getters and the two router-bound callables are supplied to
    :meth:`apply` on each call.
    """

    def __init__(
        self,
        config: Any,
        *,
        logger: logging.Logger,
        log_router_debug: Callable[..., None],
        json_shape: Callable[[str], dict[str, Any]],
    ) -> None:
        self.config = config
        self._logger = logger
        self._log_router_debug = log_router_debug
        self._json_shape = json_shape

    def apply(
        self,
        content: str,
        strategy: CompressionStrategy,
        context: str,
        language: str | None = None,
        question: str | None = None,
        bias: float = 1.0,
        *,
        get_smart_crusher: _GetCompressor,
        get_search_compressor: _GetCompressor,
        get_log_compressor: _GetCompressor,
        get_diff_compressor: _GetCompressor,
        get_html_extractor: _GetCompressor,
        try_ml_compressor: _TryMlCompressor,
        record_to_toin: _RecordToToin,
    ) -> tuple[str, int, list[str]]:
        """Apply a compression strategy to content.

        Args:
            content: Content to compress.
            strategy: Strategy to use.
            context: User context.
            language: Language hint for code.
            question: Optional question for QA-aware compression.
            bias: Compression bias multiplier (>1 = keep more, <1 = keep fewer).
            get_smart_crusher: Router getter for the SmartCrusher compressor.
            get_search_compressor: Router getter for the SearchCompressor.
            get_log_compressor: Router getter for the LogCompressor.
            get_diff_compressor: Router getter for the DiffCompressor.
            get_html_extractor: Router getter for the HTMLExtractor.
            try_ml_compressor: Router-bound ML (Kompress) compression callable.
            record_to_toin: Router-bound TOIN recording callable.

        Returns:
            Tuple of (compressed_content, compressed_token_count,
            strategy_chain). The chain lists every strategy attempted
            in order — first the requested one, then any fallbacks.
            Single-entry chain means a direct hit; multi-entry means
            the fallback chain fired (e.g. ``[smart_crusher, kompress,
            log]``). Log readers use this to see *how* we got to the
            final compressor without parsing decision_reason strings.
        """
        logger = self._logger
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
                    compressed, compressed_tokens = try_ml_compressor(
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
                    crusher = get_smart_crusher()
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
                    compressor = get_search_compressor()
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
                    compressor = get_log_compressor()
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
                compressor = get_diff_compressor()
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
                    extractor = get_html_extractor()
                    if extractor:
                        compressor_name = type(extractor).__name__
                        result = extractor.extract(content)
                        compressed = result.extracted
                        # Estimate tokens from extracted text (simple word count)
                        compressed_tokens = len(compressed.split()) if compressed else 0
                        decision_reason = "html_extractor"

            elif strategy == CompressionStrategy.KOMPRESS:
                compressed, compressed_tokens = try_ml_compressor(content, context, question)
                compressor_name = "KompressCompressor"
                decision_reason = "kompress"

            elif strategy == CompressionStrategy.TEXT:
                # Prefer Kompress ML compressor for text
                # Passes through unchanged if Kompress not available
                compressed, compressed_tokens = try_ml_compressor(content, context, question)
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
                fallback_compressed, fallback_tokens = try_ml_compressor(
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
                        log_compressor = get_log_compressor()
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
                self._log_router_debug(
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
                    json_shape=self._json_shape(content),
                    input=content,
                    output=compressed,
                    error=error,
                )
            record_to_toin(
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
            self._log_router_debug(
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
                json_shape=self._json_shape(content),
                input=content,
                output=content,
                error=error,
            )
        return content, original_tokens, strategy_chain
