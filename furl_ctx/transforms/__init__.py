"""Transform modules for Furl SDK."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Expose concrete types to static analysis while keeping runtime imports lazy.
    from furl_ctx.transforms.base import Transform  # noqa: F401
    from furl_ctx.transforms.cache_aligner import CacheAligner  # noqa: F401
    from furl_ctx.transforms.content_detector import (  # noqa: F401
        ContentType,
        DetectionResult,
        detect_content_type,
    )
    from furl_ctx.transforms.content_router import (  # noqa: F401
        ContentRouter,
        ContentRouterConfig,
        RouterCompressionResult,
    )
    from furl_ctx.transforms.cross_message_dedup import CrossMessageDeduper  # noqa: F401
    from furl_ctx.transforms.diff_compressor import (  # noqa: F401
        DiffCompressionResult,
        DiffCompressor,
        DiffCompressorConfig,
    )
    from furl_ctx.transforms.log_compressor import (  # noqa: F401
        LogCompressionResult,
        LogCompressor,
        LogCompressorConfig,
    )
    from furl_ctx.transforms.pipeline import TransformPipeline  # noqa: F401
    from furl_ctx.transforms.router_policy import CompressionStrategy  # noqa: F401
    from furl_ctx.transforms.search_compressor import (  # noqa: F401
        SearchCompressionResult,
        SearchCompressor,
        SearchCompressorConfig,
    )
    from furl_ctx.transforms.smart_crusher import SmartCrusher, SmartCrusherConfig  # noqa: F401
    from furl_ctx.transforms.text_crusher import (  # noqa: F401
        TextCrusher,
        TextCrusherConfig,
        TextCrushResult,
    )

__all__ = [
    # Base
    "Transform",
    "TransformPipeline",
    # JSON compression
    "SmartCrusher",
    "SmartCrusherConfig",
    # Text compression (coding tasks)
    "ContentType",
    "DetectionResult",
    "detect_content_type",
    "SearchCompressor",
    "SearchCompressorConfig",
    "SearchCompressionResult",
    "LogCompressor",
    "LogCompressorConfig",
    "LogCompressionResult",
    "DiffCompressor",
    "DiffCompressorConfig",
    "DiffCompressionResult",
    "TextCrusher",
    "TextCrusherConfig",
    "TextCrushResult",
    # Content routing
    "ContentRouter",
    "ContentRouterConfig",
    "RouterCompressionResult",
    "CompressionStrategy",
    # Other transforms
    "CacheAligner",
    "CrossMessageDeduper",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # Base
    "Transform": ("furl_ctx.transforms.base", "Transform"),
    "TransformPipeline": ("furl_ctx.transforms.pipeline", "TransformPipeline"),
    # Anchor selection
    # JSON compression
    "SmartCrusher": ("furl_ctx.transforms.smart_crusher", "SmartCrusher"),
    "SmartCrusherConfig": ("furl_ctx.transforms.smart_crusher", "SmartCrusherConfig"),
    # Text compression (coding tasks)
    "ContentType": ("furl_ctx.transforms.content_detector", "ContentType"),
    "DetectionResult": ("furl_ctx.transforms.content_detector", "DetectionResult"),
    "detect_content_type": ("furl_ctx.transforms.content_detector", "detect_content_type"),
    "SearchCompressor": ("furl_ctx.transforms.search_compressor", "SearchCompressor"),
    "SearchCompressorConfig": (
        "furl_ctx.transforms.search_compressor",
        "SearchCompressorConfig",
    ),
    "SearchCompressionResult": (
        "furl_ctx.transforms.search_compressor",
        "SearchCompressionResult",
    ),
    "LogCompressor": ("furl_ctx.transforms.log_compressor", "LogCompressor"),
    "LogCompressorConfig": ("furl_ctx.transforms.log_compressor", "LogCompressorConfig"),
    "LogCompressionResult": ("furl_ctx.transforms.log_compressor", "LogCompressionResult"),
    "DiffCompressor": ("furl_ctx.transforms.diff_compressor", "DiffCompressor"),
    "DiffCompressorConfig": ("furl_ctx.transforms.diff_compressor", "DiffCompressorConfig"),
    "DiffCompressionResult": (
        "furl_ctx.transforms.diff_compressor",
        "DiffCompressionResult",
    ),
    "TextCrusher": ("furl_ctx.transforms.text_crusher", "TextCrusher"),
    "TextCrusherConfig": ("furl_ctx.transforms.text_crusher", "TextCrusherConfig"),
    "TextCrushResult": ("furl_ctx.transforms.text_crusher", "TextCrushResult"),
    # Content routing
    "ContentRouter": ("furl_ctx.transforms.content_router", "ContentRouter"),
    "ContentRouterConfig": ("furl_ctx.transforms.content_router", "ContentRouterConfig"),
    "RouterCompressionResult": (
        "furl_ctx.transforms.content_router",
        "RouterCompressionResult",
    ),
    # API-12: bind the enum to its 115-line OWNER (router_policy), not the
    # content_router facade — touching CompressionStrategy must not import
    # the whole router + Rust chain. content_router re-exports the same
    # object, so both import paths resolve to one canonical enum.
    "CompressionStrategy": ("furl_ctx.transforms.router_policy", "CompressionStrategy"),
    # Other transforms
    "CacheAligner": ("furl_ctx.transforms.cache_aligner", "CacheAligner"),
    "CrossMessageDeduper": (
        "furl_ctx.transforms.cross_message_dedup",
        "CrossMessageDeduper",
    ),
}


def __getattr__(name: str) -> object:
    if name == "__path__":
        raise AttributeError(name)

    try:
        module_name, attr_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
