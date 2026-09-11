from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

import pytest

from furl_ctx.cache.backends.memory import InMemoryBackend
from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import (
    CollisionSafetyError,
    CompressionEntry,
    CompressionStore,
)

HASH = "7" * 24
FILLER = "8" * 24


def _entry(
    original: str, *, compressed: str = "view", hash_key: str = HASH
) -> CompressionEntry:
    return CompressionEntry(
        hash=hash_key,
        original_content=original,
        compressed_content=compressed,
        original_tokens=1,
        compressed_tokens=1,
        original_item_count=1,
        compressed_item_count=1,
        tool_name=None,
        tool_call_id=None,
        query_context=None,
        created_at=time.time(),
        ttl=3600,
    )



def test_unavailable_binding_authority_never_exposes_volatile_rebind(tmp_path: Any) -> None:
    """An explicit-key safety veto must not shadow an older durable binding with foreign bytes."""
    backend = SqliteBackend(db_path=tmp_path / "authority-outage.sqlite3", max_rows=100)
    store = CompressionStore(
        backend=backend,
        enable_feedback=False,
        durable_retry_attempts=0,
    )
    try:
        store.store("producer-A", "view-A", explicit_hash=HASH, require_durable=True)
        backend._degraded = True

        with pytest.raises(CollisionSafetyError):
            store.store("producer-B", "view-B", explicit_hash=HASH, require_durable=True)

        # The failed producer must not become a same-process shadow for K. The
        # durable A row is temporarily unreadable, so the only honest answer is
        # unavailable until storage recovers.
        entry, status = store.retrieve_with_status(HASH)
        assert entry is None
        assert status["status"] == "unavailable"
        assert backend._memory.get(HASH) is None
    finally:
        backend.close()

def test_unbound_legacy_spill_only_explicit_hash_is_quarantined(tmp_path: Any) -> None:
    """Upgrade cannot infer which pre-PR producer an unbound explicit key belonged to."""
    spill = SqliteBackend(db_path=tmp_path / "legacy-spill.sqlite3", max_rows=100)
    try:
        # Simulate a row created before explicit-hash provenance existed. There
        # is no ccr_bindings record, and the key is not content-derived.
        spill.set(HASH, _entry("legacy-A"))
        store = CompressionStore(
            backend=InMemoryBackend(),
            spill=spill,
            enable_feedback=False,
        )

        entry, status = store.retrieve_with_status(HASH)
        assert entry is None
        assert status["status"] == "unsafe"
    finally:
        spill.close()


def test_expiry_reaps_payload_but_not_published_explicit_identity() -> None:
    """A stale marker must never resolve to different bytes after its payload expires."""
    now = [100.0]
    store = CompressionStore(
        backend=InMemoryBackend(),
        now_fn=lambda: now[0],
        enable_feedback=False,
    )
    store.store("producer-A", "view-A", explicit_hash=HASH, ttl=1)

    now[0] = 102.0
    # Any subsequent store triggers normal expiry GC.
    store.store("filler", "filler", explicit_hash=FILLER)
    assert store.retrieve(HASH) is None

    # The payload is gone, but HASH was already published as A's identity.
    # Reusing it for B would make a surviving old marker for A serve B.
    store.store("producer-B", "view-B", explicit_hash=HASH)
    assert store.retrieve(HASH) is None
    binding = store._backend.get_binding(HASH)  # type: ignore[attr-defined]
    assert binding is not None and binding[1] is True


def test_concurrent_retrieve_cannot_resurrect_verified_delete(tmp_path: Any) -> None:
    """A reader that started before purge must not upsert the deleted row afterward."""
    db_path = tmp_path / "retrieve-race.sqlite3"
    writer_backend = SqliteBackend(db_path=db_path, max_rows=100)
    reader_backend = SqliteBackend(db_path=db_path, max_rows=100)
    writer = CompressionStore(backend=writer_backend, enable_feedback=False)
    reader = CompressionStore(backend=reader_backend, enable_feedback=False)
    try:
        writer.store("payload", "view", explicit_hash=HASH, require_durable=True)

        read_complete = threading.Event()
        allow_reader = threading.Event()
        original_read = reader._read_live_entry_locked

        def paused_read(hash_key: str):
            result = original_read(hash_key)
            read_complete.set()
            assert allow_reader.wait(timeout=5)
            return result

        reader._read_live_entry_locked = paused_read  # type: ignore[method-assign]
        retrieved: list[CompressionEntry | None] = []
        thread = threading.Thread(target=lambda: retrieved.append(reader.retrieve(HASH)))
        thread.start()
        assert read_complete.wait(timeout=5)

        assert writer.delete(HASH) is True
        allow_reader.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert retrieved and retrieved[0] is not None

        assert writer_backend.get(HASH) is None
        assert reader_backend.get(HASH) is None
    finally:
        allow_reader.set()
        writer_backend.close()
        reader_backend.close()


def test_concurrent_search_access_cannot_resurrect_verified_delete(tmp_path: Any) -> None:
    """Search bookkeeping is metadata-only logically; it must never recreate deleted content."""
    db_path = tmp_path / "search-race.sqlite3"
    writer_backend = SqliteBackend(db_path=db_path, max_rows=100)
    reader_backend = SqliteBackend(db_path=db_path, max_rows=100)
    writer = CompressionStore(backend=writer_backend, enable_feedback=False)
    reader = CompressionStore(backend=reader_backend, enable_feedback=False)
    try:
        writer.store(
            '[{"message":"needle"}]',
            "view",
            explicit_hash=HASH,
            require_durable=True,
        )

        set_entered = threading.Event()
        delete_done = threading.Event()
        original_set = reader_backend.set

        def delayed_set(hash_key: str, entry: CompressionEntry) -> None:
            set_entered.set()
            # Old code lets delete finish while this stale bookkeeping write is
            # paused, then INSERT OR REPLACE resurrects the row. Fixed code holds
            # the mutation guard, so delete cannot finish until this returns.
            delete_done.wait(timeout=0.5)
            original_set(hash_key, entry)

        reader_backend.set = delayed_set  # type: ignore[method-assign]
        results: list[list[dict[str, Any]]] = []
        search_thread = threading.Thread(
            target=lambda: results.append(reader.search(HASH, "needle", score_threshold=0.0))
        )
        search_thread.start()
        assert set_entered.wait(timeout=5)

        def purge() -> None:
            writer.delete(HASH)
            delete_done.set()

        delete_thread = threading.Thread(target=purge)
        delete_thread.start()
        # Give the old implementation a deterministic chance to delete while
        # the stale access write is paused. The fixed implementation blocks here.
        delete_done.wait(timeout=1)
        search_thread.join(timeout=5)
        delete_thread.join(timeout=5)
        assert not search_thread.is_alive()
        assert not delete_thread.is_alive()
        assert results and results[0]

        assert writer_backend.get(HASH) is None
        assert reader_backend.get(HASH) is None
    finally:
        writer_backend.close()
        reader_backend.close()


def test_checkpoint_import_cannot_resurrect_concurrent_purge(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Import participates in the same mutation serialization as verified delete."""
    from furl_ctx.cache import compression_store as store_module

    original = "checkpoint-race-payload"
    hash_key = hashlib.sha256(original.encode()).hexdigest()[:24]
    checkpoint = tmp_path / "race-checkpoint.sqlite3"
    checkpoint_backend = SqliteBackend(db_path=checkpoint, max_rows=100)
    checkpoint_backend.set(
        hash_key,
        _entry(original, compressed="checkpoint view", hash_key=hash_key),
    )
    checkpoint_backend.close()

    db_path = tmp_path / "race-destination.sqlite3"
    destination_backend = SqliteBackend(db_path=db_path, max_rows=100)
    destination = CompressionStore(backend=destination_backend, enable_feedback=False)
    monkeypatch.setattr(store_module, "_active_ccr_store", lambda *_args, **_kwargs: destination)

    write_entered = threading.Event()
    delete_done = threading.Event()
    original_set = destination_backend.set
    original_set_durable = destination_backend.set_durable

    def delay() -> None:
        write_entered.set()
        # Old import bypassed MutationGuard, so delete could finish here and the
        # later checkpoint write resurrected the row. Fixed import owns the guard;
        # delete blocks, this timeout elapses, import completes, then delete wins.
        delete_done.wait(timeout=0.5)

    def delayed_set(key: str, entry: CompressionEntry) -> None:
        delay()
        original_set(key, entry)

    def delayed_set_durable(key: str, entry: CompressionEntry) -> bool:
        delay()
        return original_set_durable(key, entry)

    destination_backend.set = delayed_set  # type: ignore[method-assign]
    destination_backend.set_durable = delayed_set_durable  # type: ignore[method-assign]
    import_errors: list[BaseException] = []

    def run_import() -> None:
        try:
            store_module.ccr_import(checkpoint)
        except BaseException as exc:  # pragma: no cover - assertion reports it
            import_errors.append(exc)

    import_thread = threading.Thread(target=run_import)
    import_thread.start()
    assert write_entered.wait(timeout=5)

    def run_delete() -> None:
        destination.delete(hash_key)
        delete_done.set()

    delete_thread = threading.Thread(target=run_delete)
    delete_thread.start()
    delete_done.wait(timeout=1)
    import_thread.join(timeout=5)
    delete_thread.join(timeout=5)

    try:
        assert not import_errors
        assert not import_thread.is_alive()
        assert not delete_thread.is_alive()
        assert destination_backend.get(hash_key) is None
    finally:
        destination_backend.close()
