"""CCR durable-retention: per-tenant namespacing + export/import (B2).

Pins the three contracts B2 delivers:

(a) ISOLATION — an entry stored under namespace A is NOT retrievable under
    namespace B. This is the security invariant: tenant data must not leak
    across namespaces. Proven at the resolver level (the store objects are
    distinct, so a cross-namespace ``retrieve`` structurally returns None).
(b) ROUND-TRIP — ``ccr_export`` -> ``ccr_import`` restores entries byte-exact,
    including ``created_at`` / ``ttl`` / ``retrieval_count`` (backend-level copy,
    not a re-``store()`` that would recompute the key and reset metadata).
(c) DEFAULT — with no namespace and no ``session_id`` / ``agent_id``, the
    request ContextVar is left untouched and behavior is the global one.

Stdlib only; the autouse ``reset_compression_store`` keeps namespace stores
from leaking between tests (the registry is folded into that reset).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from furl_ctx import ccr_export, ccr_import, compress
from furl_ctx.cache.backends.memory import InMemoryBackend
from furl_ctx.cache.compression_store import (
    FURL_CCR_NAMESPACE_ENV,
    CompressionStore,
    _namespace_key,
    _request_ccr_store,
    clear_request_compression_store,
    reset_compression_store,
    set_request_compression_store,
    resolve_ccr_namespace_store,
)


@pytest.fixture(autouse=True)
def _fresh_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Isolate every test: fresh workspace dir, no namespace env, clean stores.

    The workspace is redirected to ``tmp_path`` so any sqlite-backed namespace
    (when ``FURL_CCR_BACKEND=sqlite``) writes under the temp dir, never the real
    ``~/.furl``. The env namespace is cleared so a stray value cannot make the
    "default" cases resolve a tenant store.
    """
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv(FURL_CCR_NAMESPACE_ENV, raising=False)
    reset_compression_store()
    yield
    reset_compression_store()


# --------------------------------------------------------------------------- #
# (a) Cross-namespace isolation — the security invariant
# --------------------------------------------------------------------------- #


def test_entry_in_namespace_a_not_retrievable_in_namespace_b() -> None:
    """An entry stored under session A must miss under session B."""
    store_a = resolve_ccr_namespace_store("tenant-A", None)
    store_b = resolve_ccr_namespace_store("tenant-B", None)
    assert store_a is not None and store_b is not None
    assert store_a is not store_b, "distinct namespaces must get distinct stores"

    hash_key = store_a.store("SECRET original for tenant A", "<<ccr:x>>")

    assert store_a.retrieve(hash_key) is not None, "A must recover its own entry"
    assert store_b.retrieve(hash_key) is None, (
        "namespace B retrieved namespace A's entry — tenant isolation is broken"
    )


def test_agent_id_participates_in_isolation_boundary() -> None:
    """Same session, different agent_id => different (isolated) store."""
    store_agent1 = resolve_ccr_namespace_store("shared-session", "agent-1")
    store_agent2 = resolve_ccr_namespace_store("shared-session", "agent-2")
    assert store_agent1 is not store_agent2

    key = store_agent1.store("agent-1 private data", "<<ccr:y>>")
    assert store_agent2.retrieve(key) is None


def test_env_namespace_isolates_from_no_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FURL_CCR_NAMESPACE alone forms a boundary distinct from the global store."""
    monkeypatch.setenv(FURL_CCR_NAMESPACE_ENV, "org-42")
    tenant_store = resolve_ccr_namespace_store(None, None)
    assert tenant_store is not None

    key = tenant_store.store("org-42 data", "<<ccr:z>>")

    # A different env namespace must not see it.
    monkeypatch.setenv(FURL_CCR_NAMESPACE_ENV, "org-99")
    other_store = resolve_ccr_namespace_store(None, None)
    assert other_store is not tenant_store
    assert other_store.retrieve(key) is None


def test_sqlite_namespaces_use_distinct_files_and_stay_isolated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Durable path: each namespace gets its OWN sqlite file, no cross-read.

    A filename derived from raw ids could collide two tenants onto one file;
    the hashed filename must keep them apart AND the stored bytes must not be
    visible across namespaces even when both are durable.
    """
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    store_a = resolve_ccr_namespace_store("durable-A", None)
    store_b = resolve_ccr_namespace_store("durable-B", None)
    assert store_a is not None and store_b is not None

    # Distinct backing files on disk.
    path_a = Path(store_a._backend.get_stats()["db_path"])
    path_b = Path(store_b._backend.get_stats()["db_path"])
    assert path_a != path_b, "two tenants collided onto one sqlite file"
    assert path_a.parent == tmp_path, "namespace db must live under the workspace"

    key = store_a.store("durable secret A", "<<ccr:d>>")
    assert store_a.retrieve(key) is not None
    assert store_b.retrieve(key) is None


def test_untrusted_session_id_does_not_traverse_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A traversal-shaped session_id must resolve to a safe hashed filename."""
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    store = resolve_ccr_namespace_store("../../etc/passwd", None)
    assert store is not None
    db_path = Path(store._backend.get_stats()["db_path"])
    assert db_path.parent == tmp_path, "session_id escaped the workspace dir"
    assert db_path.name.startswith("ccr-ns-") and db_path.name.endswith(".sqlite3")


def test_same_namespace_tuple_shares_store_across_calls() -> None:
    """Identical (namespace, session, agent) => same store, so cross-turn works."""
    first = resolve_ccr_namespace_store("sess", "agent")
    key = first.store("turn-1 content", "<<ccr:t>>")

    # A later "turn" with the same ids resolves the SAME store and recovers it.
    second = resolve_ccr_namespace_store("sess", "agent")
    assert second is first
    entry = second.retrieve(key)
    assert entry is not None and entry.original_content == "turn-1 content"


# --------------------------------------------------------------------------- #
# (b) export -> import round-trip (byte-exact)
# --------------------------------------------------------------------------- #


def test_export_import_round_trip_restores_entries_byte_exact(
    tmp_path: Path,
) -> None:
    """ccr_export then ccr_import into a fresh namespace recovers originals exactly."""
    source = resolve_ccr_namespace_store("export-src", None)
    # Hostile payloads: lone surrogate, NUL, control chars — surrogatepass BLOBs
    # in the sqlite backend must round-trip these unchanged.
    originals = {
        source.store("plain original alpha", "<<ccr:a>>"),
        source.store("with\x00nul and \x01ctrl", "<<ccr:b>>"),
        source.store("lone surrogate \ud800 here", "<<ccr:c>>"),
    }

    checkpoint = tmp_path / "checkpoint.sqlite3"
    exported = ccr_export(checkpoint, session_id="export-src")
    assert exported == len(originals)
    assert checkpoint.exists()

    # Import into a DIFFERENT namespace's store and confirm byte-exactness.
    imported = ccr_import(checkpoint, session_id="import-dst")
    assert imported == len(originals)

    destination = resolve_ccr_namespace_store("import-dst", None)
    assert destination is not None
    for key in originals:
        src_entry = source.retrieve(key)
        dst_entry = destination.retrieve(key)
        assert src_entry is not None and dst_entry is not None
        assert dst_entry.original_content == src_entry.original_content
        assert dst_entry.compressed_content == src_entry.compressed_content
        # Metadata preserved (backend-level copy, not a re-store()).
        assert dst_entry.created_at == src_entry.created_at
        assert dst_entry.ttl == src_entry.ttl
        assert dst_entry.hash == src_entry.hash


def test_export_returns_zero_for_empty_store(tmp_path: Path) -> None:
    """Exporting an empty tenant writes a file with zero entries (no crash)."""
    resolve_ccr_namespace_store("empty-tenant", None)
    count = ccr_export(tmp_path / "empty.sqlite3", session_id="empty-tenant")
    assert count == 0


def test_import_does_not_leak_into_other_namespace(tmp_path: Path) -> None:
    """Entries imported into namespace X are not visible in namespace Y."""
    source = resolve_ccr_namespace_store("src", None)
    key = source.store("only for X", "<<ccr:q>>")
    checkpoint = tmp_path / "cp.sqlite3"
    ccr_export(checkpoint, session_id="src")

    ccr_import(checkpoint, session_id="dst-X")
    other = resolve_ccr_namespace_store("dst-Y", None)
    assert other is not None
    assert other.retrieve(key) is None, "import leaked across namespaces"


# --------------------------------------------------------------------------- #
# (c) default behavior unchanged (no namespace => ContextVar untouched)
# --------------------------------------------------------------------------- #


def test_no_namespace_leaves_request_contextvar_untouched() -> None:
    """resolve returns None and nothing is bound when no namespace is active."""
    assert _namespace_key(None, None) is None
    assert resolve_ccr_namespace_store(None, None) is None
    assert _request_ccr_store.get() is None


def test_compress_default_path_does_not_bind_contextvar() -> None:
    """compress() with no session/agent leaves the request store unset."""
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there, how can I help?"},
    ]
    before = _request_ccr_store.get()
    result = compress(messages, model="claude-sonnet-4-5-20250929")
    after = _request_ccr_store.get()
    assert before is None and after is None, (
        "default compress() must not leave a per-tenant store bound"
    )
    # Sanity: a real result came back (fail-open would still return messages).
    assert result.messages is not None


def test_compress_resets_contextvar_after_namespaced_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A namespaced compress() binds during the call and RESETS after (no leak)."""
    messages = [
        {"role": "user", "content": "question about the data"},
        {"role": "assistant", "content": "here is a detailed answer " * 20},
    ]
    assert _request_ccr_store.get() is None
    compress(messages, model="claude-sonnet-4-5-20250929", session_id="tenant-Z")
    # The token was reset in the finally; the ContextVar is back to unset.
    assert _request_ccr_store.get() is None


def test_namespaced_compress_fails_open_on_store_construction_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store resolution failure must fail open (passthrough), never raise.

    Namespace resolution + the ContextVar bind live inside compress()'s
    fail-open boundary, so even a construction blow-up returns the original
    messages with ``error`` set instead of propagating to the host.
    """
    import furl_ctx.cache.compression_store as store_mod

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated store construction failure")

    # compress() imports resolve_ccr_namespace_store from this module at call
    # time, so patch the source module, not the compress module.
    monkeypatch.setattr(store_mod, "resolve_ccr_namespace_store", _boom)

    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a reply " * 20},
    ]
    result = compress(messages, model="claude-sonnet-4-5-20250929", session_id="tenant")
    assert result.messages == messages, "must fall back to original messages"
    assert result.error is not None, "fail-open must surface the error honestly"
    assert _request_ccr_store.get() is None, "ContextVar must stay unbound on failure"


def test_compress_preserves_outer_middleware_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A namespaced compress() restores (not clears) an outer request store."""
    from furl_ctx.cache.compression_store import CompressionStore

    outer = CompressionStore(max_entries=10, enable_feedback=False)
    token = _request_ccr_store.set(outer)
    try:
        messages = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a longer assistant reply " * 20},
        ]
        compress(messages, model="claude-sonnet-4-5-20250929", session_id="tenantX")
        # reset (not clear) must restore the OUTER store, not leave None.
        assert _request_ccr_store.get() is outer, (
            "namespaced compress() clobbered an outer middleware store"
        )
    finally:
        _request_ccr_store.reset(token)


def test_export_import_preserves_arbitrary_explicit_hash_identity(tmp_path: Path) -> None:
    """Checkpoint round-trip must preserve the provenance that makes explicit keys safe."""
    source = resolve_ccr_namespace_store("explicit-export-src", None)
    assert source is not None
    explicit_hash = "9" * 24
    source.store("explicit producer payload", "view", explicit_hash=explicit_hash)
    before = source.retrieve(explicit_hash)
    assert before is not None

    checkpoint = tmp_path / "explicit-checkpoint.sqlite3"
    assert ccr_export(checkpoint, session_id="explicit-export-src") == 1
    assert ccr_import(checkpoint, session_id="explicit-import-dst") == 1

    destination = resolve_ccr_namespace_store("explicit-import-dst", None)
    assert destination is not None
    after, status = destination.retrieve_with_status(explicit_hash)
    assert status["status"] == "available"
    assert after is not None
    assert after.original_content == before.original_content
    assert after.created_at == before.created_at


def test_export_includes_entries_that_are_retrievable_only_from_spill(tmp_path: Path) -> None:
    """A checkpoint of the store must not drop live entries merely because they were demoted."""
    source = CompressionStore(
        max_entries=1,
        backend=InMemoryBackend(),
        spill=InMemoryBackend(),
        enable_feedback=False,
    )
    set_request_compression_store(source)
    try:
        spilled_hash = source.store("spilled original", "spilled view")
        source.store("primary filler", "filler view")
        assert source.exists(spilled_hash) is False
        assert source.exists_any_tier(spilled_hash) is True

        checkpoint = tmp_path / "spill-checkpoint.sqlite3"
        exported = ccr_export(checkpoint)
    finally:
        clear_request_compression_store()

    assert exported == 2

    destination = CompressionStore(enable_feedback=False)
    set_request_compression_store(destination)
    try:
        assert ccr_import(checkpoint) == 2
        restored = destination.retrieve(spilled_hash)
        assert restored is not None
        assert restored.original_content == "spilled original"
    finally:
        clear_request_compression_store()
