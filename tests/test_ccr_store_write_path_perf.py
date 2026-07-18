"""Audit #2 + #6 — the store() write hot path must not scan the whole shared DB.

#2: ``SqliteBackend.items()`` (``SELECT <all cols incl. BLOB> FROM ccr_entries``,
no WHERE/LIMIT) was reached from ``_clean_expired`` on EVERY ``store()`` — so
every offloading hook compression reloaded and DECODED the entire ≤10 000-row
shared file just to find expired keys.

#6: the purge predicate ``created_at + ttl < ?`` could not use the bare
``created_at`` index (a full scan), and a ``COUNT(*)`` full-scan ran on every put.

Fix: an expression index on ``created_at + ttl`` (indexed range purge), a
maintained row counter (no per-put COUNT(*)), and projected backend reads
(``purge_expired`` / ``created_at_index``) so ``store()`` never materialises the
BLOBs. These are perf-only: eviction order, stats numbers and count() are
byte-identical (pinned by the existing sqlite/store suites).
"""

from __future__ import annotations

import time

from furl_ctx.cache.backends import sqlite as sq
from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import CompressionStore

_N = 1000


def _fill(store: CompressionStore, n: int) -> None:
    for i in range(n):
        store.store(
            original=f"payload-{i}-" + "x" * 80, compressed="c", explicit_hash=f"{i:012x}", ttl=3600
        )


def test_store_does_not_decode_all_blobs(tmp_path, monkeypatch) -> None:
    # Fill a full store, then count full-entry BLOB decodes during ONE more
    # store(). Pre-fix: ~N (the whole store materialised via items() in
    # _clean_expired). Post-fix: O(1) — a collision-check get plus at most a
    # couple of eviction gets.
    store = CompressionStore(
        max_entries=_N, backend=SqliteBackend(db_path=tmp_path / "big.sqlite3")
    )
    _fill(store, _N)

    calls = {"n": 0}
    real = sq._row_to_entry

    def counting(row):
        calls["n"] += 1
        return real(row)

    monkeypatch.setattr(sq, "_row_to_entry", counting)
    store.store(original="fresh", compressed="c", explicit_hash="ffffffffffff", ttl=3600)

    assert calls["n"] < 10, (
        f"store() decoded {calls['n']} full blobs on a {_N}-entry DB — it "
        f"materialised the whole store on the write hot path (audit #2)"
    )


def test_maintained_counter_keeps_count_and_eviction_exact(tmp_path) -> None:
    # The maintained counter (replacing per-put COUNT(*)) must keep the cap and
    # oldest-first eviction exact — count() reads the real file COUNT.
    store = CompressionStore(
        max_entries=_N, backend=SqliteBackend(db_path=tmp_path / "cap.sqlite3")
    )
    _fill(store, _N)
    assert store._backend.count() == _N
    # Over-cap store evicts oldest-first; count stays capped, newest survives.
    store.store(original="over", compressed="c", explicit_hash="ffffffffffff", ttl=3600)
    assert store._backend.count() == _N
    assert store._backend.get("ffffffffffff") is not None  # newest kept
    assert store._backend.get("000000000000") is None  # oldest evicted


def test_expires_index_serves_indexed_purge(tmp_path) -> None:
    # #6: the purge predicate is now an INDEXED range search, not a full scan.
    backend = SqliteBackend(db_path=tmp_path / "idx.sqlite3")
    store = CompressionStore(max_entries=5000, backend=backend)
    _fill(store, 200)

    def inspect(conn):
        idxs = {r[1] for r in conn.execute("PRAGMA index_list(ccr_entries)").fetchall()}
        plan = conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM ccr_entries WHERE created_at + ttl < ?",
            (time.time(),),
        ).fetchall()
        return idxs, " ".join(str(step) for step in plan)

    idxs, plan_text = backend._run("inspect", inspect)
    assert "ccr_entries_expires_at_idx" in idxs, "expiry expression index was not created"
    assert "ccr_entries_expires_at_idx" in plan_text, f"purge did not use the index: {plan_text}"
    assert "SCAN" not in plan_text.upper() or "USING INDEX" in plan_text, (
        f"purge still full-scans: {plan_text}"
    )


def test_purge_expired_reaps_only_expired(tmp_path) -> None:
    # Functional pin for the projected expiry GC used by the store hot path.
    backend = SqliteBackend(db_path=tmp_path / "purge.sqlite3")
    now = time.time()
    store = CompressionStore(max_entries=100, backend=backend, now_fn=lambda: now)
    store.store(original="live", compressed="c", explicit_hash="a" * 12, ttl=3600)
    store.store(original="dead", compressed="c", explicit_hash="b" * 12, ttl=10)
    # Advance the store clock past the short TTL; purge via the backend directly.
    purged = backend.purge_expired(now + 100)
    assert purged == 1
    assert backend.get("b" * 12) is None  # expired reaped
    assert backend.get("a" * 12) is not None  # live kept


def test_created_at_index_matches_items_ordering(tmp_path) -> None:
    # The projected heap-rebuild read must carry the SAME (created_at, hash)
    # pairs items() would — just without decoding the BLOBs.
    backend = SqliteBackend(db_path=tmp_path / "proj.sqlite3")
    store = CompressionStore(max_entries=100, backend=backend)
    _fill(store, 20)
    from_items = sorted((e.created_at, k) for k, e in backend.items())
    from_index = sorted(backend.created_at_index())
    assert from_index == from_items
