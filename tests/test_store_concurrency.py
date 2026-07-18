"""Store-concurrency-honesty — contention-robust durable writes + a proven veto bound.

Two Claude Code sessions on one project run two furl MCP server processes that
share the per-project namespace SQLite store. Before this change the durable
write path vetoed the INSTANT the first persist reported non-durable — no
store-level busy/retry — so everyday cross-process contention produced spurious
vetoes (and, downstream, dishonest "unrecoverable"/"not guaranteed" messages)
even though the entry was retrievable moments later.

This pins:
* a durable write RETRIES the persist under a bounded, capped-backoff budget and
  SUCCEEDS once transient contention clears (no veto);
* real N-process concurrent writers on one shared store all land DURABLY with
  ZERO vetoes and every original round-trips byte-exact;
* the bound still exists: a sibling holding the SQLite write lock LONGER than the
  whole retry budget still vetoes — and the veto is honest (carries the hash, the
  entry is retrievable in-process now, and the message names the likely cause).

Deterministic-for-CI: contention is created with real locks + multiprocessing
barriers/events, never fixed sleeps; the veto tests inject a tiny retry budget so
the bound is reached fast.
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
from typing import Any

import pytest

from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import CompressionStore, DurableWriteError
from tests._fixtures import make_fail_open_sqlite_backend

_SPAWN = mp.get_context("spawn")


# ── a durability backend whose set_durable is flaky, then heals ───────────────


class _FlakyDurableBackend:
    """Wrap ``InMemoryBackend`` and expose ``set_durable`` that reports ``False``
    for the first ``fail_times`` calls, then ``True`` — a deterministic stand-in
    for transient lock contention that clears. The entry is always stored in the
    volatile tier, so same-process retrieval round-trips throughout."""

    def __init__(self, fail_times: int) -> None:
        from furl_ctx.cache.backends import InMemoryBackend

        self._mem = InMemoryBackend()
        self._fail_times = fail_times
        self.durable_calls = 0

    def set_durable(self, hash_key: str, entry: Any) -> bool:
        self.durable_calls += 1
        self._mem.set(hash_key, entry)
        return self.durable_calls > self._fail_times

    def __getattr__(self, name: str) -> Any:
        # Delegate every Protocol method (get/set/delete/exists/count/keys/items/
        # purge_expired/created_at_index/get_stats/close/...) to the wrapped
        # in-memory backend. Guard _mem so a pre-__init__ access can't recurse.
        if name == "_mem":
            raise AttributeError(name)
        return getattr(self._mem, name)


def _tiny_retry_store(backend: Any, *, attempts: int) -> CompressionStore:
    """A store with a near-zero backoff so retry/veto timing is fast + stable."""
    return CompressionStore(
        backend=backend,
        enable_feedback=False,
        durable_retry_attempts=attempts,
        durable_retry_base_backoff_seconds=0.001,
        durable_retry_max_backoff_seconds=0.001,
    )


# ── 1. retry succeeds once transient contention clears ────────────────────────


def test_durable_write_retries_until_contention_clears() -> None:
    backend = _FlakyDurableBackend(fail_times=2)
    store = _tiny_retry_store(backend, attempts=3)

    # First persist + 3 retries available; the 3rd call heals → no veto.
    key = store.store(
        original="payload", compressed="c", explicit_hash="a" * 12, require_durable=True
    )

    assert key == "a" * 12
    assert backend.durable_calls == 3, "should have retried until the write landed durably"
    entry = store.retrieve("a" * 12)
    assert entry is not None and entry.original_content == "payload"


def test_durable_write_vetoes_when_budget_exhausted_before_contention_clears() -> None:
    # Needs 4 healthy calls but only 1 first-persist + 2 retries = 3 attempts.
    backend = _FlakyDurableBackend(fail_times=4)
    store = _tiny_retry_store(backend, attempts=2)

    with pytest.raises(DurableWriteError) as excinfo:
        store.store(original="p", compressed="c", explicit_hash="b" * 12, require_durable=True)

    assert backend.durable_calls == 3, "first persist + exactly `attempts` retries, then veto"
    assert excinfo.value.hash_key == "b" * 12
    # Retrievable NOW in the volatile tier despite the veto.
    assert store.retrieve("b" * 12) is not None


# ── 2. the veto is honest (carries hash + names the cause, no false loss) ──────


def test_durable_write_error_is_honest(tmp_path) -> None:
    store = _tiny_retry_store(make_fail_open_sqlite_backend(tmp_path / "veto.sqlite3"), attempts=2)

    with pytest.raises(DurableWriteError) as excinfo:
        store.store(original="s", compressed="m", explicit_hash="e" * 12, require_durable=True)

    exc = excinfo.value
    message = str(exc)
    assert exc.hash_key == "e" * 12
    assert "e" * 12 in message, "the message must carry the hash the data is retrievable under"
    lowered = message.lower()
    # Honest about the data reality...
    assert "retrievable now" in lowered
    assert "restart" in lowered
    # ...and the likely cause + the runbook pointer.
    assert "another furl mcp server process" in lowered
    assert "claude code session" in lowered
    assert "LIBRARY.md" in message
    # ...and it must NOT claim the data is lost when it is retrievable right now.
    assert "unrecoverable" not in lowered
    assert "not guaranteed" not in lowered
    # Proof the claim is true: the entry round-trips from the volatile tier now.
    entry = store.retrieve("e" * 12)
    assert entry is not None and entry.original_content == "s"


# ── 3. real N-process concurrency: all durable, zero vetoes, all retrievable ──


def _concurrent_writer(db_path: str, keys: list[str], barrier: Any, result_q: Any) -> None:
    backend = SqliteBackend(db_path=db_path)
    store = CompressionStore(backend=backend, enable_feedback=False)
    barrier.wait(timeout=30)  # release all writers together → real contention
    vetoes = 0
    for key in keys:
        try:
            store.store(
                original=f"original-for-{key}",
                compressed="c",
                explicit_hash=key,
                require_durable=True,
            )
        except DurableWriteError:
            vetoes += 1
    backend.close()
    result_q.put(vetoes)


def test_concurrent_processes_write_durably_without_veto(tmp_path) -> None:
    db_path = str(tmp_path / "ccr.sqlite3")
    SqliteBackend(db_path=db_path).close()  # create + initialize the shared file once

    n_procs, per_proc = 3, 12
    keys_by_proc = [[f"{p:02x}{i:010x}" for i in range(per_proc)] for p in range(n_procs)]

    barrier = _SPAWN.Barrier(n_procs)
    result_q: Any = _SPAWN.Queue()
    procs = [
        _SPAWN.Process(
            target=_concurrent_writer, args=(db_path, keys_by_proc[p], barrier, result_q)
        )
        for p in range(n_procs)
    ]
    for proc in procs:
        proc.start()
    total_vetoes = sum(result_q.get(timeout=60) for _ in range(n_procs))
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0

    assert total_vetoes == 0, "realistic cross-process contention must not veto any durable write"

    # Every original is durably present and byte-exact from a fresh reader.
    reader = CompressionStore(backend=SqliteBackend(db_path=db_path), enable_feedback=False)
    for keys in keys_by_proc:
        for key in keys:
            entry = reader.retrieve(key)
            assert entry is not None, f"durable entry {key} missing from the shared file"
            assert entry.original_content == f"original-for-{key}"


# ── 4. the bound: a sibling holding the write lock past the budget still vetoes ─


def _exclusive_lock_holder(db_path: str, ready_evt: Any, release_evt: Any) -> None:
    conn = sqlite3.connect(db_path, timeout=0.1)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN EXCLUSIVE")
    # Take a real write to cement the writer lock, then hold it (uncommitted)
    # until the parent has observed the veto — no fixed sleep, so no flake.
    conn.execute("CREATE TABLE IF NOT EXISTS _lock_probe (x INTEGER)")
    ready_evt.set()
    release_evt.wait(timeout=30)
    conn.rollback()
    conn.close()


def test_exclusive_lock_longer_than_budget_still_vetoes(tmp_path) -> None:
    db_path = str(tmp_path / "ccr.sqlite3")
    backend = SqliteBackend(db_path=db_path, busy_timeout_seconds=0.05, lock_retries=1)
    store = _tiny_retry_store(backend, attempts=1)

    # A write BEFORE contention lands durably (schema is live, no lock held).
    store.store(original="pre", compressed="c", explicit_hash="c" * 12, require_durable=True)
    assert store.retrieve("c" * 12) is not None

    ready_evt = _SPAWN.Event()
    release_evt = _SPAWN.Event()
    holder = _SPAWN.Process(target=_exclusive_lock_holder, args=(db_path, ready_evt, release_evt))
    holder.start()
    try:
        assert ready_evt.wait(timeout=20), "lock holder never acquired the write lock"
        # The sibling holds the write lock for the whole call → every attempt in
        # the (tiny) budget loses the race → veto, honestly.
        with pytest.raises(DurableWriteError) as excinfo:
            store.store(
                original="blocked", compressed="c", explicit_hash="d" * 12, require_durable=True
            )
        assert excinfo.value.hash_key == "d" * 12
        # Retrievable in-process now even though it never reached the shared file.
        blocked = store.retrieve("d" * 12)
        assert blocked is not None and blocked.original_content == "blocked"
    finally:
        release_evt.set()
        holder.join(timeout=20)
