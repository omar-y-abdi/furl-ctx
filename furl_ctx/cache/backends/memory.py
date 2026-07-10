"""In-memory storage backend for CompressionStore.

This is the default backend, providing fast access with no external dependencies.
Data is lost when the process exits.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..compression_store import CompressionEntry


class InMemoryBackend:
    """In-memory storage backend — a plain dict, no internal locking.

    This is the default backend for CompressionStore.

    Thread-safety (ARCH-10, single ownership story): this class is NOT
    internally synchronized. ``CompressionStore`` serializes every
    backend call under its own lock (``CompressionStore._lock``), so the
    per-operation lock this class used to carry was pure double-locking
    on every hot-path op. Callers using an ``InMemoryBackend`` directly
    from multiple threads (outside ``CompressionStore``) must provide
    their own synchronization.

    Characteristics:
    - Fast: O(1) get/set/delete operations
    - Volatile: Data lost on process exit
    - Memory-bound: Stores everything in RAM

    Usage:
        backend = InMemoryBackend()
        backend.set("abc123", entry)
        entry = backend.get("abc123")
    """

    def __init__(self) -> None:
        """Initialize the in-memory backend."""
        self._store: dict[str, CompressionEntry] = {}

    def get(self, hash_key: str) -> CompressionEntry | None:
        """Retrieve an entry by hash key.

        Args:
            hash_key: The unique hash identifying the entry.

        Returns:
            CompressionEntry if found, None otherwise.
        """
        return self._store.get(hash_key)

    def set(self, hash_key: str, entry: CompressionEntry) -> None:
        """Store an entry with the given hash key.

        Args:
            hash_key: The unique hash identifying the entry.
            entry: The CompressionEntry to store.
        """
        self._store[hash_key] = entry

    def delete(self, hash_key: str) -> bool:
        """Delete an entry by hash key.

        Args:
            hash_key: The unique hash identifying the entry.

        Returns:
            True if entry was deleted, False if it didn't exist.
        """
        if hash_key in self._store:
            del self._store[hash_key]
            return True
        return False

    def exists(self, hash_key: str) -> bool:
        """Check if an entry exists.

        Not part of the ``CompressionStoreBackend`` protocol (ARCH-10) —
        kept as a convenience extra; ``SqliteBackend``'s fallback tier
        relies on it.

        Args:
            hash_key: The unique hash identifying the entry.

        Returns:
            True if entry exists, False otherwise.
        """
        return hash_key in self._store

    def clear(self) -> None:
        """Remove all entries from storage."""
        self._store.clear()

    def count(self) -> int:
        """Get the number of entries in storage.

        Returns:
            Number of entries currently stored.
        """
        return len(self._store)

    def keys(self) -> list[str]:
        """Get all hash keys in storage.

        Not part of the ``CompressionStoreBackend`` protocol (ARCH-10) —
        kept as a convenience extra; ``SqliteBackend``'s tier merge
        relies on it.

        Returns:
            List of all hash keys.
        """
        return list(self._store.keys())

    def items(self) -> list[tuple[str, CompressionEntry]]:
        """Get all entries as (hash_key, entry) pairs.

        Returns:
            List of (hash_key, CompressionEntry) tuples.
        """
        return list(self._store.items())

    def purge_expired(self, now: float) -> int:
        """Delete entries whose per-row TTL elapsed by ``now``; return the count.

        The store's expiry GC (audit #2): lets ``CompressionStore`` reap expired
        entries without materializing them back out through ``items()``. ``now``
        is the store's clock, so expiry matches the store's own TTL checks.
        """
        expired = [key for key, entry in self._store.items() if entry.is_expired(now)]
        for key in expired:
            del self._store[key]
        return len(expired)

    def created_at_index(self) -> list[tuple[float, str]]:
        """``(created_at, hash_key)`` pairs — the projection the store rebuilds
        its eviction heap from, without carrying the full entries (audit #2)."""
        return [(entry.created_at, key) for key, entry in self._store.items()]

    def get_stats(self) -> dict[str, Any]:
        """Get backend statistics.

        Returns:
            Dict with stats including entry_count and memory estimate.
        """
        entry_count = len(self._store)
        # Rough memory estimate
        bytes_used = sys.getsizeof(self._store)
        for entry in self._store.values():
            bytes_used += sys.getsizeof(entry)
            # ``surrogatepass``: stored content may carry lone surrogates
            # (the store accepts them — JSON delivers them via \uD800
            # escapes), and a strict encode would make this stats read
            # raise UnicodeEncodeError. Identical byte counts for all
            # valid-UTF8 content.
            bytes_used += len(entry.original_content.encode("utf-8", "surrogatepass"))
            bytes_used += len(entry.compressed_content.encode("utf-8", "surrogatepass"))

        return {
            "backend_type": "memory",
            "entry_count": entry_count,
            "bytes_used": bytes_used,
        }
