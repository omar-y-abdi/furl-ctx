"""Audit #4 — per-project CCR isolation + reset backend-close (invariants A–E).

The per-namespace store mechanism already gives *structural* isolation (each
namespace gets its own sqlite file / ``CompressionStore``); what was missing was
a per-project **default** so the plugin's hook + MCP server stop sharing one
machine-global ``~/.furl/ccr.sqlite3``. This suite proves the new
``FURL_CCR_PROJECT_DIR`` default wires that isolation across
search/list/retrieve/evict, that the explicit shared override still works, that
a pre-upgrade global store stays readable, and that ``reset_compression_store``
closes the sqlite handles it drops (P5).

Stdlib only; the autouse fixture pins a sandbox workspace and clears the
explicit-namespace env so a stray value cannot leak between cases.
"""

from __future__ import annotations

import pytest

from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import (
    FURL_CCR_NAMESPACE_ENV,
    FURL_CCR_PROJECT_DIR_ENV,
    CompressionStore,
    get_compression_store,
    reset_compression_store,
    resolve_ccr_namespace_store,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Durable sqlite under the sandbox; no explicit namespace; clean registry."""
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    monkeypatch.delenv(FURL_CCR_NAMESPACE_ENV, raising=False)
    monkeypatch.delenv(FURL_CCR_PROJECT_DIR_ENV, raising=False)
    reset_compression_store()
    yield
    reset_compression_store()


def _project_store(monkeypatch: pytest.MonkeyPatch, project_dir) -> CompressionStore:
    """Resolve the per-project store for ``project_dir`` (env read fresh)."""
    monkeypatch.setenv(FURL_CCR_PROJECT_DIR_ENV, str(project_dir))
    store = resolve_ccr_namespace_store()
    assert store is not None, "FURL_CCR_PROJECT_DIR set but no per-project store resolved"
    return store


# --------------------------------------------------------------------------- #
# Baseline: the default is inert without the deployment signal (1921 contract)
# --------------------------------------------------------------------------- #


def test_default_without_project_dir_stays_global() -> None:
    """No FURL_CCR_PROJECT_DIR / namespace → global singleton, byte-for-byte."""
    assert resolve_ccr_namespace_store() is None


# --------------------------------------------------------------------------- #
# A. Confidentiality — search / list / retrieve never cross the project line
# --------------------------------------------------------------------------- #


def test_A_search_list_retrieve_isolated_across_projects(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    store_a = _project_store(monkeypatch, tmp_path / "projA")
    store_b = _project_store(monkeypatch, tmp_path / "projB")
    assert store_a is not store_b, "two projects must resolve to distinct stores"

    key = store_a.store("SECRET token=abc123 belongs to project A", "<<ccr:a>>")

    # retrieve — the un-redacted full-content path the audit called out
    assert store_a.retrieve(key) is not None, "A must recover its own entry"
    assert store_b.retrieve(key) is None, "project B retrieved project A's original"

    # search / list — no preview or hash of A may surface in B
    assert any(m.hash == key for m in store_a.search_all("SECRET")), "A cannot find its own entry"
    assert store_b.search_all("SECRET") == [], "project B's search surfaced project A's entry"


# --------------------------------------------------------------------------- #
# B. Data loss — eviction is scoped to the acting store's own backend
# --------------------------------------------------------------------------- #


def test_B_eviction_scoped_to_own_backend(tmp_path) -> None:
    """Filling A past its cap evicts only within A; B (separate file) is intact.

    Per-project isolation reduces to this per-store property: each namespace has
    its OWN backend + cap + eviction heap, so oldest-first eviction can never
    reach across the boundary.
    """
    store_a = CompressionStore(max_entries=1, backend=SqliteBackend(db_path=tmp_path / "a.sqlite3"))
    store_b = CompressionStore(max_entries=1, backend=SqliteBackend(db_path=tmp_path / "b.sqlite3"))
    try:
        b_key = store_b.store("B keeps this", "<<ccr:b>>")
        a1 = store_a.store("A first", "<<ccr:a1>>")
        a2 = store_a.store("A second", "<<ccr:a2>>")

        # A evicted its OWN oldest (proves eviction actually fired) ...
        assert store_a.retrieve(a2) is not None
        assert store_a.retrieve(a1) is None
        # ... while B's entry, in a separate backend, is untouched.
        assert store_b.retrieve(b_key) is not None, "A's eviction deleted B's entry"
    finally:
        store_a.close()
        store_b.close()


def test_B_project_stores_have_distinct_backends(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    store_a = _project_store(monkeypatch, tmp_path / "A")
    store_b = _project_store(monkeypatch, tmp_path / "B")
    assert store_a._backend is not store_b._backend


# --------------------------------------------------------------------------- #
# C. Explicit shared override still works
# --------------------------------------------------------------------------- #


def test_C_explicit_shared_namespace_overrides_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """FURL_CCR_NAMESPACE wins over the per-project default → one shared store."""
    monkeypatch.setenv(FURL_CCR_NAMESPACE_ENV, "team-shared")

    s1 = _project_store(monkeypatch, tmp_path / "A")  # also sets PROJECT_DIR=A
    s2 = _project_store(monkeypatch, tmp_path / "B")  # also sets PROJECT_DIR=B

    assert s1 is s2, "an explicit namespace must be shared across projects"
    key = s1.store("shared across projects", "<<ccr:s>>")
    assert s2.retrieve(key) is not None


# --------------------------------------------------------------------------- #
# D. Backward compat — a pre-upgrade (0.27.0) global store stays readable
# --------------------------------------------------------------------------- #


def test_D_pre_upgrade_global_store_readable_after_upgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A 0.27.0 global ``ccr.sqlite3`` is not orphaned: the per-project default
    deliberately does not read it (that commingling is the vulnerability), and
    it stays fully readable once isolation is disabled (the documented opt-out).

    The upgrade is modeled as a process restart — the durable file survives; the
    new process's default just points somewhere else — NOT ``reset``, which
    wipes the backend.
    """
    from furl_ctx import paths

    # 0.27.0 process: write durably to the global ccr.sqlite3, then exit (close).
    legacy_path = paths.ccr_sqlite_path()  # == <workspace>/ccr.sqlite3 under the sandbox
    writer = CompressionStore(backend=SqliteBackend(db_path=legacy_path))
    legacy_key = writer.store("pre-upgrade original", "<<ccr:legacy>>", ttl=3600)
    assert writer.retrieve(legacy_key) is not None
    writer.close()  # durable rows stay on disk; only the connection is released

    # Upgrade: the per-project default becomes active. The global row survives on
    # disk but the isolated per-project store must NOT surface it.
    proj_store = _project_store(monkeypatch, tmp_path / "proj")
    assert proj_store.retrieve(legacy_key) is None, "per-project store leaked the legacy global row"

    # Opt-out / recovery via the REAL default path: isolation off → the global
    # singleton on ccr_sqlite_path() (the legacy file) recovers the row intact.
    monkeypatch.setenv(FURL_CCR_PROJECT_DIR_ENV, "")
    assert resolve_ccr_namespace_store() is None, "empty FURL_CCR_PROJECT_DIR must disable scoping"
    assert paths.ccr_sqlite_path() == legacy_path, (
        "global default no longer points at the 0.27 file"
    )
    recovered = get_compression_store().retrieve(legacy_key)
    assert recovered is not None, "pre-upgrade data was orphaned"
    assert recovered.original_content == "pre-upgrade original"


# --------------------------------------------------------------------------- #
# E. reset_compression_store closes the backends it drops (P5 leak)
# --------------------------------------------------------------------------- #


def test_E_reset_closes_namespace_backend(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    store = _project_store(monkeypatch, tmp_path / "proj")
    store.store("opens a connection", "<<ccr:e>>")
    backend = store._backend
    assert isinstance(backend, SqliteBackend)
    assert backend._all_connections, "expected an open sqlite connection after a write"

    reset_compression_store()
    assert backend._all_connections == [], "reset must close + drop the backend's connections (P5)"


def test_E_reset_closes_global_backend() -> None:
    store = get_compression_store()
    store.store("global write", "<<ccr:g>>")
    backend = store._backend
    assert isinstance(backend, SqliteBackend)
    assert backend._all_connections

    reset_compression_store()
    assert backend._all_connections == []


def test_E_close_is_idempotent_and_fail_open_on_memory_backend() -> None:
    """In-memory backend has no ``close``; ``CompressionStore.close`` is a no-op."""
    store = CompressionStore()  # default InMemoryBackend
    store.close()
    store.close()  # idempotent, no AttributeError
