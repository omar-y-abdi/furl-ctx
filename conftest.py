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
import os
from collections.abc import Iterator

import pytest

_UNBUILT_EXTENSION_MESSAGE = (
    "furl_ctx._core (the native Rust extension) is not importable from this "
    "checkout: the local furl_ctx/ sources shadow any installed furl-ctx "
    "wheel, and the extension is not built here.\n"
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


@pytest.fixture(autouse=True)
def _furl_ccr_env_snapshot() -> Iterator[None]:
    """Snapshot & restore ``FURL_CCR_*`` env around every test (suite hygiene).

    Product entrypoints legitimately mutate the PROCESS environment via
    ``os.environ.setdefault`` — ``cli.main()`` pins FURL_CCR_BACKEND and
    FURL_CCR_TTL_SECONDS, and the plugin hook module does the same at import
    time. An in-process test that exercises them would otherwise leak those
    defaults into every later test in the process (suite-order coupling: an
    env-sensitive test then passes or fails depending on which files ran
    before it).

    Snapshot/restore, NOT a blanket delenv: per-test ``monkeypatch.setenv``
    keeps working (monkeypatch is set up after and torn down before this
    outermost fixture), and any ambient FURL_CCR_* a developer exported on
    purpose survives for tests that never touch it. Tests that assert an
    env-UNSET contract still delenv for themselves — this fixture guarantees
    a clean slate BETWEEN tests, not an empty one.
    """
    saved = {key: value for key, value in os.environ.items() if key.startswith("FURL_CCR_")}
    yield
    for key in [key for key in os.environ if key.startswith("FURL_CCR_")]:
        if key not in saved:
            del os.environ[key]
    os.environ.update(saved)
