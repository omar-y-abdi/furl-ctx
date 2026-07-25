"""F8: a columnar row-drop's per-row chunks are DERIVED units — they do not
compete with primary compressions for the store's logical cap, and they cascade
with their parent on eviction or purge.

Report finding 8: one columnar compress created the table entry PLUS one store
entry per offloaded chunk (empirically 1 whole-blob parent + N per-row chunks),
so a handful of structured outputs burned the 1000-entry cap several times
faster than nominal and evicted retrievable markers well inside their TTL. The
fix tags each chunk with ``derived_of=<parent hash>``; the cap counts only
PRIMARY entries and cascade-deletes a parent's chunks when it is evicted/purged.

These pin the store-layer contract on BOTH backends; the end-to-end crusher
linkage (whole-blob parent == index base hash) is covered by
test_store_derived_cap_integration below.
"""

from __future__ import annotations

import hashlib
import json
import threading

import pytest

from furl_ctx.cache.backends.memory import InMemoryBackend
from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import (
    CompressionStore,
    get_compression_store,
    reset_compression_store,
)

PARENT = "a" * 24
CHILD1 = "b" * 24
CHILD2 = "c" * 24


def _mem_store(**kw) -> CompressionStore:
    return CompressionStore(**kw)


def _sqlite_store(tmp_path, **kw) -> CompressionStore:
    return CompressionStore(backend=SqliteBackend(db_path=tmp_path / "f8.sqlite3"), **kw)


# ── backend round-trip of derived_of ────────────────────────────────────────


def test_memory_backend_round_trips_derived_of() -> None:
    b = InMemoryBackend()
    store = CompressionStore(backend=b)
    store.store("child rows", "<<ccr:b>>", explicit_hash=CHILD1, derived_of=PARENT)
    assert b.get(CHILD1).derived_of == PARENT
    assert b.derived_count() == 1
    assert b.children_of(PARENT) == [CHILD1]


def test_sqlite_backend_round_trips_derived_of(tmp_path) -> None:
    b = SqliteBackend(db_path=tmp_path / "rt.sqlite3")
    store = CompressionStore(backend=b)
    store.store("prim", "<<ccr:a>>", explicit_hash=PARENT)
    store.store("child rows", "<<ccr:b>>", explicit_hash=CHILD1, derived_of=PARENT)
    got = b.get(CHILD1)
    assert got is not None and got.derived_of == PARENT
    assert b.get(PARENT).derived_of is None
    assert b.derived_count() == 1
    assert b.children_of(PARENT) == [CHILD1]
    assert b.children_of("f" * 24) == []


def test_sqlite_migration_adds_derived_of_to_preexisting_db(tmp_path) -> None:
    """A database created before the derived_of column exists gets the column
    added on open (idempotent ALTER), and derived writes then round-trip."""
    import sqlite3

    db = tmp_path / "old.sqlite3"
    # Old schema: ccr_entries WITHOUT derived_of (the pre-F8 column set).
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE ccr_entries ("
            "hash_key BLOB PRIMARY KEY, entry_hash BLOB NOT NULL, "
            "original_content BLOB NOT NULL, compressed_content BLOB NOT NULL, "
            "original_tokens INTEGER NOT NULL, compressed_tokens INTEGER NOT NULL, "
            "original_item_count INTEGER NOT NULL, compressed_item_count INTEGER NOT NULL, "
            "tool_name BLOB, tool_call_id BLOB, query_context BLOB, "
            "created_at REAL NOT NULL, ttl INTEGER NOT NULL, compression_strategy BLOB, "
            "retrieval_count INTEGER NOT NULL, search_queries TEXT NOT NULL, last_accessed REAL)"
        )
    # Opening the backend migrates the schema.
    b = SqliteBackend(db_path=db)
    cols = {c[1] for c in sqlite3.connect(db).execute("PRAGMA table_info(ccr_entries)").fetchall()}
    assert "derived_of" in cols, "migration must add the derived_of column"
    store = CompressionStore(backend=b)
    store.store("child", "<<ccr:b>>", explicit_hash=CHILD1, derived_of=PARENT)
    assert b.get(CHILD1).derived_of == PARENT


# ── logical vs derived counts ───────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_logical_and_derived_counts(kind, tmp_path) -> None:
    store = _mem_store() if kind == "memory" else _sqlite_store(tmp_path)
    store.store("p", "<<ccr:a>>", explicit_hash=PARENT)
    store.store("c1", "<<ccr:b>>", explicit_hash=CHILD1, derived_of=PARENT)
    store.store("c2", "<<ccr:c>>", explicit_hash=CHILD2, derived_of=PARENT)
    st = store.get_stats()
    assert st["entry_count"] == 3
    assert st["logical_entry_count"] == 1
    assert st["derived_entry_count"] == 2


# ── the cap counts LOGICAL entries only ─────────────────────────────────────


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_derived_entries_do_not_evict_the_primary(kind, tmp_path) -> None:
    """A tiny cap plus many derived chunks of ONE parent keeps the parent AND
    the chunks live — chunks never count against the cap."""
    store = (
        _mem_store(max_entries=3) if kind == "memory" else _sqlite_store(tmp_path, max_entries=3)
    )
    store.store("parent", "<<ccr:a>>", explicit_hash=PARENT)
    for i in range(50):
        store.store(f"row {i}", f"<<ccr:{i:024x}>>", explicit_hash=f"{i:024x}", derived_of=PARENT)
    st = store.get_stats()
    assert st["logical_entry_count"] == 1, "50 chunks must not evict the 1 primary"
    assert store.exists(PARENT)
    assert store.exists(f"{0:024x}") and store.exists(f"{49:024x}")


# ── a chunk stays retrievable if an OLDER primary evicts during its write ────


def test_chunk_retrievable_when_older_primary_evicts_during_write() -> None:
    """A logical unit stores its parent FIRST (so the parent is the NEWEST
    primary), then its chunks. When storing a chunk drives the logical count to
    the cap, FIFO evicts the OLDEST primary, never this unit's just-stored
    parent, and the chunk itself is inserted AFTER its store()'s eviction pass.
    So the tagging never makes a chunk unretrievable mid-write: the parent and
    every chunk survive, only the unrelated older primary is shed."""
    store = CompressionStore(max_entries=2)
    store.store("old primary", "co", explicit_hash="0" * 24)  # oldest primary → the victim
    store.store("parent blob", "<<ccr:a>>", explicit_hash=PARENT)  # newest primary, logical == cap
    # Storing this chunk evicts the OLDER primary "0", not PARENT (newest).
    store.store("row0", "<<ccr:b>>", explicit_hash=CHILD1, derived_of=PARENT)
    store.store("row1", "<<ccr:c>>", explicit_hash=CHILD2, derived_of=PARENT)
    assert not store.exists("0" * 24), "the OLDER primary is the eviction victim"
    assert store.retrieve(PARENT) is not None, "this unit's parent (newest) survives"
    assert store.retrieve(CHILD1) is not None and store.retrieve(CHILD2) is not None, (
        "chunks stay retrievable through the write — tagging never orphans them mid-write"
    )


# ── eviction of a primary cascades its derived children ─────────────────────


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_evicting_oldest_primary_cascades_children(kind, tmp_path) -> None:
    store = (
        _mem_store(max_entries=2) if kind == "memory" else _sqlite_store(tmp_path, max_entries=2)
    )
    # Parent is the OLDEST primary; add its chunks, then push newer primaries.
    store.store("parent", "<<ccr:a>>", explicit_hash=PARENT)
    store.store("c1", "<<ccr:b>>", explicit_hash=CHILD1, derived_of=PARENT)
    store.store("c2", "<<ccr:c>>", explicit_hash=CHILD2, derived_of=PARENT)
    store.store("P1", "cc", explicit_hash="1" * 24)  # logical -> 2 (== cap)
    store.store("P2", "cc", explicit_hash="2" * 24)  # evict oldest primary = parent
    assert not store.exists(PARENT), "oldest primary evicted"
    assert not store.exists(CHILD1) and not store.exists(CHILD2), "children cascaded"
    assert store.exists("1" * 24) and store.exists("2" * 24)
    st = store.get_stats()
    assert st["logical_entry_count"] == 2 and st["derived_entry_count"] == 0


# ── purge of a parent cascades its derived children ─────────────────────────


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_purging_parent_cascades_children(kind, tmp_path) -> None:
    store = _mem_store() if kind == "memory" else _sqlite_store(tmp_path)
    store.store("parent", "<<ccr:a>>", explicit_hash=PARENT)
    store.store("c1", "<<ccr:b>>", explicit_hash=CHILD1, derived_of=PARENT)
    store.store("c2", "<<ccr:c>>", explicit_hash=CHILD2, derived_of=PARENT)
    outcome = store.delete_cascade_detailed(PARENT)
    assert outcome.top_deleted
    assert set(outcome.nested_deleted) == {CHILD1, CHILD2}
    assert not store.exists(CHILD1) and not store.exists(CHILD2)


# ── once-primary-always-primary (content-collision safety) ──────────────────


def test_derived_write_never_demotes_existing_primary() -> None:
    store = CompressionStore()
    # Same content stored first as PRIMARY, then re-stored as derived.
    store.store("dup content", "<<ccr:a>>", explicit_hash=PARENT)
    store.store("dup content", "<<ccr:a>>", explicit_hash=PARENT, derived_of=CHILD1)
    assert store._backend.get(PARENT).derived_of is None, "primary must stay primary"
    assert store.get_stats()["derived_entry_count"] == 0


def test_primary_write_promotes_existing_derived() -> None:
    store = CompressionStore()
    store.store("dup", "<<ccr:a>>", explicit_hash=PARENT, derived_of=CHILD1)
    assert store._backend.get(PARENT).derived_of == CHILD1
    store.store("dup", "<<ccr:a>>", explicit_hash=PARENT)  # re-store as primary
    assert store._backend.get(PARENT).derived_of is None, "primary write promotes"


# ── validation ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "xyz", "not-hex!", "  "])
def test_non_hex_derived_of_rejected(bad) -> None:
    store = CompressionStore()
    with pytest.raises(ValueError, match="derived_of must be a non-empty hex string"):
        store.store("x", "<<ccr:a>>", explicit_hash=PARENT, derived_of=bad)


# ── regression: no derived_of ⇒ byte-identical cap behavior ─────────────────


def test_no_derived_of_is_byte_identical_eviction() -> None:
    """With zero derived entries the cap and eviction behave exactly as before:
    logical_entry_count == entry_count, and the oldest primary evicts first."""
    store = CompressionStore(max_entries=2)
    store.store("A", "ca", explicit_hash="a" * 24)
    store.store("B", "cb", explicit_hash="b" * 24)
    store.store("C", "cc", explicit_hash="c" * 24)  # evicts A (oldest)
    st = store.get_stats()
    assert st["entry_count"] == st["logical_entry_count"] == 2
    assert st["derived_entry_count"] == 0
    assert not store.exists("a" * 24)
    assert store.exists("b" * 24) and store.exists("c" * 24)


# ── retrieval still works for parent and children ───────────────────────────


def test_parent_and_children_both_retrievable() -> None:
    store = CompressionStore()
    store.store("parent original", "<<ccr:a>>", explicit_hash=PARENT)
    store.store("row zero", "<<ccr:b>>", explicit_hash=CHILD1, derived_of=PARENT)
    assert store.retrieve(PARENT).original_content == "parent original"
    assert store.retrieve(CHILD1).original_content == "row zero"


# ── integration: real columnar row-drop through the crusher ─────────────────


def test_columnar_rowdrop_is_one_logical_unit() -> None:
    """End-to-end: a columnar compress that offloads N rows stores 1 PRIMARY
    whole-blob parent + N DERIVED per-row chunks, so it consumes exactly ONE
    logical cap slot instead of 1 + N."""
    from furl_ctx.transforms.smart_crusher import SmartCrusher, SmartCrusherConfig

    reset_compression_store()
    store = get_compression_store()
    rows = [
        {
            "id": hashlib.sha256(f"{i}".encode()).hexdigest(),
            "service": ["api", "worker"][i % 2],
            "latency_ms": i,
            "message": f"request {i} handled " * 2,
        }
        for i in range(40)
    ]
    SmartCrusher(SmartCrusherConfig()).crush_array_json(json.dumps(rows))
    st = store.get_stats()
    assert st["logical_entry_count"] == 1, "one columnar compress == one logical unit"
    assert st["derived_entry_count"] == st["entry_count"] - 1 >= 1
    reset_compression_store()


# ── concurrency: the derived-cascade path under a shared lock ────────────────


def test_concurrent_primary_and_derived_stores_under_eviction() -> None:
    """Many threads hammer store() with primaries + derived chunks against a
    small cap, forcing concurrent eviction + child-cascade. Proves the new
    derived paths take no re-entrant lock (no deadlock: the test completes),
    raise nothing, and never leave the logical count above the cap."""
    store = CompressionStore(max_entries=8)
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def worker(t: int) -> None:
        try:
            barrier.wait(timeout=30)
            for i in range(150):
                parent = f"{t:02x}{i:022x}"
                store.store(f"p{t}-{i}", "c", explicit_hash=parent)
                for c in range(3):
                    child = f"{t:02x}{c:x}{i:021x}"
                    store.store(f"c{t}-{i}-{c}", "c", explicit_hash=child, derived_of=parent)
                store.get_stats()  # concurrent reader path (get_stats prunes + counts)
        except BaseException as exc:  # noqa: BLE001 — surface any thread failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
        assert not th.is_alive(), "a worker hung — possible deadlock in the derived path"

    assert not errors, f"concurrent stores raised: {errors[:3]}"
    st = store.get_stats()
    assert st["logical_entry_count"] <= 8, "cap must hold under concurrency"
    assert st["derived_entry_count"] >= 0


# ── R1: content-addressed chunks shared by two parents ──────────────────────
#
# Two columnar drops of an IDENTICAL row hash to ONE chunk key, so that chunk is
# referenced by TWO parents. The cascade of one parent must LEAVE the chunk for
# the other live parent (the derived analogue of the RG3 co-reference check) and
# clean it only when the LAST parent dies. A single ``derived_of`` field cannot
# express two owners; the backend link table can, and these pin that contract on
# both backends.

P_A = "a1" + "0" * 22
P_B = "b2" + "0" * 22
SHARED = "5e" + "0" * 22  # the chunk BOTH parents reference (same content -> same hash)
SOLO_A = "d4" + "0" * 22  # a chunk only P_A references
_SHARED_ROW = "identical dropped row bytes"  # same original content under both parents


def _link_two_parents_sharing_a_chunk(store: CompressionStore) -> None:
    """Lay down two parents that share ONE content-addressed chunk, plus one
    chunk P_A alone owns. The cap is wide here, so none of these writes evict:
    BOTH parent->SHARED edges exist before any test triggers a cascade. P_A is
    stored first, so it is the OLDER primary (the eviction victim)."""
    store.store("parent A", "<<ccr:a1>>", explicit_hash=P_A)
    store.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A)
    store.store("solo row of A", "<<ccr:d4>>", explicit_hash=SOLO_A, derived_of=P_A)
    store.store("parent B", "<<ccr:b2>>", explicit_hash=P_B)
    # Same content, same hash: dedups against P_A's chunk and adds the P_B edge.
    store.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_B)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_shared_chunk_survives_purge_of_one_owner(kind, tmp_path) -> None:
    """Purging one parent removes the chunks it ALONE owns but leaves a chunk a
    second live parent still references; purging that LAST parent cleans it."""
    store = _mem_store() if kind == "memory" else _sqlite_store(tmp_path)
    _link_two_parents_sharing_a_chunk(store)

    outcome = store.delete_cascade_detailed(P_A)
    assert outcome.top_deleted
    assert SOLO_A in outcome.nested_deleted, "P_A's sole-owned chunk cascades"
    assert SHARED not in outcome.nested_deleted, "a shared chunk is NOT reported deleted"
    assert not store.exists(P_A) and not store.exists(SOLO_A)
    assert store.retrieve(SHARED) is not None, "shared chunk survives via still-live P_B"
    assert store.exists(P_B)

    outcome_b = store.delete_cascade_detailed(P_B)
    assert outcome_b.top_deleted
    assert SHARED in outcome_b.nested_deleted, "the LAST parent's purge cleans the chunk"
    assert not store.exists(SHARED)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_shared_chunk_survives_eviction_of_one_owner(kind, tmp_path) -> None:
    """Evicting the OLDER parent leaves a chunk the younger parent still owns;
    once the younger parent is evicted too, the last-owner death cleans it."""
    store = (
        _mem_store(max_entries=3) if kind == "memory" else _sqlite_store(tmp_path, max_entries=3)
    )
    _link_two_parents_sharing_a_chunk(store)  # logical == 2 (P_A older, P_B younger)
    assert store.get_stats()["logical_entry_count"] == 2

    store.store("filler C", "cc", explicit_hash="c3" + "0" * 22)  # logical -> 3 (== cap)
    store.store("filler D", "cc", explicit_hash="d3" + "0" * 22)  # evicts oldest primary P_A
    assert not store.exists(P_A), "oldest primary evicted"
    assert not store.exists(SOLO_A), "P_A's sole-owned chunk cascaded with it"
    assert store.retrieve(SHARED) is not None, "shared chunk survives via still-live P_B"
    assert store.exists(P_B)

    store.store("filler E", "cc", explicit_hash="e3" + "0" * 22)  # oldest primary is now P_B
    assert not store.exists(P_B), "second (last) parent evicted"
    assert not store.exists(SHARED), "the LAST parent's eviction cleans the shared chunk"


# ── R2: the store rejects a sub-2 cap (would mis-evict a unit's own parent) ──


def test_max_entries_below_two_is_rejected() -> None:
    """A cap of 1 (or lower) runs the pre-insert eviction pass with no room for a
    parent plus a chunk, so a columnar unit's first chunk write would evict its
    own just-stored parent. The store rejects it at construction instead."""
    for bad in (1, 0, -1):
        with pytest.raises(ValueError, match="max_entries must be at least 2"):
            CompressionStore(max_entries=bad)


def test_min_cap_two_keeps_parent_through_its_first_chunk() -> None:
    """At the documented floor (2) a columnar unit's parent survives its own
    first chunk write — the cap=1 pathology R2 forbids by construction."""
    store = CompressionStore(max_entries=2)
    store.store("parent", "<<ccr:a1>>", explicit_hash=P_A)
    store.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A)
    assert store.retrieve(P_A) is not None, "parent not evicted by its own chunk write"
    assert store.retrieve(SHARED) is not None
    assert store.get_stats()["logical_entry_count"] == 1


# ── N1: a chunk PROMOTED from derived to primary sheds its stale parent edges ─
#
# A per-row chunk can (by content collision) later be re-stored standalone as a
# normal primary compression. The promotion clears derived_of; its OLD parent
# edges MUST go too, or the former parent's eviction or purge cascade would
# delete this now-primary, live entry — a data-availability regression inside
# its TTL, the exact failure class F8 exists to prevent.


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_promoted_chunk_survives_purge_of_former_parent(kind, tmp_path) -> None:
    store = _mem_store() if kind == "memory" else _sqlite_store(tmp_path)
    store.store("parent P", "<<ccr:a1>>", explicit_hash=P_A)
    store.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A)
    assert SHARED in store._backend.children_of(P_A), "edge exists while SHARED is derived"
    # Same content re-stored standalone -> dedups and PROMOTES SHARED to primary.
    store.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED)
    assert store._backend.get(SHARED).derived_of is None, "promoted to primary"
    # N1 mechanism: promotion sheds the stale parent edge (this assertion fails
    # without the fix, and the cascade below would then delete a live primary).
    assert store._backend.children_of(P_A) == [], "promotion shed the stale parent edge"

    outcome = store.delete_cascade_detailed(P_A)
    assert outcome.top_deleted
    assert SHARED not in outcome.nested_deleted, "a promoted primary is not a cascade target"
    assert store.retrieve(SHARED) is not None, "promoted primary survives former parent's purge"


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_promoted_chunk_survives_eviction_of_former_parent(kind, tmp_path) -> None:
    store = (
        _mem_store(max_entries=2) if kind == "memory" else _sqlite_store(tmp_path, max_entries=2)
    )
    store.store("parent P", "<<ccr:a1>>", explicit_hash=P_A)  # oldest primary
    store.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A)  # derived
    store.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED)  # promote -> 2 primaries == cap
    store.store("new primary", "cc", explicit_hash="c3" + "0" * 22)  # evicts oldest primary P_A
    assert not store.exists(P_A), "former parent evicted"
    assert store.retrieve(SHARED) is not None, "promoted primary survives former parent's eviction"


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_promotion_leaves_a_still_derived_sibling_cascading(kind, tmp_path) -> None:
    """Promoting ONE chunk must not disturb a sibling that stays derived: the
    sibling still cascades with the shared parent, the promoted one survives."""
    store = _mem_store() if kind == "memory" else _sqlite_store(tmp_path)
    store.store("parent P", "<<ccr:a1>>", explicit_hash=P_A)
    store.store("still derived", "<<ccr:d4>>", explicit_hash=SOLO_A, derived_of=P_A)
    store.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A)
    store.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED)  # promote SHARED only
    assert store._backend.get(SOLO_A).derived_of == P_A, "sibling stays derived"
    assert store._backend.get(SHARED).derived_of is None, "SHARED promoted"

    outcome = store.delete_cascade_detailed(P_A)
    assert SOLO_A in outcome.nested_deleted, "the still-derived sibling cascades with its parent"
    assert not store.exists(SOLO_A)
    assert store.retrieve(SHARED) is not None, "the promoted chunk is untouched by the cascade"


# ── N2: sqlite backfills link rows for derived entries that predate the table ─


def test_sqlite_backfills_links_for_preexisting_derived_entries(tmp_path) -> None:
    """A store written by the intermediate F8 commit set derived_of but wrote no
    link rows. Opening it under the current code backfills the parent->child edge
    so a parent purge still cascades the old chunk instead of orphaning it."""
    import sqlite3

    db = tmp_path / "prelink.sqlite3"
    store0 = CompressionStore(backend=SqliteBackend(db_path=db))
    store0.store("parent", "<<ccr:a1>>", explicit_hash=P_A)
    store0.store("old chunk", "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A)
    store0.close()

    # Mimic the intermediate on-disk shape: derived_of set, NO link rows.
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM ccr_derived_links")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ccr_entries WHERE derived_of IS NOT NULL"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM ccr_derived_links").fetchone()[0] == 0

    b1 = SqliteBackend(db_path=db)  # _initialize runs the N2 backfill
    assert b1.children_of(P_A) == [SHARED], "backfill reconstructed the parent->child edge"
    store1 = CompressionStore(backend=b1)
    outcome = store1.delete_cascade_detailed(P_A)
    assert SHARED in outcome.nested_deleted, "the backfilled old chunk cascades on purge"
    assert not store1.exists(SHARED)
    store1.close()


# ── M1/M2: the link-table DML must COMMIT, so it survives a close and reopen ───
#
# The three link mutations ran as bare `conn.execute` with no `with conn:`, so
# they sat in an uncommitted transaction that a plain close rolls back. A
# promotion or a link that is the LAST write before close then vanished on the
# next process, reintroducing the N1 data-loss and orphaning chunks across a
# restart. These pin durability in both directions; each is RED without the
# `with conn:` fix and GREEN with it.


def test_promotion_unlink_is_durable_across_restart(tmp_path) -> None:
    db = tmp_path / "promote_durable.sqlite3"
    s0 = CompressionStore(backend=SqliteBackend(db_path=db))
    s0.store("parent P", "<<ccr:a1>>", explicit_hash=P_A)
    s0.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A)
    s0.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED)  # promote: LAST write before close
    s0.close()

    s1 = CompressionStore(backend=SqliteBackend(db_path=db))
    assert s1._backend.get(SHARED).derived_of is None, "still primary after reopen"
    assert s1._backend.children_of(P_A) == [], "the promotion unlink survived the restart"
    s1.delete_cascade_detailed(P_A)  # a routine cascade of the former parent
    assert s1.retrieve(SHARED) is not None, "promoted primary survives the cascade after restart"
    s1.close()


def test_link_derived_is_durable_across_restart(tmp_path) -> None:
    # TWO chunks on purpose. The first edge is flushed to disk by the second
    # store's own commit, so the link table is non-empty on reopen and the
    # M4-guarded backfill is SKIPPED. That is what makes the SECOND edge's
    # durability actually observable: with the backfill masking it, a single-chunk
    # store would be silently reconstructed from derived_of and the pin would pass
    # even with the commit missing. Here nothing reconstructs the second edge, so
    # both edges present proves link_derived committed each write itself.
    db = tmp_path / "link_durable.sqlite3"
    s0 = CompressionStore(backend=SqliteBackend(db_path=db))
    s0.store("parent P", "<<ccr:a1>>", explicit_hash=P_A)
    s0.store("chunk one", "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A)
    s0.store("chunk two", "<<ccr:d4>>", explicit_hash=SOLO_A, derived_of=P_A)  # link: LAST write
    s0.close()

    s1 = CompressionStore(backend=SqliteBackend(db_path=db))
    # Without the commit the second edge is rolled back and, because the links
    # table is non-empty, no backfill restores it: the chunk is never cascade
    # reaped, yet derived_count still reads the committed derived_of column and
    # excludes it from the cap, so it wastes physical space forever. That
    # physical-versus-logical divergence is exactly what this pin catches.
    assert set(s1._backend.children_of(P_A)) == {SHARED, SOLO_A}, "both edges survived the restart"
    assert s1._backend.derived_count() == 2
    outcome = s1.delete_cascade_detailed(P_A)
    assert SHARED in outcome.nested_deleted and SOLO_A in outcome.nested_deleted, "both reaped"
    assert not s1.exists(SHARED) and not s1.exists(SOLO_A)
    s1.close()


# ── R5-2: the on-open sweep reaps child-absent orphans, keeps parent-absent ────


def test_reopen_reaps_child_absent_orphans_but_keeps_parent_absent(tmp_path) -> None:
    """R5-2: a derived chunk deleted by a plain delete or a lazy retrieve-time
    expiry leaves its edge behind with the child row absent. The unconditional
    on-open sweep in _initialize reaps it, EVEN in a zero-expiry regime where the
    old purge-gated sweep never fired. An edge whose PARENT is absent survives,
    since a former parent can return under the same content hash."""
    ghost_parent = "f0" + "0" * 22  # never stored as an entry -> a parent-absent edge
    kept_child = "e7" + "0" * 22

    db = tmp_path / "orphans.sqlite3"
    s0 = CompressionStore(backend=SqliteBackend(db_path=db), max_entries=100)
    b0 = s0._backend
    s0.store("parent P", "<<ccr:a1>>", explicit_hash=P_A, ttl=100_000)
    s0.store("child C", "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A, ttl=100_000)
    # Orphan the edge the way a lazy retrieve-time expiry does: drop the CHILD row
    # directly with no unlink, so P_A -> SHARED lingers with SHARED absent.
    assert b0.delete(SHARED) is True
    assert SHARED in b0.children_of(P_A), "edge lingers after a plain child delete"
    # A parent-absent edge that must be preserved: parent ghost, child present.
    s0.store("kept", "<<ccr:e7>>", explicit_hash=kept_child, derived_of=ghost_parent, ttl=100_000)
    s0.close()

    # Reopen. Nothing expired (long TTLs), so ONLY the unconditional on-open sweep
    # can reap the orphan — the zero-expiry case the gated sweep leaked forever.
    b1 = SqliteBackend(db_path=db)
    assert b1.children_of(P_A) == [], "the child-absent orphan edge was reaped on open"
    assert b1.derived_parents_of(kept_child) == [ghost_parent], "a parent-absent edge survives"
    b1.close()


# ── M4: the N2 backfill runs only when it can do work ──────────────────────────


def test_backfill_skips_when_links_already_present(tmp_path) -> None:
    """An already-linked store must NOT re-run the O(derived-rows) backfill on
    open. Proven by removing ONE edge while leaving the table non-empty: the guard
    sees links present and skips, so the removed edge is not reconstructed."""
    import sqlite3

    db = tmp_path / "linked.sqlite3"
    s0 = CompressionStore(backend=SqliteBackend(db_path=db))
    s0.store("parent", "<<ccr:a1>>", explicit_hash=P_A)
    s0.store("c1", "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A)  # edge P_A -> SHARED
    s0.store("c2", "<<ccr:d4>>", explicit_hash=SOLO_A, derived_of=P_A)  # edge P_A -> SOLO_A
    s0.close()

    # Remove ONE edge; the table stays non-empty, so the guard must skip backfill.
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM ccr_derived_links WHERE child_hash = ?", (SHARED.encode(),))
        assert conn.execute("SELECT COUNT(*) FROM ccr_derived_links").fetchone()[0] == 1

    b1 = SqliteBackend(db_path=db)  # _initialize sees links present -> no backfill
    assert b1.children_of(P_A) == [SOLO_A], "an already-linked store does not re-add the edge"
    b1.close()


# ── R5-4: the entry-plus-edge persist is ATOMIC, no durable half-apply ─────────
#
# A failure of the edge half rolls back the entry half too, because both land in
# ONE transaction. These inject the failure at the REAL _sqlite_set_linked link
# step (via _apply_link_in_txn), so the production ``with conn:`` wrapping is what
# is under test, then reopen a fresh backend to read the durable truth.


def _raise_locked(*_args: object, **_kwargs: object) -> None:
    import sqlite3

    # Lock-contention class: _run retries then fails open, never permanently degrades.
    raise sqlite3.OperationalError("database is locked")


def test_derived_write_has_no_durable_halfapply(tmp_path, monkeypatch) -> None:
    """R5-4b: no durable state where derived_of is SET while the edge is missing.
    A failure at the link step of a derived write rolls back the whole
    transaction, so the child entry is never durably written at all."""
    db = tmp_path / "atomic_derived.sqlite3"
    s0 = CompressionStore(backend=SqliteBackend(db_path=db))
    s0.store("parent P", "<<ccr:a1>>", explicit_hash=P_A)  # durable primary
    monkeypatch.setattr(s0._backend, "_apply_link_in_txn", _raise_locked)
    # Derived write of X under P; non-durable, so it lands volatile only, no veto.
    s0.store("chunk X", "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A)
    s0.close()

    b = SqliteBackend(db_path=db)  # fresh: read the durable truth
    got = b.get(SHARED)
    forbidden = got is not None and got.derived_of is not None and SHARED not in b.children_of(P_A)
    assert not forbidden, "durable half-apply: derived_of set while its edge is missing"
    assert got is None and b.children_of(P_A) == [], "the whole op rolled back on disk"
    b.close()


def test_promotion_has_no_durable_halfapply(tmp_path, monkeypatch) -> None:
    """R5-4a: no durable state where derived_of is NULL while an incoming edge
    remains. A failure at the link step of a promotion rolls back the whole
    transaction, so the chunk stays durably derived WITH its edge."""
    db = tmp_path / "atomic_promote.sqlite3"
    s0 = CompressionStore(backend=SqliteBackend(db_path=db))
    s0.store("parent P", "<<ccr:a1>>", explicit_hash=P_A)
    s0.store(
        _SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A
    )  # durable derived + edge
    monkeypatch.setattr(s0._backend, "_apply_link_in_txn", _raise_locked)
    # Promote X to primary (dedup on identical content); non-durable, volatile only.
    s0.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED)
    s0.close()

    b = SqliteBackend(db_path=db)  # fresh: read the durable truth
    got = b.get(SHARED)
    forbidden = got is not None and got.derived_of is None and SHARED in b.children_of(P_A)
    assert not forbidden, "durable half-apply: derived_of NULL while an incoming edge remains"
    assert got is not None and got.derived_of == P_A and b.children_of(P_A) == [SHARED], (
        "the whole op rolled back on disk: still derived WITH its edge"
    )
    b.close()


def test_promotion_under_contention_vetoes_not_silent_success(tmp_path) -> None:
    """R5-4c: a promotion whose unlink cannot reach durable storage under
    sustained contention must VETO, never return success over a durable
    inconsistency, and must leave the durable state consistent."""
    from furl_ctx.cache.backends.sqlite import _SqliteOpFailed
    from furl_ctx.cache.compression_store import DurableWriteError

    db = tmp_path / "veto_promote.sqlite3"
    b = SqliteBackend(db_path=db)
    store = CompressionStore(
        backend=b,
        durable_retry_attempts=1,
        durable_retry_base_backoff_seconds=0.001,
        durable_retry_max_backoff_seconds=0.001,
    )
    store.store("parent P", "<<ccr:a1>>", explicit_hash=P_A)
    store.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A)  # durable derived

    # Every linked persist now loses the lock race: sustained contention on the unlink.
    real_run = b._run

    def failing_run(op_name: str, fn: object) -> object:
        if op_name == "set_linked":
            raise _SqliteOpFailed()
        return real_run(op_name, fn)  # type: ignore[arg-type]

    b._run = failing_run  # type: ignore[method-assign]

    with pytest.raises(DurableWriteError):
        store.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED, require_durable=True)
    store.close()

    b2 = SqliteBackend(db_path=db)  # fresh: the promotion rolled back durably
    got = b2.get(SHARED)
    assert got is not None and got.derived_of == P_A and b2.children_of(P_A) == [SHARED], (
        "contended promotion left the durable state consistent, no silent success"
    )
    b2.close()


# ── R6-1: the linkage DECISION is STATELESS — a primary write ALWAYS clears its
# incoming edges, so the cross-process stale-read window is closed ─────────────
#
# The application of the edge is atomic (R5). R6-1 closes the remaining hole: the
# DECISION of WHICH edge to apply. Cross-process, a primary writer A reads
# get(X)=None; a derived writer B then commits X-derived plus edge P->X; A then
# commits X-primary. If A's decision were "a primary that saw no prior entry
# touches no edge" (the deleted NoLinkChange), A's write would leave B's edge P->X
# on a now-PRIMARY, live X, and a later cascade of the former parent P would delete
# that live primary. The fix makes the decision depend on the entry ALONE: every
# primary write clears its incoming edges unconditionally, so it never consults a
# read that a concurrent writer could stale.
#
# These drive two SqliteBackend instances on ONE shared file across a real
# close/reopen, and pin the invariant in BOTH commit orders. The durable edge with
# its child ENTRY absent is the deterministic stand-in for the stale-read window:
# the edge is B's committed derived write, and X's absence is exactly what A's
# get(X) returns None on. To prove RED, neutralize the ClearIncomingLinks DELETE in
# _apply_link_in_txn (restore the old "primary touches no edge") and both fire.


def test_primary_write_clears_stale_incoming_edge_across_reopen(tmp_path) -> None:
    """R6-1: a PRIMARY write of X drops any incoming parent edge unconditionally,
    so a durable edge left by a prior derived incarnation of the same hash cannot
    survive on the live primary — and no cascade of the former parent deletes it.
    RED under the old stateless-less decision (a primary that read no prior entry
    left the edge)."""
    db = tmp_path / "r6_stale_edge.sqlite3"
    b0 = SqliteBackend(db_path=db)
    s0 = CompressionStore(backend=b0, enable_feedback=False)

    # A derived write of X under P commits the durable edge P -> X (this is writer
    # B's contribution in the cross-process race).
    s0.store("parent P", "<<ccr:a1>>", explicit_hash=P_A)
    s0.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A)
    # Force the stale-read window deterministically: X's ENTRY goes away while its
    # edge stays, so the imminent primary write's get(X) returns None exactly as
    # writer A's did before it saw B's entry. A plain backend delete leaves the edge
    # (it does not unlink), same shape as an evicted/deleted derived child.
    assert b0.delete(SHARED) is True
    assert SHARED in b0.children_of(P_A), "edge P->X lingers after the entry is gone"

    # Writer A commits X as PRIMARY. get(X) is None, so the OLD decision would leave
    # the edge; the stateless decision clears every incoming edge unconditionally.
    s0.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED)
    assert s0._backend.get(SHARED).derived_of is None, "X is now primary"
    s0.close()

    # Read the durable truth from a fresh instance. The on-open orphan sweep is
    # child-ABSENT only; X's entry is present (primary), so a surviving edge would
    # NOT be swept — the assertion catches a real stale edge, not a sweep artifact.
    s1 = CompressionStore(backend=SqliteBackend(db_path=db), enable_feedback=False)
    assert s1._backend.children_of(P_A) == [], "primary write cleared the stale incoming edge"
    # The consequence R6-1 prevents: the cascade of the former parent must NOT
    # delete the live primary X.
    s1.delete_cascade_detailed(P_A)
    assert s1.retrieve(SHARED) is not None, "the live primary survives the former parent's cascade"
    s1.close()


def test_derived_write_over_durable_primary_strands_no_edge(tmp_path) -> None:
    """R6-1 (reverse order): when the primary write lands FIRST and the derived
    write of the same hash arrives second, the once-primary guard keeps X primary
    and the stateless clear adds no incoming edge — so neither commit order strands
    a stale edge on a live primary. Safe by construction; this pins no regression."""
    db = tmp_path / "r6_reverse.sqlite3"
    s0 = CompressionStore(backend=SqliteBackend(db_path=db), enable_feedback=False)
    s0.store("parent P", "<<ccr:a1>>", explicit_hash=P_A)
    s0.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED)  # X primary FIRST, no edge
    s0.close()

    # The derived write of the same hash arrives second (writer B, second commit).
    s1 = CompressionStore(backend=SqliteBackend(db_path=db), enable_feedback=False)
    s1.store(_SHARED_ROW, "<<ccr:5e>>", explicit_hash=SHARED, derived_of=P_A)
    assert s1._backend.get(SHARED).derived_of is None, "once-primary guard keeps X primary"
    assert s1._backend.children_of(P_A) == [], "no incoming edge was stranded on the live primary"
    s1.close()

    s2 = CompressionStore(backend=SqliteBackend(db_path=db), enable_feedback=False)
    assert s2._backend.children_of(P_A) == [], "still no stale edge after reopen"
    s2.delete_cascade_detailed(P_A)
    assert s2.retrieve(SHARED) is not None, (
        "the live primary survives the cascade in this order too"
    )
    s2.close()


# ── R6-4: ``set_durable_linked`` is a REQUIRED backend method — the store calls it
# directly, with NO getattr fallback, so a non-conforming backend fails fast ─────


class _NoDurableLinkedBackend:
    """A backend that implements the CRUD contract but PREDATES the required
    ``set_durable_linked`` persist method. Everything delegates to an in-memory
    backend; ``set_durable_linked`` is deliberately ABSENT so the store must fail
    fast and visibly at the persist call, rather than silently degrading to the
    non-atomic entry-then-edge path this series removed (R6-4)."""

    def __init__(self) -> None:
        self._mem = InMemoryBackend()

    def __getattr__(self, name: str) -> object:
        if name in ("_mem", "set_durable_linked"):
            # _mem guards pre-__init__ recursion; set_durable_linked is the missing
            # required method whose absence must surface, not be delegated away.
            raise AttributeError(name)
        return getattr(self._mem, name)


def test_persist_requires_set_durable_linked_and_fails_fast() -> None:
    """R6-4: the store's persist path calls ``set_durable_linked`` directly with no
    getattr fallback, so a backend lacking it raises at the call site instead of
    quietly taking a non-atomic two-step write that could half-apply."""
    store = CompressionStore(backend=_NoDurableLinkedBackend(), enable_feedback=False)
    with pytest.raises(AttributeError, match="set_durable_linked"):
        store.store(original="o", compressed="c", explicit_hash="f" * 12)
