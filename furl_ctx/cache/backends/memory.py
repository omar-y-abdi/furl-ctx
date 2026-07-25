"""In-memory storage backend for CompressionStore.

This is the default backend, providing fast access with no external dependencies.
Data is lost when the process exits.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from .base import ClearIncomingLinks, LinkMutation, LinkToParent

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
        # F8 many-to-many derived link (R1): a per-row chunk is content-addressed,
        # so two columnar drops of an IDENTICAL row share ONE chunk hash under
        # TWO parents. A single ``derived_of`` field cannot record that, so the
        # PARENT<->CHILD relation lives here as a set of edges kept both ways: the
        # cascade of one parent then leaves a chunk that another live parent still
        # references, and only the LAST parent's death removes it.
        self._children_by_parent: dict[str, set[str]] = {}
        self._parents_by_child: dict[str, set[str]] = {}

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

    def set_durable_linked(
        self, hash_key: str, entry: CompressionEntry, link: LinkMutation
    ) -> bool:
        """Atomically upsert the entry AND apply its edge mutation (F8, R5).

        The in-memory backend is volatile, so it reports durability-satisfied
        (True), matching how the store treats the absence of ``set_durable``. The
        two dict mutations run under the store lock with no yield point between
        them, so ``derived_of`` and the edge maps can never be observed disagreeing
        — the same atomicity the SQLite backend gets from one ``with conn:``.
        """
        self._store[hash_key] = entry
        if isinstance(link, LinkToParent):
            self.link_derived(link.parent_hash, hash_key)
        elif isinstance(link, ClearIncomingLinks):
            # Primary write (R6-1): clear every incoming edge. These two cases are
            # the whole LinkMutation union — there is no no-op inhabitant.
            self.unlink_child(hash_key)
        return True

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
        self._children_by_parent.clear()
        self._parents_by_child.clear()

    def increment_counter(self, name: str, amount: int = 1) -> int:
        """Add ``amount`` to the named counter and return its new value.

        Not part of the ``CompressionStoreBackend`` protocol (ARCH-10) — a
        convenience extra for observability, mirroring ``exists``/``keys``. The
        in-memory tally is process-local; the durable SqliteBackend persists the
        same names to the shared file for the cross-process furl_stats picture.
        """
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

    def derived_count(self) -> int:
        """Number of DERIVED entries (``derived_of is not None``) — the per-row
        chunks a columnar row-drop offloads (F8). Subtracted from ``count`` to
        get the logical cap count."""
        return sum(1 for entry in self._store.values() if entry.derived_of is not None)

    def link_derived(self, parent_hash: str, child_hash: str) -> None:
        """Record that *child_hash* is a derived per-row chunk of *parent_hash*
        (F8, R1). Idempotent, kept both ways so the cascade can ask either
        direction. Two parents sharing one content-addressed chunk each add their
        own edge, so neither parent's cascade removes the chunk while the other
        is live."""
        self._children_by_parent.setdefault(parent_hash, set()).add(child_hash)
        self._parents_by_child.setdefault(child_hash, set()).add(parent_hash)

    def children_of(self, parent_hash: str) -> list[str]:
        """Every derived chunk *parent_hash* owns (F8) — from the link edges, so
        it returns ALL of a parent's chunks even ones shared with other parents,
        for cascade delete on eviction/purge."""
        return list(self._children_by_parent.get(parent_hash, ()))

    def derived_parents_of(self, child_hash: str) -> list[str]:
        """Every parent that references derived chunk *child_hash* (F8, R1) — the
        set the cascade checks for a surviving live owner before deleting."""
        return list(self._parents_by_child.get(child_hash, ()))

    def unlink_parent(self, parent_hash: str) -> None:
        """Drop every derived edge owned by *parent_hash* (F8, R1) — called when
        the parent is cascaded so a later child survival check sees only the
        parents that remain."""
        for child in self._children_by_parent.pop(parent_hash, set()):
            parents = self._parents_by_child.get(child)
            if parents is not None:
                parents.discard(parent_hash)
                if not parents:
                    del self._parents_by_child[child]

    def unlink_child(self, child_hash: str) -> None:
        """Drop every derived edge pointing at *child_hash* (F8, R1) — called when
        the chunk itself is deleted so no dangling edge lingers."""
        for parent in self._parents_by_child.pop(child_hash, set()):
            children = self._children_by_parent.get(parent)
            if children is not None:
                children.discard(child_hash)
                if not children:
                    del self._children_by_parent[parent]

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
            # Bound derived-link staleness (R1): an entry that TTL-expires without
            # a cascade would otherwise leave its edges behind. The cascade's
            # survival check already ignores dead parents, so this is a memory
            # bound, not a correctness fix — drop the expired key's edges in both
            # roles.
            self.unlink_parent(key)
            self.unlink_child(key)
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
