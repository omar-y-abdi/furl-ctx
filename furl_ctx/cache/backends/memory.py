"""In-memory storage backend for CompressionStore.

This is the default backend, providing fast access with no external dependencies.
Data is lost when the process exits.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from .base import reject_negative_counter_amount

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
        # Named observability counters (hook invocations/compressions, etc.).
        # PROCESS-LOCAL here — the in-memory backend is volatile and not shared
        # across processes, so these count only THIS process's activity. The
        # durable SqliteBackend persists the same names to the shared file, which
        # is what lets furl_stats see the hook's cross-process increments.
        self._counters: dict[str, int] = {}

    @property
    def max_rows(self) -> int | None:
        """Physical row cap for this backend — always ``None``: there is none.

        The DECLARED half of the cap-ordering contract ``CompressionStore`` checks
        (F4). ``None`` is a positive statement — "this backend imposes no physical
        row limit, so the logical ``max_entries`` cap always binds first" — not an
        absence. The store distinguishes this declaration from a backend that
        declares nothing at all, so its guard staying silent here is observably
        different from its guard being unable to see anything.

        Replaces the store reaching for a private ``_max_rows`` that this class
        never had: that ``getattr(..., None)`` could not tell "no cap" from "cap
        renamed / not exposed", so the invariant check silently no-opped for every
        backend but one.
        """
        return None

    @property
    def durable(self) -> bool:
        """Always ``False``: this backend is volatile by construction.

        A positive declaration, not an omission. The store used to infer
        durability from whether a backend happened to define ``set_durable``,
        which read this class's silence as "durability satisfied" — the same
        answer it gave a genuinely durable third-party backend that never
        implemented that undocumented name. Declaring ``False`` says what is
        true, and leaves no way for another backend's silence to mean the same
        thing.
        """
        return False

    def set_durable(self, hash_key: str, entry: CompressionEntry) -> bool:
        """Store *entry* and report ``False`` — this backend has no durable tier.

        The store does not consult this result while :attr:`durable` is ``False``
        (a volatile backend is what the operator asked for, so ``require_durable``
        has nothing to veto). It is implemented, and answers honestly, so the
        method is never the thing that distinguishes a durable backend from a
        volatile one — the declaration above is.
        """
        self.set(hash_key, entry)
        return False

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
        # A full reset clears observability counters too, so test isolation
        # (reset_compression_store) and furl_purge(all) start from a clean slate.
        self._counters.clear()

    def increment_counter(self, name: str, amount: int = 1) -> int:
        """Add ``amount`` to the named counter and return its new value.

        Not part of the ``CompressionStoreBackend`` protocol (ARCH-10) — a
        convenience extra for observability, mirroring ``exists``/``keys``. The
        in-memory tally is process-local; the durable SqliteBackend persists the
        same names to the shared file for the cross-process furl_stats picture.

        A negative ``amount`` raises ``ValueError`` — see
        :func:`~furl_ctx.cache.backends.base.reject_negative_counter_amount`.
        """
        reject_negative_counter_amount(name, amount)
        self._counters[name] = self._counters.get(name, 0) + amount
        return self._counters[name]

    def get_counters(self) -> dict[str, int]:
        """Snapshot of all named counters (a copy — callers cannot mutate state)."""
        return dict(self._counters)

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
