"""Lazy compressor registry for the content router.

Owns the five SELF-CONTAINED lazy compressor factories that the
:class:`~headroom.transforms.content_router.ContentRouter` dispatches to:
SmartCrusher, SearchCompressor, LogCompressor, DiffCompressor, and
HTMLExtractor.

Each factory is a plain lazy-init-and-cache: it reads only from the router's
``ContentRouterConfig`` and memoizes the constructed compressor in a private
slot. None of them touch the router's thread-local per-request runtime options
(``_runtime_*``), which is exactly why they extract cleanly out of the router
god-object without re-opening the #10 worker-options surface.

This registry is INTERNAL — it is not part of ``headroom.__all__``. The
``ContentRouter`` holds one instance (``self._registry``) and its public
``_get_*`` getters delegate to the matching ``get_*`` method here, so every
existing call site (including the fallback chain and ``eager_load_compressors``)
stays byte-unchanged.

The ML text compressors (Kompress / ``_get_kompress``) deliberately stay on the
router: ``_get_kompress`` reads ``self._runtime_kompress_model`` (thread-local),
so it is NOT self-contained and does not belong here.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class CompressorRegistry:
    """Lazy-init + cache for the five self-contained compressors.

    Construct with the router's :class:`ContentRouterConfig`; each ``get_*``
    method lazy-imports and instantiates its compressor on first call and
    caches it for subsequent calls. Missing optional dependencies are handled
    exactly as before (debug log + ``None`` return) so callers keep their
    graceful-skip behaviour.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self._smart_crusher: Any = None
        self._search_compressor: Any = None
        self._log_compressor: Any = None
        self._diff_compressor: Any = None
        self._html_extractor: Any = None

    def get_smart_crusher(self) -> Any:
        """Get SmartCrusher (lazy load) with CCR config."""
        if self._smart_crusher is None:
            try:
                from ..config import CCRConfig
                from .smart_crusher import SmartCrusher, SmartCrusherConfig

                # Pass CCR config for marker injection
                ccr_config = CCRConfig(
                    enabled=self.config.ccr_enabled,
                    inject_retrieval_marker=self.config.ccr_inject_marker,
                )
                crusher_config = SmartCrusherConfig()
                if self.config.smart_crusher_max_items_after_crush is not None:
                    crusher_config.max_items_after_crush = (
                        self.config.smart_crusher_max_items_after_crush
                    )
                crusher_config.routing_policy = self.config.smart_crusher_routing_policy
                self._smart_crusher = SmartCrusher(
                    config=crusher_config,
                    ccr_config=ccr_config,
                    with_compaction=self.config.smart_crusher_with_compaction,
                )
            except ImportError:
                logger.debug("SmartCrusher not available")
        return self._smart_crusher

    def get_search_compressor(self) -> Any:
        """Get SearchCompressor (lazy load)."""
        if self._search_compressor is None:
            try:
                from .search_compressor import SearchCompressor

                self._search_compressor = SearchCompressor()
            except ImportError:
                logger.debug("SearchCompressor not available")
        return self._search_compressor

    def get_log_compressor(self) -> Any:
        """Get LogCompressor (lazy load)."""
        if self._log_compressor is None:
            try:
                from .log_compressor import LogCompressor

                self._log_compressor = LogCompressor()
            except ImportError:
                logger.debug("LogCompressor not available")
        return self._log_compressor

    def get_diff_compressor(self) -> Any:
        """Get DiffCompressor (lazy load). Rust-only — Python implementation
        retired in Stage 3b. The wheel (`headroom._core`) is a hard import.
        """
        if self._diff_compressor is None:
            from .diff_compressor import DiffCompressor

            self._diff_compressor = DiffCompressor()
        return self._diff_compressor

    def get_html_extractor(self) -> Any:
        """Get HTMLExtractor (lazy load)."""
        if self._html_extractor is None:
            try:
                from .html_extractor import HTMLExtractor

                self._html_extractor = HTMLExtractor()
            except ImportError:
                logger.debug("HTMLExtractor not available (install trafilatura)")
        return self._html_extractor
