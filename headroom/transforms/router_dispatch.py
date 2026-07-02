"""Per-strategy dispatch + fallback chain for the content router.

Owns the body of :meth:`ContentRouter._apply_strategy_to_content`: the
per-strategy compressor dispatch (SMART_CRUSHER / SEARCH / LOG / DIFF /
TEXT / PASSTHROUGH) and the no-savings fallback chain (SMART_CRUSHER -> LOG,
then passthrough). Strategies with no compressor (CODE_AWARE — the AST
compressor was retired; TEXT — the ML text compressor was excised) resolve to
passthrough, so the dispatch stays total.

This is a TRUE leaf module: it imports nothing from ``content_router`` (so there
is no import cycle) and never receives the router. Every dependency is injected
explicitly:

* ``config`` and the debug helpers (``log_router_debug`` / ``json_shape``) plus
  the shared ``logger`` come in via the constructor — they are stable for the
  router's lifetime.
* The compressor getters (``get_smart_crusher`` etc.) are passed to
  :meth:`apply` *per call*. The router's thin
  ``_apply_strategy_to_content`` delegator resolves them fresh on every
  invocation, so monkeypatching ``router._get_log_compressor`` (as the
  test-suite does) still bites — a construction-time capture would have been
  stale.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .router_policy import CompressionStrategy

# Type alias for the injected callables (documentation, not enforcement).
_GetCompressor = Callable[[], Any]


def _word_count(text: str) -> int:
    """Whitespace word count — the historical token proxy, used whenever the
    caller threads no real ``token_counter`` (COR-17)."""
    return len(text.split())


class StrategyDispatcher:
    """Applies a compression strategy and runs the no-savings fallback chain.

    Holds no reference to the :class:`ContentRouter`. Constructed once with the
    router's ``config`` and the (module-level) debug helpers; the per-strategy
    compressor getters are supplied to :meth:`apply` on each call.
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
        token_counter: Callable[[str], int] | None = None,
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
            token_counter: Optional real token counter (COR-17). When set,
                every token count in this dispatch — original, per-strategy
                compressed, and the fallback-chain comparisons — is measured
                with it, so the ratio the router's ``min_ratio`` gate reads is
                in tokenizer units, not whitespace words (word-ratios
                systematically overstate savings on low-whitespace compaction
                outputs). ``None`` keeps the historical word counts,
                byte-identical to prior behavior.

        Returns:
            Tuple of (compressed_content, compressed_token_count,
            strategy_chain). The chain lists every strategy attempted
            in order — first the requested one, then any fallbacks.
            Single-entry chain means a direct hit; multi-entry means
            the fallback chain fired (e.g. ``[smart_crusher, log]``).
            Log readers use this to see *how* we got to the final
            compressor without parsing decision_reason strings.
        """
        logger = self._logger
        count = token_counter or _word_count
        # Original token count — the no-savings comparisons below measure against it.
        original_tokens = count(content)
        compressed: str | None = None
        compressed_tokens: int | None = None
        requested_strategy = strategy
        actual_strategy = strategy
        compressor_name = strategy.value
        decision_reason = "strategy_not_enabled_or_unavailable"
        strategy_chain: list[str] = [strategy.value]

        # Compressor exceptions propagate: a bug in a compressor must stay
        # loud, not degrade into a silent passthrough (#4-upstream). CODE_AWARE
        # has no branch here — the AST compressor was retired and the ML text
        # compressor excised — so it falls through to the generic passthrough
        # fallback at the bottom.
        if strategy == CompressionStrategy.SMART_CRUSHER:
            # The no-savings Log fallback is handled ONCE by the generic
            # post-dispatch fallback below.
            if self.config.enable_smart_crusher:
                crusher = get_smart_crusher()
                if crusher:
                    compressor_name = type(crusher).__name__
                    result = crusher.crush(content, query=context, bias=bias)
                    compressed, compressed_tokens = (
                        result.compressed,
                        count(result.compressed),
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
                        count(result.compressed),
                    )
                    decision_reason = "search_compressor"

        elif strategy == CompressionStrategy.LOG:
            if self.config.enable_log_compressor:
                compressor = get_log_compressor()
                if compressor:
                    compressor_name = type(compressor).__name__
                    result = compressor.compress(content, bias=bias)
                    # Use the same count metric the rest of the
                    # router uses; `compressed_line_count` is in
                    # lines, not tokens — recording it here made
                    # ratios meaningless against `original_tokens`.
                    compressed, compressed_tokens = (
                        result.compressed,
                        count(result.compressed),
                    )
                    decision_reason = "log_compressor"

        elif strategy == CompressionStrategy.DIFF:
            compressor = get_diff_compressor()
            if compressor:
                compressor_name = type(compressor).__name__
                result = compressor.compress(content, context=context)
                compressed, compressed_tokens = (
                    result.compressed,
                    count(result.compressed),
                )
                decision_reason = "diff_compressor"

        elif strategy == CompressionStrategy.TEXT:
            # Plain text has no compressor (the ML text compressor was
            # excised; when it was merely not installed this path already
            # passed through unchanged — that behavior is preserved).
            # Large uncompressible text still gets the router's reversible
            # CCR offload downstream.
            compressed = content
            compressed_tokens = original_tokens
            compressor_name = "Passthrough"
            decision_reason = "text_passthrough"

        elif strategy == CompressionStrategy.PASSTHROUGH:
            compressed = content
            compressed_tokens = original_tokens
            compressor_name = "Passthrough"
            decision_reason = "explicit_passthrough"

        # If compression succeeded, run the no-savings fallback chain
        if compressed is not None and compressed_tokens is not None:
            fallback_no_savings = compressed == content or compressed_tokens >= original_tokens
            if strategy == CompressionStrategy.SMART_CRUSHER and fallback_no_savings:
                if compressed_tokens > original_tokens:
                    # Never ship an EXPANDED result — revert to the original
                    # bytes. (Historically the not-installed ML fallback
                    # returned the original here and won the token
                    # comparison; the revert is now explicit.)
                    strategy_chain.append(CompressionStrategy.PASSTHROUGH.value)
                    compressed = content
                    compressed_tokens = original_tokens
                    actual_strategy = CompressionStrategy.PASSTHROUGH
                    compressor_name = "Passthrough"
                    decision_reason = f"{decision_reason}_no_savings_passthrough"
                elif self.config.enable_log_compressor:
                    # Last-ditch: line-structured compressors (log dumps
                    # land here — repetitive JSONL that SmartCrusher
                    # can't shrink but the log compressor can). Only
                    # attempted when the strategy was SMART_CRUSHER so
                    # we don't reroute genuine code/diff content.
                    log_compressor = get_log_compressor()
                    if log_compressor is not None:
                        strategy_chain.append(CompressionStrategy.LOG.value)
                        try:
                            log_result = log_compressor.compress(content, bias=bias)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("Log fallback failed for SMART_CRUSHER: %s", exc)
                        else:
                            log_compressed_tokens = count(log_result.compressed)
                            if log_compressed_tokens < compressed_tokens:
                                compressed = log_result.compressed
                                compressed_tokens = log_compressed_tokens
                                actual_strategy = CompressionStrategy.LOG
                                compressor_name = type(log_compressor).__name__
                                decision_reason = f"{decision_reason}_fallback_log_after_no_savings"

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
                    error=None,
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
                error=None,
            )
        return content, original_tokens, strategy_chain
