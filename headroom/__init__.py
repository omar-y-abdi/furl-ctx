"""
Headroom - The Context Compression Layer for LLM Applications.

Cut your LLM token usage by 50-90% without losing accuracy.

Headroom provides:
- Smart compression of tool outputs (keeps errors, anomalies, relevant items)
- Cache-aligned prefix optimization for better provider cache hits
- BM25 / embedding relevance scoring for content selection
- Deterministic, byte-stable transforms with zero accuracy loss

Quick Start:

    from headroom import compress

    messages = [
        {"role": "user", "content": "Summarize these logs..."},
        {"role": "tool", "content": "<10k lines of log output>"},
    ]

    result = compress(messages)
    print(f"Tokens saved: {result.tokens_saved}")
    print(f"Compression ratio: {result.compression_ratio:.2f}")
    compressed_messages = result.messages

Configuration:

    from headroom import compress, CompressConfig

    config = CompressConfig(...)
    result = compress(messages, config=config)

Error Handling:

    from headroom import HeadroomError, ConfigurationError

    try:
        result = compress(messages)
    except ConfigurationError as e:
        print(f"Config issue: {e.details}")
    except HeadroomError as e:
        print(f"Headroom error: {e}")
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from ._version import __version__  # noqa: F401
from .compress import CompressConfig, CompressResult, compress

# Keep a real callable bound for the one-function compression API so
# `from headroom import compress` is never shadowed by the submodule object.

__all__ = [
    # Exceptions
    "HeadroomError",
    "ConfigurationError",
    "ProviderError",
    "StorageError",
    "CompressionError",
    "TokenizationError",
    "CacheError",
    "ValidationError",
    "TransformError",
    # Config
    "HeadroomConfig",
    "SmartCrusherConfig",
    "CacheAlignerConfig",
    "RelevanceScorerConfig",
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
    "CANONICAL_PIPELINE_STAGES",
]

# Keep package-level imports lightweight so `import headroom` does not eagerly
# load provider SDKs, ML stacks, or optional proxy/runtime integrations.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # Exceptions
    "HeadroomError": ("headroom.exceptions", "HeadroomError"),
    "ConfigurationError": ("headroom.exceptions", "ConfigurationError"),
    "ProviderError": ("headroom.exceptions", "ProviderError"),
    "StorageError": ("headroom.exceptions", "StorageError"),
    "CompressionError": ("headroom.exceptions", "CompressionError"),
    "TokenizationError": ("headroom.exceptions", "TokenizationError"),
    "CacheError": ("headroom.exceptions", "CacheError"),
    "ValidationError": ("headroom.exceptions", "ValidationError"),
    "TransformError": ("headroom.exceptions", "TransformError"),
    # Config
    "HeadroomConfig": ("headroom.config", "HeadroomConfig"),
    "SmartCrusherConfig": ("headroom.config", "SmartCrusherConfig"),
    "CacheAlignerConfig": ("headroom.config", "CacheAlignerConfig"),
    "RelevanceScorerConfig": ("headroom.config", "RelevanceScorerConfig"),
    # Data models
    "Block": ("headroom.config", "Block"),
    "CachePrefixMetrics": ("headroom.config", "CachePrefixMetrics"),
    "DiffArtifact": ("headroom.config", "DiffArtifact"),
    "TransformDiff": ("headroom.config", "TransformDiff"),
    "TransformResult": ("headroom.config", "TransformResult"),
    "WasteSignals": ("headroom.config", "WasteSignals"),
    # Transforms
    "SmartCrusher": ("headroom.transforms", "SmartCrusher"),
    "CacheAligner": ("headroom.transforms", "CacheAligner"),
    "TransformPipeline": ("headroom.transforms", "TransformPipeline"),
    # Cache config types
    "CacheConfig": ("headroom.cache", "CacheConfig"),
    "CacheStrategy": ("headroom.cache", "CacheStrategy"),
    # Relevance scoring
    "RelevanceScore": ("headroom.relevance", "RelevanceScore"),
    "RelevanceScorer": ("headroom.relevance", "RelevanceScorer"),
    "BM25Scorer": ("headroom.relevance", "BM25Scorer"),
    # Utilities
    "Tokenizer": ("headroom.tokenizer", "Tokenizer"),
    "count_tokens_text": ("headroom.tokenizer", "count_tokens_text"),
    "count_tokens_messages": ("headroom.tokenizer", "count_tokens_messages"),
    # One-function API
    "compress": ("headroom.compress", "compress"),
    # Hooks
    "CompressionHooks": ("headroom.hooks", "CompressionHooks"),
    "CompressContext": ("headroom.hooks", "CompressContext"),
    "CompressEvent": ("headroom.hooks", "CompressEvent"),
    # Canonical pipeline
    "PipelineStage": ("headroom.pipeline", "PipelineStage"),
    "PipelineEvent": ("headroom.pipeline", "PipelineEvent"),
    "PipelineExtensionManager": ("headroom.pipeline", "PipelineExtensionManager"),
    "CANONICAL_PIPELINE_STAGES": ("headroom.pipeline", "CANONICAL_PIPELINE_STAGES"),
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
    return sorted(set(globals()) | set(__all__))
