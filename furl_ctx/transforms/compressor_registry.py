"""Lazy compressor registry for the content router.

Owns the six SELF-CONTAINED lazy compressor factories that the
:class:`~furl_ctx.transforms.content_router.ContentRouter` dispatches to:
SmartCrusher, SearchCompressor, LogCompressor, DiffCompressor,
TextCrusher, and CodeAwareCompressor.

Each factory is a plain lazy-init-and-cache: it reads only from the router's
``ContentRouterConfig`` and memoizes the constructed compressor in a private
slot.

This registry is INTERNAL — it is not part of ``furl_ctx.__all__``. The
``ContentRouter`` holds one instance (``self._registry``) and its public
``_get_*`` getters delegate to the matching ``get_*`` method here, so every
existing call site (including the fallback chain) stays byte-unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotation-only: the whole point of this registry is that compressor
    # modules are imported LAZILY inside each getter. These imports never
    # run at runtime (`from __future__ import annotations` keeps every
    # annotation a string); they exist so mypy checks the getter contracts
    # (TYPE-3) instead of trusting `Any`. The ContentRouterConfig import is
    # reverse-of-runtime (content_router imports this module), which is
    # cycle-safe under TYPE_CHECKING.
    from .code_aware_compressor import CodeAwareCompressor
    from .content_router import ContentRouterConfig
    from .diff_compressor import DiffCompressor
    from .log_compressor import LogCompressor
    from .search_compressor import SearchCompressor
    from .smart_crusher import SmartCrusher
    from .text_crusher import TextCrusher

logger = logging.getLogger(__name__)


class CompressorRegistry:
    """Lazy-init + cache for the six self-contained compressors.

    Construct with the router's :class:`ContentRouterConfig`; each ``get_*``
    method lazy-imports and instantiates its compressor on first call and
    caches it for subsequent calls. Missing optional dependencies are handled
    with a debug log + ``None`` return so callers keep their
    graceful-skip behaviour — the ``| None`` in a getter's return type marks
    exactly the getters with that failure mode (SmartCrusher / Search / Log);
    the Rust-backed hard imports (Diff / Text) and the pure-Python
    CodeAware compressor always construct.
    """

    def __init__(self, config: ContentRouterConfig) -> None:
        self.config = config
        self._smart_crusher: SmartCrusher | None = None
        self._search_compressor: SearchCompressor | None = None
        self._log_compressor: LogCompressor | None = None
        self._diff_compressor: DiffCompressor | None = None
        self._text_crusher: TextCrusher | None = None
        self._code_aware_compressor: CodeAwareCompressor | None = None

    def get_smart_crusher(self) -> SmartCrusher | None:
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
                # Strict lossless-or-passthrough mode: threaded through to
                # the Rust crusher's routing (lossy candidates never built).
                crusher_config.lossless_only = self.config.lossless_only
                self._smart_crusher = SmartCrusher(
                    config=crusher_config,
                    ccr_config=ccr_config,
                    with_compaction=self.config.smart_crusher_with_compaction,
                )
            except ImportError:
                logger.debug("SmartCrusher not available")
        return self._smart_crusher

    def get_search_compressor(self) -> SearchCompressor | None:
        """Get SearchCompressor (lazy load)."""
        if self._search_compressor is None:
            try:
                from .search_compressor import SearchCompressor

                self._search_compressor = SearchCompressor()
            except ImportError:
                logger.debug("SearchCompressor not available")
        return self._search_compressor

    def get_log_compressor(self) -> LogCompressor | None:
        """Get LogCompressor (lazy load)."""
        if self._log_compressor is None:
            try:
                from .log_compressor import LogCompressor

                self._log_compressor = LogCompressor()
            except ImportError:
                logger.debug("LogCompressor not available")
        return self._log_compressor

    def get_diff_compressor(self) -> DiffCompressor:
        """Get DiffCompressor (lazy load). Rust-only — the wheel
        (`furl_ctx._core`) is a hard import.
        """
        if self._diff_compressor is None:
            from .diff_compressor import DiffCompressor

            self._diff_compressor = DiffCompressor()
        return self._diff_compressor

    def get_text_crusher(self) -> TextCrusher:
        """Get TextCrusher (lazy load). Rust-only — the
        wheel (`furl_ctx._core`) is a hard import. Size floors and the
        CCR-or-passthrough discipline live in the compressor itself;
        `enable_text_crusher` / `lossless_only` gating happens in the
        dispatcher, matching the search/log arms.
        """
        if self._text_crusher is None:
            from .text_crusher import TextCrusher

            self._text_crusher = TextCrusher()
        return self._text_crusher

    def get_code_aware_compressor(self) -> CodeAwareCompressor:
        """Get CodeAwareCompressor (lazy load). Pure Python;
        the tree-sitter grammars are an OPTIONAL extra (`furl-ctx[code]`)
        imported lazily inside `compress()`, which fails open to
        passthrough when they are missing — so construction never gates
        on the dep. `enable_code_aware` / `lossless_only` gating happens
        in the dispatcher, matching the search/log/text arms.
        """
        if self._code_aware_compressor is None:
            from .code_aware_compressor import CodeAwareCompressor

            self._code_aware_compressor = CodeAwareCompressor()
        return self._code_aware_compressor
