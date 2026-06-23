"""Headroom Cache module.

Exposes the dynamic-content detection types (``DynamicContentDetector`` and
friends) plus the shared ``CacheConfig`` / ``CacheStrategy`` types. The
provider-specific cache optimizers were retired with the public SDK surface.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Expose concrete types to static analysis while keeping runtime imports lazy.
    from headroom.cache.base import (  # noqa: F401
        BaseCacheOptimizer,
        CacheBreakpoint,
        CacheConfig,
        CacheMetrics,
        CacheOptimizer,
        CacheResult,
        CacheStrategy,
        OptimizationContext,
    )
    from headroom.cache.dynamic_detector import (  # noqa: F401
        DetectorConfig,
        DynamicCategory,
        DynamicContentDetector,
        DynamicSpan,
        detect_dynamic_content,
    )

__all__ = [
    # Base types
    "BaseCacheOptimizer",
    "CacheBreakpoint",
    "CacheConfig",
    "CacheMetrics",
    "CacheOptimizer",
    "CacheResult",
    "CacheStrategy",
    "OptimizationContext",
    # Dynamic content detection
    "DetectorConfig",
    "DynamicCategory",
    "DynamicContentDetector",
    "DynamicSpan",
    "detect_dynamic_content",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # Base types
    "BaseCacheOptimizer": ("headroom.cache.base", "BaseCacheOptimizer"),
    "CacheBreakpoint": ("headroom.cache.base", "CacheBreakpoint"),
    "CacheConfig": ("headroom.cache.base", "CacheConfig"),
    "CacheMetrics": ("headroom.cache.base", "CacheMetrics"),
    "CacheOptimizer": ("headroom.cache.base", "CacheOptimizer"),
    "CacheResult": ("headroom.cache.base", "CacheResult"),
    "CacheStrategy": ("headroom.cache.base", "CacheStrategy"),
    "OptimizationContext": ("headroom.cache.base", "OptimizationContext"),
    # Dynamic content detection
    "DetectorConfig": ("headroom.cache.dynamic_detector", "DetectorConfig"),
    "DynamicCategory": ("headroom.cache.dynamic_detector", "DynamicCategory"),
    "DynamicContentDetector": ("headroom.cache.dynamic_detector", "DynamicContentDetector"),
    "DynamicSpan": ("headroom.cache.dynamic_detector", "DynamicSpan"),
    "detect_dynamic_content": ("headroom.cache.dynamic_detector", "detect_dynamic_content"),
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
