"""Root pytest configuration for the Furl repository.

Guards against the most common first-contact failure: running ``pytest`` in a
fresh clone before the native Rust extension (``furl_ctx._core``) has been
built. Without this guard, collection explodes into dozens of identical
``ModuleNotFoundError: No module named 'furl_ctx._core'`` tracebacks — a
cryptic wall for a first-time reader. Here we detect the unbuilt extension up
front and stop the session with a single actionable message.

In a built environment (CI builds before testing; local dev builds via
``maturin develop``) the extension resolves and this hook is a no-op.
"""

from __future__ import annotations

import importlib.util

import pytest

_UNBUILT_EXTENSION_MESSAGE = (
    "furl_ctx._core (the native Rust extension) is not built, so the test "
    "suite cannot import furl_ctx.\n"
    "Build the extension first, then re-run pytest:\n\n"
    "    pip install maturin && maturin develop --release\n\n"
    "See CONTRIBUTING.md ('Development setup') for the full quickstart."
)


def pytest_configure(config: pytest.Config) -> None:
    """Abort collection early with one clear message when _core is unbuilt."""
    try:
        spec = importlib.util.find_spec("furl_ctx._core")
    except ModuleNotFoundError as exc:
        # ``find_spec`` imports the parent package to read its ``__path__``. If a
        # future refactor makes ``import furl_ctx`` hard-import the extension,
        # treat that as the same unbuilt case; surface anything else unchanged.
        if exc.name != "furl_ctx._core":
            raise
        spec = None
    if spec is None:
        pytest.exit(_UNBUILT_EXTENSION_MESSAGE, returncode=1)
