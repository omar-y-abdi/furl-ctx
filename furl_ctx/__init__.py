"""
Furl - The context compression layer for AI agents.

60-95% fewer tokens on redundant workloads, reversible via CCR.

Furl provides:
- Smart compression of tool outputs (keeps errors, anomalies, relevant items)
- Cache-aligned prefix stability warnings for better provider cache hits
- BM25 relevance scoring for content selection
- Reversible CCR offload: dropped content stays retrievable by hash

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

Error Handling — the real contract:

    ``compress()`` is FAIL-OPEN for content and pipeline failures: it never
    raises for them. On failure it returns the ORIGINAL messages unchanged
    with ``result.error`` set (a string describing the swallowed failure)
    — check it to tell a failed run from a genuine "nothing to do"
    (``error is None``). Non-fatal problems land in ``result.warnings``.
    The only exception raised for bad usage is ``TypeError``, for unknown
    keyword arguments (e.g. a typo'd config field):

    result = compress(messages)
    if result.error is not None:
        log.warning("compression failed open: %s", result.error)

    ``FurlError`` is exported as the reserved base class for future typed
    errors; no current API raises it.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .compress import CompressConfig, CompressResult, compress
from .compress_to import compress_to

# Keep a real callable bound for the one-function compression API so
# `from furl_ctx import compress` is never shadowed by the submodule object.
# ``compress_to`` is bound the same way (its submodule would otherwise shadow it).

__all__ = [
    # Exceptions — the reserved base class only. The eight subclasses this
    # package used to export were raised NOWHERE (decorative API) and were
    # removed in the API-1 prune; compress() fails open (``result.error``)
    # and raises TypeError for unknown kwargs.
    "FurlError",
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
    "compress_to",
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
