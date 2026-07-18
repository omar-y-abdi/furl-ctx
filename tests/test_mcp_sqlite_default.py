"""Engine P1-7 wiring: the MCP server's store singleton defaults to SQLite.

Durability is an MCP-deployment property: the MCP server process is what
restarts mid-session and what sub-agent processes run alongside, so ITS store
singleton defaults to the durable SQLite backend. ``FURL_CCR_BACKEND=memory``
opts back out; any other explicit ``FURL_CCR_BACKEND`` value defers to the
library's env-selected loader. The library default (plain ``compress()`` with
no env) stays in-memory — pinned in test_sqlite_backend.py.

Requires the optional ``mcp`` extra, mirroring test_mcp_server_handlers.py.
"""

from __future__ import annotations

import os
import stat

import pytest

pytest.importorskip("mcp")

from furl_ctx.cache.backends import InMemoryBackend  # noqa: E402
from furl_ctx.cache.backends.sqlite import SqliteBackend  # noqa: E402
from furl_ctx.cache.compression_store import (  # noqa: E402
    get_compression_store,
    reset_compression_store,
)
from furl_ctx.ccr import mcp_server  # noqa: E402
from furl_ctx.ccr.mcp_server import MCP_SESSION_TTL, FurlMCPServer  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    """Mirror the handler-test isolation: sandboxed workspace, fresh singleton,
    and a clean backend-selection environment. The shared-stats paths follow
    FURL_WORKSPACE_DIR per call (SEC-7), so the setenv alone sandboxes them."""
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("FURL_CCR_BACKEND", raising=False)
    monkeypatch.delenv("FURL_CCR_SQLITE_PATH", raising=False)
    monkeypatch.delenv("FURL_CCR_SQLITE_MAX_ROWS", raising=False)
    # The session TTL is env-aware now (_mcp_session_ttl): the 3600-second
    # assertions below pin the env-UNSET contract, so the premise must be
    # established — earlier suite files (e.g. in-process cli.main() runs)
    # legitimately leave their own FURL_CCR_TTL_SECONDS setdefault behind.
    monkeypatch.delenv("FURL_CCR_TTL_SECONDS", raising=False)
    reset_compression_store()
    yield
    reset_compression_store()


@pytest.fixture
def server() -> FurlMCPServer:
    return FurlMCPServer()


def test_mcp_singleton_defaults_to_sqlite_backend(server, tmp_path) -> None:
    store = server._get_local_store()
    assert isinstance(store._backend, SqliteBackend)
    db = tmp_path / "ccr.sqlite3"
    assert db.is_file(), "the durable store must live under the workspace dir"
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600


def test_mcp_env_memory_opts_back_out(server, monkeypatch) -> None:
    monkeypatch.setenv("FURL_CCR_BACKEND", "memory")
    store = server._get_local_store()
    assert isinstance(store._backend, InMemoryBackend)


def test_mcp_env_sqlite_still_resolves_sqlite(server, monkeypatch, tmp_path) -> None:
    """An EXPLICIT FURL_CCR_BACKEND=sqlite goes through the library loader and
    still lands on the durable backend (no double-construction surprises)."""
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    store = server._get_local_store()
    assert isinstance(store._backend, SqliteBackend)


def test_mcp_session_ttl_relationship_preserved(server) -> None:
    """The singleton still pins default_ttl=MCP_SESSION_TTL (3600) on top of
    the library's 1800 s DEFAULT_CCR_TTL_SECONDS — absent FURL_CCR_TTL_SECONDS
    (cleared by the autouse fixture; when set, the env wins, see
    tests/test_mcp_ttl_env.py) — and remains THE process singleton the
    pipeline's own no-arg get_compression_store() resolves."""
    store = server._get_local_store()
    assert store.default_ttl_seconds == MCP_SESSION_TTL == 3600
    assert get_compression_store() is store


def test_mcp_store_round_trips_through_sqlite(server) -> None:
    store = server._get_local_store()
    original = '[{"id": 7, "v": "needle", "weird": "\ud800"}]'
    key = store.store(original, "<<ccr:abcdef123456>>", explicit_hash="abcdef123456")
    entry = store.retrieve(key)
    assert entry is not None
    assert entry.original_content == original
    assert entry.ttl == MCP_SESSION_TTL


def test_mcp_sqlite_backend_init_failure_falls_back_to_memory(monkeypatch) -> None:
    """If the durable backend cannot be BUILT at all, the MCP server must still
    come up (fail-open to memory) rather than refuse to serve."""

    def boom(*args, **kwargs):
        raise RuntimeError("injected backend construction failure")

    monkeypatch.setattr(mcp_server, "_default_store_backend_factory", boom)
    server = FurlMCPServer()
    store = server._get_local_store()
    assert isinstance(store._backend, InMemoryBackend)
