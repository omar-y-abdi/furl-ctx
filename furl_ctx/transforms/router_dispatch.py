"""Per-strategy dispatch + fallback chain for the content router.

Owns the body of :meth:`ContentRouter._apply_strategy_to_content`: the
per-strategy compressor dispatch (SMART_CRUSHER / SEARCH / LOG / DIFF /
TEXT / PASSTHROUGH) and the no-savings fallback chain (SMART_CRUSHER -> LOG,
then passthrough). The TEXT arm dispatches to the deterministic
TextCrusher (Engine P2-11 — the passthrough that replaced the excised ML
text compressor is itself replaced); when the arm is gated off
(``enable_text_crusher=False`` or ``lossless_only``) it resolves to
passthrough, so the dispatch stays total.

The SMART_CRUSHER arm additionally owns tabular ingestion (raw CSV →
records → SmartCrusher, via :mod:`.csv_ingest`): non-JSON content that
sniffs as delimiter-consistent CSV is converted to records first, and its
fail-open outcomes (no savings / store veto) resolve to a raw-bytes
passthrough with the LOG fallback suppressed — a detected table must
never fall through to a lossy line-dropper.

This is a TRUE leaf module: it imports nothing from ``content_router`` at
runtime (so there is no import cycle) and never receives the router. Every
dependency is injected explicitly:

* ``config`` and the debug helpers (``log_router_debug`` / ``json_shape``) plus
  the shared ``logger`` come in via the constructor — they are stable for the
  router's lifetime. ``config`` is typed as the narrow :class:`DispatchConfig`
  protocol of exactly the gate fields this module reads (TYPE-3);
  ``ContentRouterConfig`` satisfies it structurally.
* The compressor getters (``get_smart_crusher`` etc.) are passed to
  :meth:`apply` *per call*. The router's thin
  ``_apply_strategy_to_content`` delegator resolves them fresh on every
  invocation, so monkeypatching ``router._get_log_compressor`` (as the
  test-suite does) still bites — a construction-time capture would have been
  stale. Each getter is typed against its concrete compressor class
  (annotation-only ``TYPE_CHECKING`` imports — the lazy-import discipline of
  ``CompressorRegistry`` is untouched), so mypy checks the ``result``
  attribute reads below instead of trusting a ``Callable[[], Any]`` alias.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .csv_ingest import compress_tabular_csv, sniff_csv
from .envelope_ingest import compress_envelope, sniff_envelope
from .html_ingest import compress_html
from .log_template import encode_verified
from .router_policy import CompressionStrategy

if TYPE_CHECKING:
    from .code_aware_compressor import CodeAwareCompressor
    from .content_router import ContentRouterConfig
    from .diff_compressor import DiffCompressor
    from .log_compressor import LogCompressor
    from .search_compressor import SearchCompressor
    from .smart_crusher import SmartCrusher
    from .text_crusher import TextCrusher


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
        config: ContentRouterConfig,
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
        get_smart_crusher: Callable[[], SmartCrusher | None],
        get_search_compressor: Callable[[], SearchCompressor | None],
        get_log_compressor: Callable[[], LogCompressor | None],
        get_diff_compressor: Callable[[], DiffCompressor | None],
        get_text_crusher: Callable[[], TextCrusher | None],
        get_code_aware_compressor: Callable[[], CodeAwareCompressor | None],
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
            get_text_crusher: Router getter for the TextCrusher (P2-11).
            get_code_aware_compressor: Router getter for the opt-in
                CodeAwareCompressor (P2-12).
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
        # Set by the tabular-CSV sub-branch when it already resolved a
        # fail-open passthrough: the generic SMART_CRUSHER → LOG fallback
        # must NOT re-route a detected table through a lossy line-dropper.
        suppress_no_savings_fallback = False

        # Compressor exceptions propagate: a bug in a compressor must stay
        # loud, not degrade into a silent passthrough (#4-upstream). Any
        # strategy without a branch here falls through to the generic
        # passthrough fallback at the bottom, so the dispatch stays total.
        #
        # `lossless_only` (strict lossless-or-passthrough): the search /
        # log / diff compressors all DROP lines, so their arms are gated
        # off below and those strategies resolve to the passthrough
        # fallback — same shape as `enable_*_compressor=False`. The
        # SMART_CRUSHER arm stays live: the Rust crusher routes
        # lossless-or-passthrough internally in that mode.
        if strategy == CompressionStrategy.SMART_CRUSHER:
            # The no-savings Log fallback is handled ONCE by the generic
            # post-dispatch fallback below.
            if self.config.enable_smart_crusher:
                crusher = get_smart_crusher()
                if crusher:
                    compressor_name = type(crusher).__name__
                    # Tabular ingestion: raw CSV → records → SmartCrusher.
                    # Attempted only for content that cannot be JSON (the
                    # arm's normal input) and never under ``lossless_only``
                    # — the conversion is a visible, CCR-recoverable
                    # substitution of the raw bytes, exactly what strict
                    # mode forbids. ``sniff_csv`` is the SAME predicate the
                    # detector used, so the two can never disagree; a
                    # ``None`` here keeps the historical crush path
                    # byte-identical.
                    view = None if self.config.lossless_only else sniff_envelope(content)
                    table = None
                    if not self.config.lossless_only and not content.lstrip().startswith(
                        ("[", "{")
                    ):
                        table = sniff_csv(content)
                    if view is not None:
                        shipped = compress_envelope(
                            content,
                            view,
                            crusher,
                            context=context,
                            bias=bias,
                            token_counter=count,
                        )
                        if shipped is not None:
                            compressed, compressed_tokens = shipped, count(shipped)
                            decision_reason = "smart_crusher_envelope"
                        else:
                            # Fail-open: no savings, or the raw-recovery store
                            # write vetoed — the raw envelope ships byte-exact.
                            compressed, compressed_tokens = content, original_tokens
                            actual_strategy = CompressionStrategy.PASSTHROUGH
                            compressor_name = "Passthrough"
                            decision_reason = "envelope_passthrough"
                            strategy_chain.append(CompressionStrategy.PASSTHROUGH.value)
                            suppress_no_savings_fallback = True
                    elif table is not None:
                        shipped = compress_tabular_csv(
                            content,
                            table,
                            crusher,
                            context=context,
                            bias=bias,
                            token_counter=count,
                        )
                        if shipped is not None:
                            compressed, compressed_tokens = shipped, count(shipped)
                            decision_reason = "smart_crusher_tabular_csv"
                        else:
                            # Fail-open: no savings, or the raw-recovery
                            # store write vetoed — the raw CSV ships
                            # byte-exact. The LOG fallback is suppressed:
                            # lossy line-dropping is never an acceptable
                            # fallback for a detected table (the engine's
                            # reversible CCR offload still applies
                            # downstream).
                            compressed, compressed_tokens = content, original_tokens
                            actual_strategy = CompressionStrategy.PASSTHROUGH
                            compressor_name = "Passthrough"
                            decision_reason = "tabular_csv_passthrough"
                            strategy_chain.append(CompressionStrategy.PASSTHROUGH.value)
                            suppress_no_savings_fallback = True
                    else:
                        crush_result = crusher.crush(content, query=context, bias=bias)
                        compressed, compressed_tokens = (
                            crush_result.compressed,
                            count(crush_result.compressed),
                        )
                        decision_reason = "smart_crusher"

        elif strategy == CompressionStrategy.SEARCH:
            if self.config.enable_search_compressor and not self.config.lossless_only:
                search_compressor = get_search_compressor()
                if search_compressor:
                    compressor_name = type(search_compressor).__name__
                    search_result = search_compressor.compress(content, context=context, bias=bias)
                    compressed, compressed_tokens = (
                        search_result.compressed,
                        count(search_result.compressed),
                    )
                    decision_reason = "search_compressor"

        elif strategy == CompressionStrategy.LOG:
            # LogTemplate (NR2-3b): lossless template mining, tried BEFORE the
            # lossy line-dropping LogCompressor. `encode_verified` self-checks
            # its round-trip and returns None on no structure / no win / verify
            # failure, so this arm is lossless-or-None — architecturally the
            # SMART_CRUSHER shape, NOT the lossy-log shape. It is therefore
            # gated by `enable_log_template` ALONE and stays live under
            # `lossless_only` (strict mode = lossless-or-passthrough, and the
            # wire is self-describing so no CCR store is written). The size
            # gate is re-checked in tokenizer units via the injected `count`:
            # a wire smaller in code points can still cost more tokens.
            log_template_won = False
            if self.config.enable_log_template:
                enc = encode_verified(content)
                if enc is not None:
                    enc_tokens = count(enc.wire)
                    if enc_tokens < original_tokens:
                        compressed, compressed_tokens = enc.wire, enc_tokens
                        actual_strategy = CompressionStrategy.LOG
                        compressor_name = "LogTemplate"
                        decision_reason = "log_template"
                        strategy_chain.append("log_template")
                        log_template_won = True
                        # Surface mining stats on the SAME debug channel the
                        # tabular branch uses for its per-strategy extras.
                        self._log_router_debug(
                            "log_template_encoded",
                            template_count=enc.template_count,
                            templated_lines=enc.templated_lines,
                            verbatim_lines=enc.verbatim_lines,
                        )
            if (
                not log_template_won
                and self.config.enable_log_compressor
                and not self.config.lossless_only
            ):
                log_compressor_arm = get_log_compressor()
                if log_compressor_arm:
                    compressor_name = type(log_compressor_arm).__name__
                    log_arm_result = log_compressor_arm.compress(content, bias=bias)
                    # Use the same count metric the rest of the
                    # router uses; `compressed_line_count` is in
                    # lines, not tokens — recording it here made
                    # ratios meaningless against `original_tokens`.
                    compressed, compressed_tokens = (
                        log_arm_result.compressed,
                        count(log_arm_result.compressed),
                    )
                    decision_reason = "log_compressor"

        elif strategy == CompressionStrategy.DIFF:
            diff_compressor = get_diff_compressor() if not self.config.lossless_only else None
            if diff_compressor:
                compressor_name = type(diff_compressor).__name__
                diff_result = diff_compressor.compress(content, context=context)
                compressed, compressed_tokens = (
                    diff_result.compressed,
                    count(diff_result.compressed),
                )
                decision_reason = "diff_compressor"

        elif strategy == CompressionStrategy.TEXT:
            # Deterministic extractive prose compression (Engine P2-11).
            # Gated like the other line-dropping compressors: never under
            # `lossless_only` (the crusher drops segments). Below its size
            # floors (600 chars / 15 segments) the crusher returns the
            # original bytes, which the no-savings fallback below turns
            # into an honest passthrough chain entry. When the arm is
            # gated off, `compressed` stays None and the generic
            # passthrough fallback at the bottom fires (same shape as
            # `enable_search_compressor=False`) — large uncompressible
            # text still gets the router's reversible CCR offload
            # downstream.
            # HTML main-content extraction first: WebFetch/HTML ships as extracted
            # article text + a marker recovering the full raw HTML. Lossy-but-
            # reversible, so gated off under lossless_only like the prose crusher.
            html_shipped = (
                None if self.config.lossless_only else compress_html(content, token_counter=count)
            )
            if html_shipped is not None:
                compressed, compressed_tokens = html_shipped, count(html_shipped)
                compressor_name = "HtmlExtractor"
                decision_reason = "text_html_extract"
            elif self.config.enable_text_crusher and not self.config.lossless_only:
                text_crusher = get_text_crusher()
                if text_crusher:
                    compressor_name = type(text_crusher).__name__
                    text_result = text_crusher.compress(content, context=context, bias=bias)
                    compressed, compressed_tokens = (
                        text_result.compressed,
                        count(text_result.compressed),
                    )
                    decision_reason = "text_crusher"

        elif strategy == CompressionStrategy.CODE_AWARE:
            # Opt-in AST code compression (Engine P2-12, default OFF — the
            # policy maps SOURCE_CODE here only when `enable_code_aware`).
            # Gated off under `lossless_only` like the other line-dropping
            # compressors (body truncation is a visible, CCR-recoverable
            # reduction). The compressor itself fails open to a passthrough
            # result (missing tree-sitter, unknown language, invalid render,
            # store-write failure), which the generic no-savings handling
            # below reports honestly.
            if self.config.enable_code_aware and not self.config.lossless_only:
                code_compressor = get_code_aware_compressor()
                if code_compressor:
                    compressor_name = type(code_compressor).__name__
                    code_result = code_compressor.compress(
                        content, language=language, context=context
                    )
                    compressed, compressed_tokens = (
                        code_result.compressed,
                        count(code_result.compressed),
                    )
                    decision_reason = "code_aware_compressor"

        elif strategy == CompressionStrategy.PASSTHROUGH:
            compressed = content
            compressed_tokens = original_tokens
            compressor_name = "Passthrough"
            decision_reason = "explicit_passthrough"

        # If compression succeeded, run the no-savings fallback chain
        if compressed is not None and compressed_tokens is not None:
            fallback_no_savings = compressed == content or compressed_tokens >= original_tokens
            if (
                strategy == CompressionStrategy.SMART_CRUSHER
                and fallback_no_savings
                and not suppress_no_savings_fallback
            ):
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
                elif self.config.enable_log_compressor and not self.config.lossless_only:
                    # Last-ditch: line-structured compressors (log dumps
                    # land here — repetitive JSONL that SmartCrusher
                    # can't shrink but the log compressor can). Only
                    # attempted when the strategy was SMART_CRUSHER so
                    # we don't reroute genuine code/diff content — and
                    # never under `lossless_only` (the log compressor
                    # drops lines).
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
