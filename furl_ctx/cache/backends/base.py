"""Base protocol for CompressionStore backends.

This protocol defines the minimal interface that storage backends must implement.
The interface is intentionally simple - it only handles CRUD operations on entries.
Higher-level concerns (search, feedback, eviction policies) are handled by CompressionStore.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..compression_store import CompressionEntry


@runtime_checkable
class CompressionStoreBackend(Protocol):
    """Protocol for CompressionStore storage backends.

    This protocol defines the minimal interface for pluggable storage
    backends — exactly the operations ``CompressionStore`` calls
    (ARCH-10: ``keys()``/``exists()`` were required but never called by
    the store and are no longer part of the contract; implementations
    may still offer them as extras). Implementations can use any storage
    mechanism: memory, SQLite, Redis, etc.

    Design Principles:
    - Simple CRUD operations only
    - No business logic (search, feedback, eviction policies)
    - Thread-safety: NOT required of implementations.
      ``CompressionStore`` serializes every backend call under its own
      lock (``CompressionStore._lock``), so a backend needs no internal
      locking for store-mediated use — that is the single ownership
      story (ARCH-10). A backend used directly (outside
      ``CompressionStore``) is not synchronized unless it says
      otherwise; a backend guarding its own OS resources may still keep
      internal state protection for its own invariants (e.g.
      ``SqliteBackend._state_lock`` for connection/degrade state).
    - TTL handling can be delegated to backend or handled by CompressionStore

    Example implementation:
        class MyBackend:
            def get(self, hash_key: str) -> CompressionEntry | None:
                return self._storage.get(hash_key)

            def set(self, hash_key: str, entry: CompressionEntry) -> None:
                self._storage[hash_key] = entry

            # ... other methods
    """

    def get(self, hash_key: str) -> CompressionEntry | None:
        """Retrieve an entry by hash key.

        Args:
            hash_key: The unique hash identifying the entry.

        Returns:
            CompressionEntry if found, None otherwise.
            Does NOT check TTL - that's CompressionStore's responsibility.
        """
        ...

    def set(self, hash_key: str, entry: CompressionEntry) -> None:
        """Store an entry with the given hash key.

        Args:
            hash_key: The unique hash identifying the entry.
            entry: The CompressionEntry to store.

        Note:
            Overwrites any existing entry with the same key.
        """
        ...

    def delete(self, hash_key: str) -> bool:
        """Delete an entry by hash key.

        Args:
            hash_key: The unique hash identifying the entry.

        Returns:
            True if entry was deleted, False if it didn't exist.
        """
        ...

    def clear(self) -> None:
        """Remove all entries from storage."""
        ...

    def count(self) -> int:
        """Get the number of entries in storage.

        Returns:
            Number of entries currently stored.
        """
        ...

    def items(self) -> list[tuple[str, CompressionEntry]]:
        """Get all entries as (hash_key, entry) pairs.

        Returns:
            List of (hash_key, CompressionEntry) tuples.

        Note:
            For large stores, consider implementing an iterator version.
        """
        ...

    def purge_expired(self, now: float) -> int:
        """Delete entries whose per-row TTL elapsed by ``now`` and return the
        count purged.

        Lets ``CompressionStore`` GC expired entries without materializing every
        row into Python just to find the expired keys (audit #2 — the durable
        backend can push this to an indexed range delete). ``now`` is the
        STORE's clock (injectable for tests), NOT the backend's own wall clock,
        so expiry stays consistent with the store's TTL checks.
        """
        ...

    def created_at_index(self) -> list[tuple[float, str]]:
        """Return ``(created_at, hash_key)`` for every entry WITHOUT the content
        BLOBs — the projection ``CompressionStore`` uses to rebuild its eviction
        heap cheaply (audit #2), instead of decoding every full entry via
        ``items()``.
        """
        ...

    def get_stats(self) -> dict[str, Any]:
        """Get backend-specific statistics.

        Returns:
            Dict with backend stats. Should include at minimum:
            - "entry_count": number of entries
            - "backend_type": name of the backend implementation

            Backends may include additional stats like:
            - "bytes_used": memory/storage used
            - "connection_status": for remote backends
        """
        ...
