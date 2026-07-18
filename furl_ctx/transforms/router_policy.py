"""Pure routing-policy mappings for the ContentRouter.

Extracted from ``content_router.py``. Everything here is a pure function of
its arguments — config thresholds + a content/strategy value — with no access
to router runtime state, the result cache, or thread-locals.

``CompressionStrategy`` lives here (rather than in ``content_router``) so the
strategy-mapping functions, whose dict keys/values ARE ``CompressionStrategy``
members, can be defined without importing back from ``content_router`` — that
would form an import cycle. ``content_router`` re-exports the enum, so existing
``from ...content_router import CompressionStrategy`` imports and the package
lazy-export both keep resolving the single canonical object.

Dependency-light by design: imports only ``ContentType`` /
``DetectionResult`` from ``content_detector``; never imports
``content_router``. The config parameters are therefore typed as narrow
PROTOCOLS of exactly the fields each policy function reads (TYPE-3) —
``ContentRouterConfig`` satisfies them structurally without this module
importing it.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Protocol

from .content_detector import ContentType, DetectionResult


class CompressionStrategy(Enum):
    """Available compression strategies."""

    SMART_CRUSHER = "smart_crusher"
    SEARCH = "search"
    LOG = "log"
    TEXT = "text"
    DIFF = "diff"
    # Opt-in AST code compression (Engine P2-12, `enable_code_aware`).
    CODE_AWARE = "code_aware"
    MIXED = "mixed"
    PASSTHROUGH = "passthrough"
    # Reversible last-resort offload to the CCR store (ContentRouter fallback).
    CCR_OFFLOAD = "ccr_offload"


class StrategyPolicyConfig(Protocol):
    """The config fields the strategy-mapping policy reads."""

    fallback_strategy: CompressionStrategy
    enable_code_aware: bool


class RatioPolicyConfig(Protocol):
    """The config fields the adaptive-ratio policy reads."""

    min_ratio_relaxed: float
    min_ratio_aggressive: float


def strategy_from_detection(
    config: StrategyPolicyConfig, detection: DetectionResult
) -> CompressionStrategy:
    """Get strategy from content detection result.

    Args:
        config: ``ContentRouterConfig`` (or any object with the
            ``StrategyPolicyConfig`` fields).
        detection: Result from detect_content_type.

    Returns:
        Selected strategy.
    """
    mapping = {
        ContentType.SOURCE_CODE: _source_code_strategy(config),
        ContentType.JSON_ARRAY: CompressionStrategy.SMART_CRUSHER,
        ContentType.SEARCH_RESULTS: CompressionStrategy.SEARCH,
        ContentType.BUILD_OUTPUT: CompressionStrategy.LOG,
        ContentType.GIT_DIFF: CompressionStrategy.DIFF,
        ContentType.PLAIN_TEXT: CompressionStrategy.TEXT,
    }
    return mapping.get(detection.content_type, config.fallback_strategy)


def _source_code_strategy(config: StrategyPolicyConfig) -> CompressionStrategy:
    """SOURCE_CODE routing: PASSTHROUGH by default — code ships unmangled,
    exactly the behavior the retired AST/ML code compressors left behind.
    The opt-in CodeAwareCompressor (``enable_code_aware=True``, Engine
    P2-12) claims the arm instead; its dispatch arm applies the
    ``lossless_only`` gate."""
    if config.enable_code_aware:
        return CompressionStrategy.CODE_AWARE
    return CompressionStrategy.PASSTHROUGH


def strategy_from_detection_type(
    config: StrategyPolicyConfig, content_type: ContentType
) -> CompressionStrategy:
    """Get strategy from ContentType enum."""
    mapping = {
        ContentType.SOURCE_CODE: _source_code_strategy(config),
        ContentType.JSON_ARRAY: CompressionStrategy.SMART_CRUSHER,
        ContentType.SEARCH_RESULTS: CompressionStrategy.SEARCH,
        ContentType.BUILD_OUTPUT: CompressionStrategy.LOG,
        ContentType.GIT_DIFF: CompressionStrategy.DIFF,
        ContentType.PLAIN_TEXT: CompressionStrategy.TEXT,
    }
    return mapping.get(content_type, config.fallback_strategy)


def content_type_from_strategy(strategy: CompressionStrategy) -> ContentType:
    """Get ContentType from strategy."""
    mapping = {
        CompressionStrategy.SMART_CRUSHER: ContentType.JSON_ARRAY,
        CompressionStrategy.SEARCH: ContentType.SEARCH_RESULTS,
        CompressionStrategy.LOG: ContentType.BUILD_OUTPUT,
        CompressionStrategy.DIFF: ContentType.GIT_DIFF,
        CompressionStrategy.CODE_AWARE: ContentType.SOURCE_CODE,
        CompressionStrategy.TEXT: ContentType.PLAIN_TEXT,
        CompressionStrategy.PASSTHROUGH: ContentType.PLAIN_TEXT,
        CompressionStrategy.CCR_OFFLOAD: ContentType.PLAIN_TEXT,
    }
    return mapping.get(strategy, ContentType.PLAIN_TEXT)


def adaptive_min_ratio(config: RatioPolicyConfig, context_pressure: float) -> float:
    """Compression-acceptance threshold scaled by context pressure.

    A compression is accepted when ``ratio < min_ratio`` (lower ratio =
    more aggressive). A HIGHER ``min_ratio`` accepts more compressions.
    At low pressure use the relaxed (stricter, lower) threshold; at high
    pressure use the aggressive (permissive, higher) threshold, so the
    agent accepts marginal compressions exactly when context is tightest.
    Monotone non-decreasing in ``context_pressure``; clamped to
    ``[relaxed, aggressive]``.
    """
    relaxed: float = config.min_ratio_relaxed
    aggressive: float = config.min_ratio_aggressive
    min_ratio = relaxed + (aggressive - relaxed) * context_pressure
    return max(relaxed, min(aggressive, min_ratio))


# Route-count lanes that can coexist with an all-passthrough router result,
# paired with the machine-readable reason each contributes. Dominance is
# computed over ALL of these lanes — the highest count wins; ties fall to this
# declaration order, which is two-tiered on purpose: GRANULAR lanes first (each
# names the specific gate that stopped content), then the SHAPE/PINNING lanes
# the content-block walk books without a transform (``content_blocks`` /
# ``nested_blocks`` per container walked, ``cache_control_protected`` per
# pinned block). The latter fold into the umbrella ``no_eligible_content`` —
# true of any no-op by definition — so a specific explanation always beats the
# umbrella on equal counts. Counts are heuristic weights, not a partition:
# string-path lanes book per message, block-path lanes per block, and one
# message may book several lanes. Protection lanes (excluded_tool / user_msg /
# recent_code / analysis_ctx / error_protected / feedback_skip) are absent by
# construction — each books a ``router:protected:*``-style transform, so a
# protected message or block is never part of a bare no-op. Cache bookkeeping
# (cache_hit / cache_miss) is absent too: it records HOW a lane resolved, not
# WHY nothing shipped.
_NOOP_REASON_BY_COUNTER: tuple[tuple[str, str], ...] = (
    ("ratio_too_high", "no_savings"),
    ("small", "below_min_tokens"),
    ("net_mutation_gate", "net_mutation_gate"),
    ("non_string", "non_string"),
    ("already_compressed", "already_compressed"),
    ("content_blocks", "no_eligible_content"),
    ("nested_blocks", "no_eligible_content"),
    ("cache_control_protected", "no_eligible_content"),
)


def noop_transform(route_counts: Mapping[str, int]) -> str:
    """The self-explaining ``router:noop:{reason}`` transform label.

    ``run_router_passes`` books a router no-op only when NO transform —
    compression OR protection — fired for any message. ``route_counts`` still
    records WHY every message shipped through untouched (the lane each hit);
    collapse it to the single dominant reason so ``transforms_applied`` explains
    a 0% result instead of a bare, silent ``router:noop`` (the evaluator saw the
    bare form and read the tool as broken).

    The ``router:noop`` PREFIX is preserved, so ``summarize_routing_markers``
    and every ``startswith("router:")`` consumer stay byte-unaffected. Total: an
    all-zero or unrecognised ``route_counts`` (a fully-frozen prefix or empty
    input) falls through to ``router:noop:no_eligible_content`` — the same
    umbrella reason the transform-less shape/pinning lanes map to.
    """
    dominant = "no_eligible_content"
    best = 0
    for counter, reason in _NOOP_REASON_BY_COUNTER:
        count = route_counts.get(counter, 0)
        if count > best:
            best = count
            dominant = reason
    return f"router:noop:{dominant}"
