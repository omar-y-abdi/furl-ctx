"""
Furl - The Context Compression Layer for LLM Applications.

Cut your LLM token usage by 50-90% without losing accuracy.

Furl provides:
- Smart compression of tool outputs (keeps errors, anomalies, relevant items)
- Cache-aligned prefix optimization for better provider cache hits
- BM25 / embedding relevance scoring for content selection
- Deterministic, byte-stable transforms with zero accuracy loss

Quick Start:

    from furl_ctx import compress

    messages = [
        {"role": "user", "content": "Summarize these logs..."},
        {"role": "tool", "content": "<10k lines of log output>"},
    ]

    result = compress(messages)
    print(f"Tokens saved: {result.tokens_saved}")
    print(f"Compression ratio: {result.compression_ratio:.2f}")
    compressed_messages = result.messages

Configuration:

    from furl_ctx import compress, CompressConfig

    config = CompressConfig(...)
    result = compress(messages, config=config)

Error Handling:

    from furl_ctx import FurlError, ConfigurationError

    try:
        result = compress(messages)
    except ConfigurationError as e:
        print(f"Config issue: {e.details}")
    except FurlError as e:
        print(f"Furl error: {e}")
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .compress import CompressConfig, CompressResult, compress

# Keep a real callable bound for the one-function compression API so
# `from furl_ctx import compress` is never shadowed by the submodule object.

__all__ = [
    # Exceptions
    "FurlError",
    "ConfigurationError",
    "ProviderError",
    "StorageError",
    "CompressionError",
    "TokenizationError",
    "CacheError",
    "ValidationError",
    "TransformError",
    # Config
    "FurlConfig",
    "SmartCrusherConfig",
    "CacheAlignerConfig",
    # Data models
    "Block",
    "CachePrefixMetrics",
    "DiffArtifact",
    "TransformDiff",
    "TransformResult",
    "WasteSignals",
    # Transforms
    "SmartCrusher",
    "CacheAligner",
    "TransformPipeline",
    # Cache config types
    "CacheConfig",
    "CacheStrategy",
    # Relevance scoring - BM25 keyword scorer
    "RelevanceScore",
    "RelevanceScorer",
    "BM25Scorer",
    # Utilities
    "Tokenizer",
    "count_tokens_text",
    "count_tokens_messages",
    # One-function compression API
    "compress",
    "CompressConfig",
    "CompressResult",
    # Hooks
    "CompressionHooks",
    "CompressContext",
    "CompressEvent",
    # Canonical pipeline
    "PipelineStage",
    "PipelineEvent",
    "PipelineExtensionManager",
]

# Keep package-level imports lightweight so `import furl_ctx` does not eagerly
# load provider SDKs, ML stacks, or optional runtime integrations.
# ``__version__`` is lazy too (PERF-13): resolving it reads installed
# distribution metadata — never at import time, never via git subprocesses.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "__version__": ("furl_ctx._version", "__version__"),
    # Exceptions
    "FurlError": ("furl_ctx.exceptions", "FurlError"),
    "ConfigurationError": ("furl_ctx.exceptions", "ConfigurationError"),
    "ProviderError": ("furl_ctx.exceptions", "ProviderError"),
    "StorageError": ("furl_ctx.exceptions", "StorageError"),
    "CompressionError": ("furl_ctx.exceptions", "CompressionError"),
    "TokenizationError": ("furl_ctx.exceptions", "TokenizationError"),
    "CacheError": ("furl_ctx.exceptions", "CacheError"),
    "ValidationError": ("furl_ctx.exceptions", "ValidationError"),
    "TransformError": ("furl_ctx.exceptions", "TransformError"),
    # Config
    "FurlConfig": ("furl_ctx.config", "FurlConfig"),
    # API-14: the LIVE engine config class. The top-level export used to
    # point at a second, incompatible ``config.SmartCrusherConfig`` whose
    # own defaults crashed the engine (TypeError: unexpected keyword
    # argument 'relevance'); that class was deleted and this now names
    # the class ``SmartCrusher(config=...)`` actually accepts.
    "SmartCrusherConfig": ("furl_ctx.transforms.smart_crusher", "SmartCrusherConfig"),
    "CacheAlignerConfig": ("furl_ctx.config", "CacheAlignerConfig"),
    # Data models
    "Block": ("furl_ctx.config", "Block"),
    "CachePrefixMetrics": ("furl_ctx.config", "CachePrefixMetrics"),
    "DiffArtifact": ("furl_ctx.config", "DiffArtifact"),
    "TransformDiff": ("furl_ctx.config", "TransformDiff"),
    "TransformResult": ("furl_ctx.config", "TransformResult"),
    "WasteSignals": ("furl_ctx.config", "WasteSignals"),
    # Transforms
    "SmartCrusher": ("furl_ctx.transforms", "SmartCrusher"),
    "CacheAligner": ("furl_ctx.transforms", "CacheAligner"),
    "TransformPipeline": ("furl_ctx.transforms", "TransformPipeline"),
    # Cache config types
    "CacheConfig": ("furl_ctx.cache", "CacheConfig"),
    "CacheStrategy": ("furl_ctx.cache", "CacheStrategy"),
    # Relevance scoring
    "RelevanceScore": ("furl_ctx.relevance", "RelevanceScore"),
    "RelevanceScorer": ("furl_ctx.relevance", "RelevanceScorer"),
    "BM25Scorer": ("furl_ctx.relevance", "BM25Scorer"),
    # Utilities
    "Tokenizer": ("furl_ctx.tokenizer", "Tokenizer"),
    "count_tokens_text": ("furl_ctx.tokenizer", "count_tokens_text"),
    "count_tokens_messages": ("furl_ctx.tokenizer", "count_tokens_messages"),
    # One-function API
    "compress": ("furl_ctx.compress", "compress"),
    # Hooks
    "CompressionHooks": ("furl_ctx.hooks", "CompressionHooks"),
    "CompressContext": ("furl_ctx.hooks", "CompressContext"),
    "CompressEvent": ("furl_ctx.hooks", "CompressEvent"),
    # Canonical pipeline
    "PipelineStage": ("furl_ctx.pipeline", "PipelineStage"),
    "PipelineEvent": ("furl_ctx.pipeline", "PipelineEvent"),
    "PipelineExtensionManager": ("furl_ctx.pipeline", "PipelineExtensionManager"),
}


def __getattr__(name: str) -> Any:
    """Resolve package exports lazily while preserving legacy import paths."""
    module_attr = _LAZY_EXPORTS.get(name)
    if module_attr is not None:
        module_name, attr_name = module_attr
        value = getattr(import_module(module_name), attr_name)
        globals()[name] = value
        return value

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | {"__version__"})
