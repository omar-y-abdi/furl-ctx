"""Cross-process regression coverage for proof-oriented CCR mutation locks."""

from __future__ import annotations

import multiprocessing
import os
from typing import Any

import pytest

from furl_ctx.cache import storage_safety
from furl_ctx.cache.storage_safety import MutationGuard, StorageUnavailableError


def _hold_guard(identity: str, ready: Any, release: Any) -> None:
    with MutationGuard(identity).hold():
        ready.set()
        if not release.wait(10):
            raise RuntimeError("timed out waiting to release mutation guard")


def _attempt_guard(identity: str, attempting: Any, acquired: Any) -> None:
    attempting.set()
    with MutationGuard(identity).hold():
        acquired.set()


def test_mutation_guard_serializes_independent_processes(tmp_path: Any) -> None:
    """Two processes sharing a durable identity cannot overlap a logical mutation."""
    ctx = multiprocessing.get_context("spawn")
    identity = f"file:{tmp_path / 'shared.sqlite3'}"
    ready = ctx.Event()
    release = ctx.Event()
    attempting = ctx.Event()
    acquired = ctx.Event()
    first = ctx.Process(target=_hold_guard, args=(identity, ready, release))
    second = ctx.Process(target=_attempt_guard, args=(identity, attempting, acquired))

    first.start()
    try:
        assert ready.wait(5), "first process never acquired the guard"
        second.start()
        assert attempting.wait(5), "second process never attempted the guard"
        assert not acquired.wait(0.3), "second process overlapped the first mutation"
        release.set()
        assert acquired.wait(5), "second process never acquired after release"
    finally:
        release.set()
        for process in (first, second):
            if process.pid is None:
                continue
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0


class _FakeMsvcrt:
    LK_LOCK = 1
    LK_UNLCK = 2

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int, int]] = []

    def locking(self, fd: int, mode: int, length: int) -> None:
        self.calls.append((mode, length, os.lseek(fd, 0, os.SEEK_CUR), os.fstat(fd).st_size))


def test_mutation_guard_uses_windows_byte_range_lock(monkeypatch: Any, tmp_path: Any) -> None:
    """The Windows path locks the same materialized byte for acquire and release."""
    fake = _FakeMsvcrt()
    monkeypatch.setattr(storage_safety, "fcntl", None)
    monkeypatch.setattr(storage_safety, "msvcrt", fake)
    db_path = tmp_path / "windows.sqlite3"

    with MutationGuard(f"file:{db_path}").hold():
        pass

    assert [call[0] for call in fake.calls] == [fake.LK_LOCK, fake.LK_UNLCK]
    assert [call[1] for call in fake.calls] == [1, 1]
    assert all(call[2] == 0 for call in fake.calls)
    assert all(call[3] == 1 for call in fake.calls)
    assert (tmp_path / "windows.sqlite3.mutation.lock").read_bytes() == b"\0"


def test_mutation_guard_fails_closed_without_process_lock_primitive(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """A durable mutation never silently degrades to process-local locking."""
    monkeypatch.setattr(storage_safety, "fcntl", None)
    monkeypatch.setattr(storage_safety, "msvcrt", None)

    with pytest.raises(StorageUnavailableError, match="no supported inter-process"):
        with MutationGuard(f"file:{tmp_path / 'unsupported.sqlite3'}").hold():
            raise AssertionError("guard body must not run without a process lock")
