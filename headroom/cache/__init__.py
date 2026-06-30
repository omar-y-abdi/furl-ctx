"""Headroom Cache module.

Exposes the shared ``CacheConfig`` / ``CacheStrategy`` types. The
provider-specific cache optimizers were retired with the public SDK surface.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Expose concrete types to static analysis while keeping runtime imports lazy.
    from headroom.cache.base import (  # noqa: F401
        CacheConfig,
        CacheStrategy,
    )

__all__ = [
    # Base types
    "CacheConfig",
    "CacheStrategy",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # Base types
    "CacheConfig": ("headroom.cache.base", "CacheConfig"),
    "CacheStrategy": ("headroom.cache.base", "CacheStrategy"),
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
