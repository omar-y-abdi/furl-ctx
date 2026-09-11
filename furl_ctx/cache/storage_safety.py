"""Proof-oriented coordination primitives for CCR storage.

Ordinary CCR reads and writes intentionally fail open so compression never takes
its host down. Destructive operations and hash-identity decisions have the
opposite contract: uncertainty must never be mistaken for absence or success.
This module is the small boundary between those two policies.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:  # pragma: no cover - platform split
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


class StorageUnavailableError(RuntimeError):
    """A proof-requiring storage operation could not establish its result."""


_guard_registry_lock = threading.Lock()
_guard_registry: dict[str, threading.RLock] = {}


def _process_lock(identity: str) -> threading.RLock:
    with _guard_registry_lock:
        lock = _guard_registry.get(identity)
        if lock is None:
            lock = threading.RLock()
            _guard_registry[identity] = lock
        return lock


class MutationGuard:
    """Serialize CCR graph/identity mutations across threads and processes.

    ``CompressionStore._lock`` protects one Python object only. Two MCP server
    processes can still point at the same SQLite file and race a collision check
    or rewrite a marker graph between cascade preflight and delete. A durable
    backend exposes a filesystem coordination identity; stores sharing it take
    the same advisory lock file for the whole logical mutation.

    The in-process lock is registry-backed so two store instances sharing one
    identity also serialize. The context is re-entrant per thread because a
    cascade calls the public delete primitive while already holding the logical
    mutation guard.
    """

    def __init__(self, identity: str) -> None:
        self._identity = identity
        self._thread_lock = _process_lock(identity)
        self._local = threading.local()
        self._lock_path = self._derive_lock_path(identity)

    @staticmethod
    def _derive_lock_path(identity: str) -> Path | None:
        if not identity.startswith("file:"):
            return None
        db_path = Path(identity[5:])
        return db_path.with_name(f"{db_path.name}.mutation.lock")

    @contextmanager
    def hold(self) -> Iterator[None]:
        with self._thread_lock:
            depth = int(getattr(self._local, "depth", 0))
            if depth:
                self._local.depth = depth + 1
                try:
                    yield
                finally:
                    self._local.depth -= 1
                return

            self._local.depth = 1
            fd: int | None = None
            try:
                if self._lock_path is not None:
                    self._lock_path.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                    os.chmod(self._lock_path, 0o600)
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    if fd is not None and fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    if fd is not None:
                        os.close(fd)
                    self._local.depth = 0
