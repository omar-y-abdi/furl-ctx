"""Shared pytest configuration for Furl tests.

The suite is fully offline and hermetic: the proxy-era network suites
(and their httpx.ReadTimeout skip-hook) and the proxy file-logging
fixture were removed with the proxy — an exception-to-skip hook can
only mask genuine bugs in an offline suite (TEST-13/TEST-34).
"""

# Defensive default, set before any imports: silences fork-parallelism warnings
# from third-party tokenizer libraries if one happens to be installed in the
# test environment (not a Furl dependency).
import os

os.environ["TOKENIZERS_PARALLELISM"] = "false"

from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def _furl_workspace_dir_sandbox(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Redirect the Furl workspace off the real ``~/.furl`` for the whole suite (F-E1).

    ``furl_ctx.paths.workspace_dir()`` defaults to ``~/.furl`` whenever
    ``FURL_WORKSPACE_DIR`` is unset, and that default is the root for every
    durable file the product writes: the global ``ccr.sqlite3``, every
    per-namespace ``ccr-ns-*.sqlite3``, and ``session_stats.jsonl``
    (``furl_ctx/paths.py``). Most tests already sandbox their own
    ``FURL_WORKSPACE_DIR`` via ``monkeypatch`` or an explicit subprocess env,
    but any test that reaches ``furl_ctx.cli.main()`` in-process without one
    does not: CLI entrypoints ``setdefault`` ``FURL_CCR_BACKEND=sqlite``
    before dispatch (``furl_ctx/cli.py``), so the first uncovered call
    durably opens ``~/.furl/ccr.sqlite3`` on the real machine (audit
    R2#11) — hook-counter rows the product treats as user data landing in a
    developer's actual home directory from a plain ``pytest`` run.

    Session-scoped and autouse, so this runs before any test BODY executes —
    the floor every test starts from is a temp dir, never the real home. A
    plain ``setdefault`` (not an unconditional assignment) so:

    * a developer who exported ``FURL_WORKSPACE_DIR`` themselves before
      invoking pytest keeps that explicit choice;
    * per-test ``monkeypatch.setenv("FURL_WORKSPACE_DIR", ...)`` (the
      existing convention — see ``tests/matrix/conftest.py``,
      ``test_hook_counters.py``) is untouched: monkeypatch snapshots and
      restores around each test, so it overrides this sandbox for the
      test's duration and falls back to it again afterward, never to the
      real default.

    The ``furl_ctx.paths`` import is deferred to fixture-setup time (not
    module scope) because pytest imports ``tests/conftest.py`` before it
    calls the root ``conftest.py``'s ``pytest_configure`` hook — the guard
    that turns an unbuilt ``furl_ctx._core`` into one friendly message.
    Importing ``furl_ctx`` eagerly from this module would import
    ``furl_ctx/__init__.py`` (which imports the native extension
    transitively) before that guard runs, replacing its friendly message
    with a raw traceback on a fresh, unbuilt checkout.
    """
    from furl_ctx.paths import FURL_WORKSPACE_DIR_ENV

    already_set = FURL_WORKSPACE_DIR_ENV in os.environ
    sandbox_dir = tmp_path_factory.mktemp("furl-workspace")
    os.environ.setdefault(FURL_WORKSPACE_DIR_ENV, str(sandbox_dir))
    yield
    if not already_set:
        os.environ.pop(FURL_WORKSPACE_DIR_ENV, None)
