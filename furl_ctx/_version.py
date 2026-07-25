"""Package version metadata — lazy, subprocess-free.

``__version__`` resolves through PEP 562 module ``__getattr__`` on FIRST
access, never at import: the old eager path spawned ``git tag`` + ``git
log`` subprocesses (~90 ms) on every ``import furl_ctx`` in a checkout and
made imports non-hermetic. The runtime version is
always the installed distribution metadata via ``importlib.metadata``, with
``"unknown"`` as the not-installed fallback.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

UNKNOWN_VERSION = "unknown"


def get_version() -> str:
    """Return Furl's runtime version from installed package metadata.

    Total: never raises — an uninstalled source tree (no ``furl-ctx``
    distribution visible) resolves to :data:`UNKNOWN_VERSION`.
    """
    try:
        return version("furl-ctx")
    except PackageNotFoundError:
        return UNKNOWN_VERSION


def __getattr__(name: str) -> str:
    """Resolve ``__version__`` lazily (PEP 562) and cache it in module
    globals so the metadata read happens at most once per process."""
    if name == "__version__":
        resolved = get_version()
        globals()["__version__"] = resolved
        return resolved
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), "__version__"})
