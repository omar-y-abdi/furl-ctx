"""Provider protocols retained for the compression core.

The concrete provider implementations and agent integrations were removed in
the compression-only amputation. This package now exposes only the abstract
protocols the compression library depends on:

- :class:`headroom.providers.base.TokenCounter` — token-counting protocol used
  by :mod:`headroom.tokenizer`.
- :class:`headroom.providers.base.Provider` — provider protocol referenced by
  the transform pipeline for typing.
"""

from __future__ import annotations

from importlib import import_module

__all__ = ["Provider", "TokenCounter"]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "Provider": ("headroom.providers.base", "Provider"),
    "TokenCounter": ("headroom.providers.base", "TokenCounter"),
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
