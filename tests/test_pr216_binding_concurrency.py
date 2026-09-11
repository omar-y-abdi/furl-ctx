"""Adversarial identity/concurrency regressions for PR #216 explicit hashes."""

from __future__ import annotations

import multiprocessing as mp
from typing import Any

from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import CompressionStore, DurableWriteError

HASH_KEY = "f" * 24
_SPAWN = mp.get_context("spawn")


def _race_writer(db_path: str, original: str, barrier: Any, result_q: Any) -> None:
    backend = SqliteBackend(db_path=db_path, max_rows=100)
    store = CompressionStore(backend=backend, enable_feedback=False)
    try:
        barrier.wait(timeout=20)
        try:
            store.store(
                original,
                f"view-{original}",
                explicit_hash=HASH_KEY,
                require_durable=True,
            )
        except DurableWriteError:
            result_q.put("collision")
        else:
            result_q.put("accepted")
    finally:
        backend.close()


def test_two_processes_cannot_accept_different_explicit_bindings(tmp_path: Any) -> None:
    """The public store operation, not only the backend CAS, is race-safe."""
    db_path = str(tmp_path / "shared-binding.sqlite3")
    SqliteBackend(db_path=db_path).close()
    barrier = _SPAWN.Barrier(2)
    result_q: Any = _SPAWN.Queue()
    workers = [
        _SPAWN.Process(target=_race_writer, args=(db_path, original, barrier, result_q))
        for original in ("producer-A", "producer-B")
    ]
    for worker in workers:
        worker.start()
    outcomes = sorted(result_q.get(timeout=30) for _ in workers)
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    assert outcomes == ["accepted", "collision"]
    backend = SqliteBackend(db_path=db_path, max_rows=100)
    try:
        binding = backend.get_binding(HASH_KEY)
        assert binding is not None and binding[1] is True
        store = CompressionStore(backend=backend, enable_feedback=False)
        assert store.retrieve(HASH_KEY) is None
    finally:
        backend.close()


def test_delete_retires_explicit_identity_instead_of_allowing_foreign_rebind(
    tmp_path: Any,
) -> None:
    """Deleting bytes must not let an old marker resolve to different future bytes."""
    db_path = tmp_path / "retired-binding.sqlite3"
    backend = SqliteBackend(db_path=db_path, max_rows=100)
    store = CompressionStore(backend=backend, enable_feedback=False)
    store.store("producer-A", "view-A", explicit_hash=HASH_KEY)
    assert store.delete(HASH_KEY) is True
    assert store.retrieve(HASH_KEY) is None
    backend.close()

    reopened_backend = SqliteBackend(db_path=db_path, max_rows=100)
    reopened = CompressionStore(backend=reopened_backend, enable_feedback=False)
    try:
        # K was already published as an identity for A. Reusing K for B would
        # make any old marker for A retrieve foreign content, even though A's
        # payload was deliberately deleted.
        reopened.store("producer-B", "view-B", explicit_hash=HASH_KEY)
        assert reopened.retrieve(HASH_KEY) is None
        binding = reopened_backend.get_binding(HASH_KEY)
        assert binding is not None and binding[1] is True
    finally:
        reopened_backend.close()
