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
    backends — exactly the operations ``CompressionStore`` calls;
    implementations may still offer extras beyond it. Implementations
    can use any storage mechanism: memory, SQLite, Redis, etc.

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

    @property
    def max_rows(self) -> int | None:
        """The backend's PHYSICAL row cap, or ``None`` if it imposes none.

        Part of the contract because ``CompressionStore`` checks a documented
        invariant against it (F4): the logical ``max_entries`` cap must bind
        before any physical cap, or the physical one evicts oldest-first with no
        TTL ordering. ``None`` is a positive declaration — "no physical limit, so
        the logical cap always binds first" — NOT an absence.

        Declaring it here is what makes the invariant checkable. The store used
        to read ``getattr(backend, "_max_rows", None)``: a PRIVATE field of one
        implementation, which collapsed three distinct situations into one silent
        skip — no physical cap, field renamed, and never present. A rename would
        have disabled the check permanently with no log, no warning and no
        failing test. A backend that does not declare this is now a type error
        here, and is logged rather than silently skipped at runtime.

        READ-ONLY on purpose (a ``@property``, not a bare ``max_rows: int | None``
        annotation). A bare annotation declares a SETTABLE variable, which is
        invariant: it would both reject ``SqliteBackend``'s narrower ``-> int``
        and imply the store may assign a backend's cap. A read-only property is
        covariant in its return type, so an implementation may legitimately
        promise the narrower ``int``, and the cap stays the backend's to state.
        """
        ...

    @property
    def durable(self) -> bool:
        """Whether this backend persists DURABLY — surviving process restart and
        visible to other processes sharing the same storage.

        A DECLARATION, for the same reason ``max_rows`` is one. ``CompressionStore``
        used to infer durability from the mere PRESENCE of a ``set_durable``
        attribute, which collapsed two opposite situations into one answer: a
        deliberately volatile backend (``InMemoryBackend``, which does not define
        it) and a genuinely DURABLE third-party backend that never implemented an
        undocumented name. Both came back "durability satisfied", so a
        ``require_durable=True`` write never vetoed and a ``<<ccr:HASH>>`` marker
        could ship for content whose durability was never actually checked.

        A backend cannot express "I am durable" by accident, and it can no longer
        fail to express it by omission: this is required, and it is required
        BEFORE ``set_durable``'s result is ever consulted.
        """
        ...

    def set_durable(self, hash_key: str, entry: CompressionEntry) -> bool:
        """Store *entry* and report whether the write reached DURABLE storage.

        Required of every backend so the call site can be a direct attribute
        access that a type checker actually sees. Declaring a name on this
        Protocol while the caller still reaches it through ``getattr`` restores
        nothing: mypy types a ``getattr`` result as ``Any`` and stays silent, so
        the declaration would be documentation rather than enforcement.

        A volatile backend implements this honestly — it stores the entry and
        returns ``False``, because the write did not reach durable storage. The
        store only consults this result when :attr:`durable` is ``True``; a
        backend that declares itself volatile has nothing for ``require_durable``
        to veto, since the operator chose volatile storage deliberately.
        """
        ...

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


def reject_negative_counter_amount(name: str, amount: int) -> None:
    """Raise ``ValueError`` when *amount* would move a named counter backwards.

    The validation rule for the OPTIONAL ``increment_counter`` extra. That method
    is deliberately not a Protocol member (ARCH-10 — it is an observability extra
    alongside ``exists``/``keys``), but every backend that offers it shares this
    one rule, so the rule lives HERE, beside the contract, rather than inside any
    single implementation.

    That placement is the point. It first lived in ``memory.py`` and was imported
    from there by ``sqlite.py`` and by ``CompressionStore`` — the store reaching
    past the contract into one concrete backend's internals, which is precisely
    the defect the ``max_rows`` declaration above exists to remove (there by
    attribute, here by import, and an import rots more quietly). A backend author
    reads this module for the contract; a rule stated anywhere else is a rule
    they have no reason to find.

    Why the rule exists: these counters are MONOTONIC cumulative tallies (hook
    invocations, compressions) that ``furl_stats`` reads, and the cross-process
    rebase depends on that monotonicity. A negative increment has no meaning for
    them and silently corrupts the total it lands in. ``0`` stays legal — a
    computed step of zero is a harmless no-op.

    Raised, not clamped or swallowed: a negative amount is a CALLER BUG, and
    quietly repairing it would hide the bug while leaving the tally wrong. This
    mirrors ``CompressionStore.store``'s ``ttl``/``explicit_hash`` rejections.

    Backends implementing ``increment_counter`` should call this FIRST, before
    any storage-specific work, and callers that wrap them (the store) validate
    independently — see ``CompressionStore.increment_counter`` for why a
    fail-open wrapper cannot rely on the backend's raise reaching anyone.
    """
    if amount < 0:
        raise ValueError(
            f"increment_counter amount must be >= 0 (counters are monotonic "
            f"cumulative tallies), got {amount!r} for counter {name!r}"
        )
