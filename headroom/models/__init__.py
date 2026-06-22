"""Shared ML model helpers retained for the compression core.

The LLM metadata/pricing registry was removed in the compression-only
amputation. This package now exposes only:

- :data:`headroom.models.config.ML_MODEL_DEFAULTS` — shared ML defaults used by
  config and cache modules.
- :class:`headroom.models.ml_models.MLModelRegistry` and its accessors for
  sharing heavy model instances (sentence transformers, spaCy) so the
  same model is not loaded multiple times across the process.
"""

from __future__ import annotations

from importlib import import_module

__all__ = [
    # ML Model Registry
    "MLModelRegistry",
    "get_sentence_transformer",
    "get_spacy",
]

# Keep the package entrypoint lightweight so importing headroom.models does
# not eagerly load optional ML dependencies until a specific export is used.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # ML model registry
    "MLModelRegistry": ("headroom.models.ml_models", "MLModelRegistry"),
    "get_sentence_transformer": ("headroom.models.ml_models", "get_sentence_transformer"),
    "get_spacy": ("headroom.models.ml_models", "get_spacy"),
}


def __getattr__(name: str) -> object:
    """Resolve model exports lazily while preserving package imports."""
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
