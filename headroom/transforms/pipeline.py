"""Transform pipeline orchestration for Headroom SDK."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from ..config import (
    CompressRequest,
    DiffArtifact,
    HeadroomConfig,
    TransformDiff,
    TransformResult,
    WasteSignals,
)
from ..tokenizer import Tokenizer
from ..utils import deep_copy_messages
from .base import Transform
from .cache_aligner import CacheAligner
from .content_router import ContentRouter
from .cross_message_dedup import CrossMessageDeduper

logger = logging.getLogger(__name__)

_N = TypeVar("_N", int, float)


class _ProviderLike(Protocol):
    """Minimal structural interface for provider objects.

    Only ``get_token_counter`` is accessed on the provider inside
    ``TransformPipeline``; keeping the protocol minimal avoids coupling
    this module to any concrete provider class.
    """

    def get_token_counter(self, model: str) -> Any:
        """Return a token-counting callable for *model*."""


def _breaker_env(name: str, default: _N, cast: Callable[[str], _N]) -> _N:
    """Parse a circuit-breaker env var, falling back on bad input.

    The breaker is a safety net — a typo'd value must degrade to the
    default with a warning, not crash startup.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return cast(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default


class TransformPipeline:
    """
    Orchestrates multiple transforms in the correct order.

    Transform order:
    1. Cache Aligner (opt-in — disabled by default via
       ``CacheAlignerConfig.enabled``) - detector-only prefix-stability
       warnings; never rewrites messages
    2. Content Router - intelligent content-aware compression (routes to appropriate
       compressor: SmartCrusher for JSON, log/search/diff/HTML compressors, etc.)

    There is no IntelligentContextManager / RollingWindow
    "drop messages from history" stage. Live-zone-only compression is the
    sole strategy — message-list mutation never happens
    in the pipeline.
    """

    def __init__(
        self,
        config: HeadroomConfig | None = None,
        transforms: list[Transform] | None = None,
        provider: _ProviderLike | None = None,
    ):
        """
        Initialize pipeline.

        Args:
            config: Headroom configuration.
            transforms: Optional custom transform list (overrides config).
            provider: Provider for model-specific behavior.
        """
        self.config = config or HeadroomConfig()
        self._provider = provider

        if transforms is not None:
            self.transforms = transforms
        else:
            self.transforms = self._build_default_transforms()

        # Circuit breaker: after N consecutive pipeline
        # failures, pass messages through untouched for a cooldown window
        # instead of re-running (and re-failing) transforms on every
        # request. Threshold <= 0 disables the breaker.
        self._breaker_threshold = _breaker_env("HEADROOM_PIPELINE_BREAKER_THRESHOLD", 3, int)
        self._breaker_cooldown_s = _breaker_env("HEADROOM_PIPELINE_BREAKER_COOLDOWN_S", 60.0, float)
        self._breaker_lock = threading.Lock()
        self._breaker_failures = 0
        self._breaker_open_until = 0.0

    def _build_default_transforms(self) -> list[Transform]:
        """Build default transform pipeline from config."""
        transforms: list[Transform] = []

        # Order matters!

        # 1. Cache Aligner (prefix stabilization)
        if self.config.cache_aligner.enabled:
            transforms.append(CacheAligner(self.config.cache_aligner))

        # 1b. Cross-message dedup — elide later byte-identical tool outputs
        # (recoverable via <<ccr:HASH>>) BEFORE per-message compression so
        # duplicates don't pay the compression price per copy. Earlier
        # messages are never modified, so the cached prefix stays stable.
        if getattr(self.config, "cross_message_dedup_enabled", True):
            transforms.append(CrossMessageDeduper())

        # 2. Content-aware Compression
        # ContentRouter handles ALL content types intelligently:
        # - JSON arrays -> SmartCrusher
        # - Plain text -> passthrough (reversible CCR offload for large
        #   uncompressible content)
        # - Code -> passthrough (ships unmangled; AST compressor retired)
        # - Logs -> LogCompressor
        # - Search results -> SearchCompressor
        # - HTML -> HTMLExtractor
        transforms.append(ContentRouter())
        logger.info("Pipeline using ContentRouter for intelligent content-aware compression")

        return transforms

    def _get_tokenizer(self, model: str) -> Tokenizer:
        """Get tokenizer for model.

        Uses provider's tokenizer if available, otherwise falls back to
        the tokenizer registry which auto-detects the best backend per model:
        - OpenAI models: tiktoken (exact)
        - Anthropic models: calibrated estimation (~3.5 chars/token)
        - Open models: HuggingFace tokenizer (if installed)
        - Unknown models: character-based estimation
        """
        if self._provider is not None:
            token_counter = self._provider.get_token_counter(model)
            return Tokenizer(token_counter, model)

        # No provider — use the tokenizer registry (auto-detects per model)
        # TokenCounter from tokenizers and providers have the same interface
        # (count_text, count_messages) but are different Protocol types.
        from headroom.tokenizers import get_tokenizer

        return Tokenizer(get_tokenizer(model), model)  # type: ignore[arg-type]

    def _breaker_is_open(self) -> bool:
        """True while the circuit breaker cooldown window is active."""
        if self._breaker_threshold <= 0:
            return False
        with self._breaker_lock:
            return time.monotonic() < self._breaker_open_until

    def _breaker_record_failure(self) -> None:
        """Count a pipeline failure; open the breaker at the threshold."""
        if self._breaker_threshold <= 0:
            return
        with self._breaker_lock:
            self._breaker_failures += 1
            if self._breaker_failures >= self._breaker_threshold:
                self._breaker_open_until = time.monotonic() + self._breaker_cooldown_s
                self._breaker_failures = 0
                logger.warning(
                    "Pipeline circuit breaker OPEN after %d consecutive failures; "
                    "passing messages through for %.0fs",
                    self._breaker_threshold,
                    self._breaker_cooldown_s,
                )

    def _breaker_record_success(self) -> None:
        """Reset the consecutive-failure count after a clean run."""
        if self._breaker_threshold <= 0:
            return
        with self._breaker_lock:
            self._breaker_failures = 0

    def apply(
        self,
        messages: list[dict[str, Any]],
        model: str,
        **kwargs: Any,
    ) -> TransformResult:
        """
        Apply all transforms in sequence.

        Args:
            messages: List of messages to transform.
            model: Model name for token counting.
            **kwargs: Additional arguments passed to transforms.
                - model_limit: Context limit override.
                - output_buffer: Output buffer override.
                - tool_profiles: Per-tool compression profiles.
                - request_id: Optional request ID for diff artifact.

        Returns:
            Combined TransformResult.
        """
        # Consume the dry-run marker so it never reaches the transforms.
        # simulate() calls apply(record_metrics=False); the pop keeps that flag
        # out of **kwargs (which is forwarded to should_apply / transform.apply
        # below), so simulate() and apply() drive the transforms identically.
        kwargs.pop("record_metrics", True)

        # Build the typed per-request seam ONCE, here at the single boundary
        # every caller crosses — both compress() (which forwards
        # CompressConfig.min_tokens_to_compress) and direct
        # TransformPipeline.apply(**kwargs) callers (who omit it). Built from the
        # loose kwargs bag with ONE unified default (250), so direct callers and
        # compress() callers agree; previously direct callers silently got 50.
        # Placed back into kwargs under "compress_request" so the existing
        # should_apply / transform.apply forwarding threads it explicitly to
        # transforms (ContentRouter reads min_tokens from it), while the public
        # **kwargs entry surface stays unchanged.
        kwargs["compress_request"] = CompressRequest.from_kwargs(kwargs)

        tokenizer = self._get_tokenizer(model)

        # Get model limit from kwargs (should be set by client)
        model_limit = kwargs.get("model_limit")
        if model_limit is None:
            raise ValueError(
                "model_limit is required. Provide it via kwargs or "
                "configure model_context_limits in HeadroomClient."
            )

        # Start with original tokens
        # Circuit breaker open — pass through untouched.
        if self._breaker_is_open():
            passthrough_tokens = tokenizer.count_messages(messages)
            return TransformResult(
                messages=messages,
                tokens_before=passthrough_tokens,
                tokens_after=passthrough_tokens,
                transforms_applied=["pipeline:circuit_open"],
            )

        t_count = time.perf_counter()
        tokens_before = tokenizer.count_messages(messages)
        count_ms = (time.perf_counter() - t_count) * 1000

        logger.debug(
            "Pipeline starting: %d messages, %d tokens, model=%s",
            len(messages),
            tokens_before,
            model,
        )

        # Track all transforms applied
        all_transforms: list[str] = []
        all_markers: list[str] = []
        all_warnings: list[str] = []
        all_timing: dict[str, float] = {}  # transform_name → ms

        # Track transform diffs if enabled
        transform_diffs: list[TransformDiff] = []
        generate_diff = self.config.generate_diff_artifact

        t_copy = time.perf_counter()
        current_messages = deep_copy_messages(messages)
        copy_ms = (time.perf_counter() - t_copy) * 1000

        all_timing["_deep_copy"] = copy_ms
        all_timing["_initial_token_count"] = count_ms

        pipeline_start = time.perf_counter()

        request_id = kwargs.get("request_id", "")
        log_prefix = f"[{request_id}] " if request_id else ""

        frozen_count = kwargs.get("frozen_message_count", 0)
        if frozen_count > 0:
            logger.info(
                "%sPipeline: freezing first %d/%d messages (prefix cached by provider)",
                log_prefix,
                frozen_count,
                len(messages),
            )

        for transform in self.transforms:
            # Check if transform should run
            if not transform.should_apply(current_messages, tokenizer, **kwargs):
                continue

            # Time the transform
            t0 = time.perf_counter()
            try:
                result = transform.apply(current_messages, tokenizer, **kwargs)
            except Exception:
                self._breaker_record_failure()
                raise
            duration_ms = (time.perf_counter() - t0) * 1000

            # Update messages for next transform
            current_messages = result.messages

            # Use token counts reported by the transform itself — avoids
            # redundant O(N) recount of the full message list after each step.
            tokens_before_transform = result.tokens_before
            tokens_after_transform = result.tokens_after

            # Accumulate results
            all_transforms.extend(result.transforms_applied)
            all_markers.extend(result.markers_inserted)
            all_warnings.extend(result.warnings)
            all_timing[transform.name] = duration_ms

            # Merge sub-transform timing (e.g. ContentRouter's per-compressor breakdown)
            if result.timing:
                all_timing.update(result.timing)

            # Log transform results
            if result.transforms_applied:
                logger.info(
                    "Transform %s: %d -> %d tokens (saved %d) [%.1fms]",
                    transform.name,
                    tokens_before_transform,
                    tokens_after_transform,
                    tokens_before_transform - tokens_after_transform,
                    duration_ms,
                )
            else:
                logger.debug("Transform %s: no changes [%.1fms]", transform.name, duration_ms)

            # Record diff if enabled
            if generate_diff:
                transform_diffs.append(
                    TransformDiff(
                        transform_name=transform.name,
                        tokens_before=tokens_before_transform,
                        tokens_after=tokens_after_transform,
                        tokens_saved=tokens_before_transform - tokens_after_transform,
                        details=", ".join(result.transforms_applied)
                        if result.transforms_applied
                        else "",
                        duration_ms=duration_ms,
                    )
                )

        # All transforms ran without raising — reset the breaker.
        self._breaker_record_success()

        # Single final token count — the only full recount in the pipeline.
        # Earlier per-transform counts come from each transform's own result.
        t_final_count = time.perf_counter()
        tokens_after = tokenizer.count_messages(current_messages)
        all_timing["_final_token_count"] = (time.perf_counter() - t_final_count) * 1000

        pipeline_ms = (time.perf_counter() - pipeline_start) * 1000
        all_timing["pipeline_total"] = pipeline_ms

        # Log pipeline summary
        total_saved = tokens_before - tokens_after
        timing_parts = " ".join(f"{k}={v:.0f}ms" for k, v in all_timing.items())
        if total_saved > 0:
            logger.info(
                "%sPipeline complete: %d -> %d tokens (saved %d, %.1f%% reduction) [%s]",
                log_prefix,
                tokens_before,
                tokens_after,
                total_saved,
                (total_saved / tokens_before * 100) if tokens_before > 0 else 0,
                timing_parts,
            )
        else:
            logger.debug("%sPipeline complete: no token savings [%s]", log_prefix, timing_parts)

        # Build diff artifact if enabled
        diff_artifact = None
        if generate_diff:
            diff_artifact = DiffArtifact(
                request_id=kwargs.get("request_id", ""),
                original_tokens=tokens_before,
                optimized_tokens=tokens_after,
                total_tokens_saved=tokens_before - tokens_after,
                transforms=transform_diffs,
            )

        # Detect waste signals in original messages (only when significant compression)
        waste_signals: WasteSignals | None = None
        if tokens_before > tokens_after and (tokens_before - tokens_after) > 100:
            try:
                from ..parser import parse_messages

                _, _, waste_signals = parse_messages(messages, tokenizer)
                if waste_signals.total() == 0:
                    waste_signals = None
            except Exception:
                # Best-effort diagnostics only — never block the pipeline,
                # but never swallow silently either.
                logger.debug("Waste-signal detection failed (non-fatal)", exc_info=True)

        return TransformResult(
            messages=current_messages,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            transforms_applied=all_transforms,
            markers_inserted=all_markers,
            warnings=all_warnings,
            diff_artifact=diff_artifact,
            timing=all_timing,
            waste_signals=waste_signals,
        )

    def simulate(
        self,
        messages: list[dict[str, Any]],
        model: str,
        **kwargs: Any,
    ) -> TransformResult:
        """
        Simulate transforms without modifying messages.

        Same as apply() but returns what WOULD happen.

        Args:
            messages: List of messages.
            model: Model name.
            **kwargs: Additional arguments.

        Returns:
            TransformResult with simulated changes.
        """
        # apply() already works on a copy, so this is safe
        return self.apply(messages, model, record_metrics=False, **kwargs)
