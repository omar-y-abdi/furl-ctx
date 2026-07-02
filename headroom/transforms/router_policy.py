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

Dependency-light by design: imports only ``ContentType`` from
``content_detector``; never imports ``content_router``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from .content_detector import ContentType


class CompressionStrategy(Enum):
    """Available compression strategies."""

    CODE_AWARE = "code_aware"
    SMART_CRUSHER = "smart_crusher"
    SEARCH = "search"
    LOG = "log"
    TEXT = "text"
    DIFF = "diff"
    HTML = "html"
    MIXED = "mixed"
    PASSTHROUGH = "passthrough"
    # Reversible last-resort offload to the CCR store (ContentRouter fallback).
    CCR_OFFLOAD = "ccr_offload"


def strategy_from_detection(config: Any, detection: Any) -> CompressionStrategy:
    """Get strategy from content detection result.

    Args:
        config: ContentRouterConfig providing ``fallback_strategy`` and
            ``prefer_code_aware_for_code``.
        detection: Result from detect_content_type.

    Returns:
        Selected strategy.
    """
    mapping = {
        ContentType.SOURCE_CODE: CompressionStrategy.CODE_AWARE,
        ContentType.JSON_ARRAY: CompressionStrategy.SMART_CRUSHER,
        ContentType.SEARCH_RESULTS: CompressionStrategy.SEARCH,
        ContentType.BUILD_OUTPUT: CompressionStrategy.LOG,
        ContentType.GIT_DIFF: CompressionStrategy.DIFF,
        ContentType.HTML: CompressionStrategy.HTML,
        ContentType.PLAIN_TEXT: CompressionStrategy.TEXT,
    }

    strategy: CompressionStrategy = mapping.get(detection.content_type, config.fallback_strategy)

    # Override: unless CodeAware is explicitly preferred, source code ships
    # unmangled. (The retired ML text compressor used to take this arm; its
    # not-installed behavior was a passthrough, preserved here explicitly.)
    if strategy == CompressionStrategy.CODE_AWARE and not config.prefer_code_aware_for_code:
        strategy = CompressionStrategy.PASSTHROUGH

    return strategy


def strategy_from_detection_type(config: Any, content_type: ContentType) -> CompressionStrategy:
    """Get strategy from ContentType enum."""
    mapping = {
        ContentType.SOURCE_CODE: CompressionStrategy.CODE_AWARE,
        ContentType.JSON_ARRAY: CompressionStrategy.SMART_CRUSHER,
        ContentType.SEARCH_RESULTS: CompressionStrategy.SEARCH,
        ContentType.BUILD_OUTPUT: CompressionStrategy.LOG,
        ContentType.GIT_DIFF: CompressionStrategy.DIFF,
        ContentType.HTML: CompressionStrategy.HTML,
        ContentType.PLAIN_TEXT: CompressionStrategy.TEXT,
    }
    return mapping.get(content_type, config.fallback_strategy)


def content_type_from_strategy(strategy: CompressionStrategy) -> ContentType:
    """Get ContentType from strategy."""
    mapping = {
        CompressionStrategy.CODE_AWARE: ContentType.SOURCE_CODE,
        CompressionStrategy.SMART_CRUSHER: ContentType.JSON_ARRAY,
        CompressionStrategy.SEARCH: ContentType.SEARCH_RESULTS,
        CompressionStrategy.LOG: ContentType.BUILD_OUTPUT,
        CompressionStrategy.DIFF: ContentType.GIT_DIFF,
        CompressionStrategy.HTML: ContentType.HTML,
        CompressionStrategy.TEXT: ContentType.PLAIN_TEXT,
        CompressionStrategy.PASSTHROUGH: ContentType.PLAIN_TEXT,
        CompressionStrategy.CCR_OFFLOAD: ContentType.PLAIN_TEXT,
    }
    return mapping.get(strategy, ContentType.PLAIN_TEXT)


def adaptive_min_ratio(config: Any, context_pressure: float) -> float:
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
