"""Base protocol for CompressionStore backends.

This protocol defines the minimal interface that storage backends must implement.
The interface is intentionally simple - it only handles CRUD operations on entries.
Higher-level concerns (search, feedback, eviction policies) are handled by CompressionStore.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..compression_store import CompressionEntry


# ── Atomic entry-plus-edge linkage (F8, R5) ─────────────────────────────────
#
# Every MAJOR in the F8/N1/M1 series was the same shape: the entry row and its
# derived-link edges were mutated in SEPARATE transactions, so a durable
# intermediate state existed where ``derived_of`` and the edge table disagreed.
# The cure is to make that state unrepresentable: a persist carries the edge
# mutation it must land WITH, in ONE transaction, expressed as this sum type so
# the backend matches it exhaustively. Two public methods a caller could invoke
# separately would keep the half-applied state constructible; one method taking
# an entry and its ``LinkMutation`` cannot.
#
# R6-1: the sum has exactly TWO inhabitants on purpose. There is deliberately no
# "apply an entry write without deciding about edges" case, because that case IS
# the bug expressed as a type: a primary write that leaves edges untouched relies
# on a stale read of the pre-state, so a concurrent derived write of the same hash
# leaves a live primary carrying a stale parent edge. A primary write therefore
# ALWAYS clears its incoming edges, which makes the linkage decision stateless.


@dataclass(frozen=True)
class LinkToParent:
    """Derived-write linkage: this entry is a per-row chunk, so create the edge
    ``parent_hash -> entry`` in the same transaction as the entry upsert."""

    parent_hash: str


@dataclass(frozen=True)
class ClearIncomingLinks:
    """Primary-write linkage: this entry is PRIMARY, so drop EVERY incoming parent
    edge to it in the same transaction as the entry upsert. A primary owns no
    incoming edge by definition, so this is the invariant, not a special case, and
    clearing UNCONDITIONALLY on every primary write is what makes the decision
    stateless: it never reads pre-state, so no concurrent derived write can leave a
    live primary carrying a stale edge (R6-1), and no former parent's cascade can
    delete it (N1)."""


# The sum of the two linkage directions a persist can carry: link to a parent
# (derived) or clear incoming edges (primary). There is no third "leave edges
# alone" case by design; see the R6-1 note above.
LinkMutation = LinkToParent | ClearIncomingLinks


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
            Overwrites any existing entry with the same key. This is NOT the
            persist path for a store() write; see :meth:`set_durable_linked`.
        """
        ...

    def set_durable_linked(
        self, hash_key: str, entry: CompressionEntry, link: LinkMutation
    ) -> bool:
        """Persist an entry AND apply its edge mutation in ONE atomic unit, and
        report whether it reached DURABLE storage. THIS is the store's persist
        path — it is REQUIRED, not optional (R5, R6).

        The entry upsert and the ``link`` mutation MUST commit or roll back
        together, so ``derived_of`` and the derived-link edge table can never be
        observed disagreeing. That single-unit guarantee is the whole point: it is
        why the store never pairs an entry write with :meth:`link_derived` or
        :meth:`unlink_child` by hand, which is the defect this whole series fixed.

        Returns ``True`` iff the write reached durable storage; ``False`` if it
        fell to a volatile tier, in which case the entry AND its edge fall together
        so the tiers never half-apply, and a ``require_durable`` caller vetoes. A
        volatile-only backend (no durable tier to miss) returns ``True``. There is
        NO fallback in the store for a backend lacking this method: it is called
        directly, so a non-conforming backend fails fast and visibly rather than
        silently degrading to a non-atomic two-step path.
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

    def derived_count(self) -> int:
        """Number of stored entries that are DERIVED units of a larger logical
        compression (``CompressionEntry.derived_of is not None``) — the per-row
        chunks a columnar row-drop offloads (F8).

        ``CompressionStore`` subtracts this from :meth:`count` to get the
        LOGICAL entry count it caps on, so one structured compression consumes
        one cap slot instead of ``1 + N`` chunks. TTL is NOT applied here (same
        contract as :meth:`count`): the store filters expiry itself.
        """
        ...

    def link_derived(self, parent_hash: str, child_hash: str) -> None:
        """Record that *child_hash* is a derived per-row chunk of *parent_hash*
        (F8, R1). Idempotent. Because chunks are content-addressed, two columnar
        drops of an identical row share ONE chunk hash under TWO parents; this
        many-to-many edge lets the cascade of one parent leave a chunk another
        live parent still references, and remove it only when the LAST parent
        dies. Called with the store lock held.

        NOT the persist path. This mutates ONLY the edge table, so pairing it with
        an entry write in a SEPARATE transaction is exactly the defect this whole
        series fixed: the two commits could interleave or one could fail, leaving a
        durable state where ``derived_of`` and the edge table disagree, which
        deletes a live primary or strands a chunk. A store() write goes through the
        atomic :meth:`set_durable_linked` instead. This method is for the cascade
        cleanup paths only (eviction and purge), where the entry is already being
        deleted so no live entry can be left disagreeing with its edges.
        """
        ...

    def children_of(self, parent_hash: str) -> list[str]:
        """Every derived chunk *parent_hash* owns (F8) — read from the link edges
        so it returns ALL of a parent's chunks, including ones shared with other
        parents.

        Used by ``CompressionStore`` to cascade a parent's chunks when the parent
        is capacity-evicted or purged, so an evicted parent never leaves an
        independently-retrievable orphan behind. Returns an empty list when the
        parent owns no chunks (the common case for a normal compression). No TTL
        filtering — the store owns expiry.
        """
        ...

    def derived_parents_of(self, child_hash: str) -> list[str]:
        """Every parent that references derived chunk *child_hash* (F8, R1) — the
        candidate owner set the cascade checks for a surviving LIVE parent before
        deleting a shared chunk. May include parents that have since died; the
        store filters those against liveness. No TTL filtering here.
        """
        ...

    def unlink_parent(self, parent_hash: str) -> None:
        """Drop every derived edge owned by *parent_hash* (F8, R1), called when
        the parent is cascaded so a later child-survival check sees only the
        parents that remain. Called with the store lock held."""
        ...

    def unlink_child(self, child_hash: str) -> None:
        """Drop every derived edge pointing at *child_hash* (F8, R1), called when
        the chunk itself is deleted so no dangling edge lingers. Called with the
        store lock held.

        NOT the persist path, for the same reason as :meth:`link_derived`: it
        mutates ONLY the edge table, so hand-pairing it with an entry write in a
        SEPARATE transaction is the exact defect this series fixed, a durable state
        where ``derived_of`` and the edge table disagree that deletes a live primary
        or strands a chunk. A store() write goes through the atomic
        :meth:`set_durable_linked`. This method is for the cascade cleanup paths
        only (eviction and purge), where the chunk's entry is deleted in the same
        step so no live entry is left disagreeing with its edges.
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
