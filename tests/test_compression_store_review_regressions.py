"""Regression pins for PR review findings in the CCR spill/cascade paths."""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

import pytest

from furl_ctx.cache.backends.memory import InMemoryBackend
from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import (
    CollisionSafetyError,
    CompressionEntry,
    CompressionStore,
    format_retrieval_miss_detail,
)

OLD_HASH = "a" * 24
FILLER_HASH = "b" * 24
CHILD_HASH = "c" * 24
PARENT_HASH = "d" * 24
SECOND_CHILD_HASH = "e" * 24


class _DictSpill:
    """Minimal spill backend for deterministic cross-tier collision tests."""

    def __init__(self) -> None:
        self.data: dict[str, CompressionEntry] = {}
        self.fail_get = False
        self.fail_delete = False

    def get(self, hash_key: str) -> CompressionEntry | None:
        if self.fail_get:
            raise RuntimeError("spill get boom")
        return self.data.get(hash_key)

    def set(self, hash_key: str, entry: CompressionEntry) -> None:
        self.data[hash_key] = entry

    def delete(self, hash_key: str) -> bool:
        if self.fail_delete:
            raise RuntimeError("spill delete boom")
        return self.data.pop(hash_key, None) is not None

    def items(self) -> list[tuple[str, CompressionEntry]]:
        return list(self.data.items())


class _UnreadableSpill:
    """Spill whose parent row cannot be inspected but whose delete would succeed."""

    def __init__(self, parent_hash: str) -> None:
        self.parent_hash = parent_hash
        self.delete_calls: list[str] = []

    def get(self, hash_key: str) -> CompressionEntry | None:
        if hash_key == self.parent_hash:
            raise RuntimeError("transient spill read failure")
        return None

    def delete(self, hash_key: str) -> bool:
        self.delete_calls.append(hash_key)
        return True


class _UnreadableNestedSpill:
    """Spill that becomes unreadable only when preflight reaches a child."""

    def __init__(self, unreadable_hash: str) -> None:
        self.unreadable_hash = unreadable_hash
        self.delete_calls: list[str] = []

    def get(self, hash_key: str) -> CompressionEntry | None:
        if hash_key == self.unreadable_hash:
            raise RuntimeError("nested spill read failure")
        return None

    def delete(self, hash_key: str) -> bool:
        self.delete_calls.append(hash_key)
        return False


class _OneShotVerificationSpill(_DictSpill):
    """Readable during preflight, then fails exactly one post-delete verification read."""

    def __init__(self, fail_hash: str) -> None:
        super().__init__()
        self.fail_hash = fail_hash
        self._reads: dict[str, int] = {}

    def get(self, hash_key: str) -> CompressionEntry | None:
        count = self._reads.get(hash_key, 0) + 1
        self._reads[hash_key] = count
        if hash_key == self.fail_hash and count == 2:
            raise RuntimeError("one-shot verification outage")
        return self.data.get(hash_key)


class _UnavailableSpill:
    """A spill tier whose reads/index are temporarily unavailable."""

    durable = False
    max_rows = None

    def get(self, hash_key: str) -> CompressionEntry | None:
        raise RuntimeError("spill unavailable")

    def set(self, hash_key: str, entry: CompressionEntry) -> None:
        raise RuntimeError("spill unavailable")

    def delete(self, hash_key: str) -> bool:
        raise RuntimeError("spill unavailable")

    def clear(self) -> None:
        raise RuntimeError("spill unavailable")

    def count(self) -> int:
        raise RuntimeError("spill unavailable")

    def items(self) -> list[tuple[str, CompressionEntry]]:
        raise RuntimeError("spill unavailable")

    def created_at_index(self) -> list[tuple[float, str]]:
        raise RuntimeError("spill unavailable")

    def set_durable(self, hash_key: str, entry: CompressionEntry) -> bool:
        raise RuntimeError("spill unavailable")


def _entry(hash_key: str, original: str, compressed: str = "view") -> CompressionEntry:
    return CompressionEntry(
        hash=hash_key,
        original_content=original,
        compressed_content=compressed,
        original_tokens=1,
        compressed_tokens=1,
        original_item_count=1,
        compressed_item_count=1,
        tool_name=None,
        tool_call_id=None,
        query_context=None,
        created_at=time.time(),
        ttl=3600,
    )


def test_store_rejects_different_live_binding_already_in_spill() -> None:
    """A stale spill value must never reappear after a newer same-key binding."""
    spill = _DictSpill()
    store = CompressionStore(
        max_entries=1,
        backend=InMemoryBackend(),
        spill=spill,  # type: ignore[arg-type]
        enable_feedback=False,
    )

    store.store("old-A", "old view", explicit_hash=OLD_HASH)
    store.store("filler", "filler view", explicit_hash=FILLER_HASH)
    assert OLD_HASH in spill.data, "precondition: old binding was demoted to spill"

    # Rebinding the same explicit key to different content is a collision even
    # when the older binding lives only in spill. The collision contract drops
    # the ambiguous key rather than serving either producer's foreign bytes.
    store.store("new-B", "new view", explicit_hash=OLD_HASH)

    assert store.retrieve(OLD_HASH) is None
    assert OLD_HASH not in spill.data


def test_store_fails_closed_when_explicit_hash_spill_inspection_errors() -> None:
    """An unreadable spill cannot be treated as proof that an explicit key is free."""
    spill = _DictSpill()
    store = CompressionStore(
        max_entries=1,
        backend=InMemoryBackend(),
        spill=spill,  # type: ignore[arg-type]
        enable_feedback=False,
    )
    store.store("old-A", "old view", explicit_hash=OLD_HASH)
    store.store("filler", "filler view", explicit_hash=FILLER_HASH)
    assert OLD_HASH in spill.data

    spill.fail_get = True
    with pytest.raises(CollisionSafetyError, match="could not be inspected"):
        store.store("new-B", "new view", explicit_hash=OLD_HASH)

    spill.fail_get = False
    recovered = store.retrieve(OLD_HASH)
    assert recovered is not None
    assert recovered.original_content == "old-A"


def test_store_fails_closed_when_collision_spill_cleanup_errors() -> None:
    """A failed spill delete vetoes the new binding instead of serving foreign bytes."""
    spill = _DictSpill()
    store = CompressionStore(
        max_entries=1,
        backend=InMemoryBackend(),
        spill=spill,  # type: ignore[arg-type]
        enable_feedback=False,
    )
    store.store("old-A", "old view", explicit_hash=OLD_HASH)
    store.store("filler", "filler view", explicit_hash=FILLER_HASH)
    assert OLD_HASH in spill.data

    spill.fail_delete = True
    with pytest.raises(CollisionSafetyError, match="cleanup could not be verified"):
        store.store("new-B", "new view", explicit_hash=OLD_HASH)

    spill.fail_delete = False
    recovered = store.retrieve(OLD_HASH)
    assert recovered is not None
    assert recovered.original_content == "old-A"


def test_delete_cascade_aborts_before_delete_when_spill_markers_are_unreadable() -> None:
    """Unreadable spill marker discovery must fail closed before any purge."""
    store = CompressionStore(enable_feedback=False)
    store.store("child original", "child view", explicit_hash=CHILD_HASH)
    store.store(
        "parent original",
        f"parent view <<ccr:{CHILD_HASH}>>",
        explicit_hash=PARENT_HASH,
    )

    # Make the parent spill-only, then simulate a transient read failure. The
    # spill advertises a successful delete to pin the old unsafe behavior: it
    # would delete the parent after failing to discover CHILD_HASH.
    assert store._backend.delete(PARENT_HASH) is True
    spill = _UnreadableSpill(PARENT_HASH)
    store._spill = spill  # type: ignore[assignment]

    outcome = store.delete_cascade_detailed(PARENT_HASH)

    assert outcome.top_deleted is False
    assert store.exists(CHILD_HASH) is True
    assert spill.delete_calls == [], "cascade must abort before deleting the unreadable parent"


def test_delete_cascade_preflights_nested_nodes_before_parent_mutation() -> None:
    """A child spill read failure aborts before the readable parent is deleted."""
    store = CompressionStore(enable_feedback=False)
    store.store("child original", "child view", explicit_hash=CHILD_HASH)
    store.store(
        "parent original",
        f"parent view <<ccr:{CHILD_HASH}>>",
        explicit_hash=PARENT_HASH,
    )
    spill = _UnreadableNestedSpill(CHILD_HASH)
    store._spill = spill  # type: ignore[assignment]

    outcome = store.delete_cascade_detailed(PARENT_HASH)

    assert outcome.top_deleted is False
    assert store.exists(PARENT_HASH) is True
    assert store.exists(CHILD_HASH) is True
    assert spill.delete_calls == [], "preflight failure must happen before any mutation"


def test_delete_cascade_stops_when_parent_survives_spill_delete_failure() -> None:
    """A surviving spill parent must keep its referenced child reachable."""
    spill = _DictSpill()
    store = CompressionStore(
        backend=InMemoryBackend(),
        spill=spill,  # type: ignore[arg-type]
        enable_feedback=False,
    )
    store.store("child original", "child view", explicit_hash=CHILD_HASH)
    store.store(
        "parent original",
        f"parent view <<ccr:{CHILD_HASH}>>",
        explicit_hash=PARENT_HASH,
    )

    parent = store._backend.get(PARENT_HASH)
    assert parent is not None
    spill.data[PARENT_HASH] = parent
    assert store._backend.delete(PARENT_HASH) is True

    # Preflight can read the parent, but mutation cannot remove its spill copy.
    # The unsafe implementation added PARENT_HASH to ``visited`` first, ignored
    # that still-live parent during co-reference checks, and then deleted CHILD.
    spill.fail_delete = True
    outcome = store.delete_cascade_detailed(PARENT_HASH)

    assert outcome.top_deleted is False
    assert store.exists_any_tier(PARENT_HASH) is True
    assert store.exists_any_tier(CHILD_HASH) is True
    assert outcome.nested_deleted == ()
    assert outcome.failed_hashes == (PARENT_HASH,)


def test_available_miss_status_never_claims_the_hash_is_missing() -> None:
    """A read discrepancy must not be formatted as an eviction/missing claim."""
    detail = format_retrieval_miss_detail({"status": "available"})

    assert "available" in detail.lower()
    assert "No entry for this hash" not in detail


def test_mcp_query_no_match_on_spill_only_entry_is_empty_success() -> None:
    """A spill-only entry with zero query hits is still an available entry."""
    pytest.importorskip("mcp")
    from furl_ctx.ccr.mcp_server import FurlMCPServer, SessionStats

    spill = InMemoryBackend()
    store = CompressionStore(
        max_entries=1,
        backend=InMemoryBackend(),
        spill=spill,
        enable_feedback=False,
    )
    store.store("alpha beta gamma", "alpha view", explicit_hash=OLD_HASH)
    store.store("filler payload", "filler view", explicit_hash=FILLER_HASH)
    assert store.exists(OLD_HASH) is False
    assert store.exists_any_tier(OLD_HASH) is True

    server = object.__new__(FurlMCPServer)
    server._local_store = store
    server._stats = SessionStats()

    result = server._retrieve_content_sync(OLD_HASH, "absent-term")

    assert "error" not in result
    assert result["hash"] == OLD_HASH
    assert result["results"] == []
    assert result["count"] == 0
    assert "available" in result["note"].lower()


def test_sqlite_degraded_clear_cannot_report_verified_empty(tmp_path: Any) -> None:
    """Fail-open SQLite CRUD must not be accepted as proof that durable rows were erased."""
    db_path = tmp_path / "primary.sqlite3"
    backend = SqliteBackend(db_path=db_path, max_rows=100)
    store = CompressionStore(backend=backend, enable_feedback=False)
    store.store("durable parent", "view", explicit_hash=PARENT_HASH)

    # Fault injection through the backend's real degraded channel. The durable
    # file still contains the row, while ordinary clear()/count() fall back to
    # the empty volatile overlay on the old implementation.
    backend._degraded = True
    residual = store.clear()

    with sqlite3.connect(db_path) as conn:
        durable_rows = int(conn.execute("SELECT COUNT(*) FROM ccr_entries").fetchone()[0])
    assert durable_rows == 1, "precondition: degradation prevented the durable erase"
    assert residual > 0, "an unprovably-empty durable primary must fail closed"


def test_sqlite_degraded_cascade_preflight_is_indeterminate_not_absent(tmp_path: Any) -> None:
    """A durable-primary outage cannot be interpreted as an empty marker graph."""
    db_path = tmp_path / "cascade.sqlite3"
    backend = SqliteBackend(db_path=db_path, max_rows=100)
    store = CompressionStore(backend=backend, enable_feedback=False)
    store.store("child", "child view", explicit_hash=CHILD_HASH)
    store.store("parent", f"parent <<ccr:{CHILD_HASH}>>", explicit_hash=PARENT_HASH)

    backend._degraded = True
    outcome = store.delete_cascade_detailed(PARENT_HASH)

    assert outcome.top_deleted is False
    assert outcome.failed_hashes == (PARENT_HASH,)
    with sqlite3.connect(db_path) as conn:
        keys = {
            bytes(row[0]).decode("utf-8")
            for row in conn.execute("SELECT hash_key FROM ccr_entries").fetchall()
        }
    assert {PARENT_HASH, CHILD_HASH} <= keys


def test_one_shot_verification_outage_remains_a_sticky_purge_failure() -> None:
    """A transient post-delete read failure must not later erase the incomplete-cascade fact."""
    pytest.importorskip("mcp")
    from furl_ctx.ccr.mcp_server import FurlMCPServer

    spill = _OneShotVerificationSpill(PARENT_HASH)
    store = CompressionStore(
        backend=InMemoryBackend(),
        spill=spill,  # type: ignore[arg-type]
        enable_feedback=False,
    )
    store.store("child", "child view", explicit_hash=CHILD_HASH)
    store.store("parent", f"parent <<ccr:{CHILD_HASH}>>", explicit_hash=PARENT_HASH)
    parent = store._backend.get(PARENT_HASH)
    assert parent is not None
    assert store._backend.delete(PARENT_HASH) is True
    spill.data[PARENT_HASH] = parent

    server = object.__new__(FurlMCPServer)
    server._local_store = store
    _deleted, _nested, survivors, _shared = server._purge_one(PARENT_HASH)

    assert PARENT_HASH in survivors, "transient uncertainty must remain sticky to the purge result"
    assert store.exists_any_tier(CHILD_HASH) is True, "the unvisited child still exists"


def test_mcp_query_surfaces_spill_unavailability_instead_of_no_match() -> None:
    """A query that could not read the spill is not an authoritative zero-match search."""
    pytest.importorskip("mcp")
    from furl_ctx.ccr.mcp_server import FurlMCPServer, SessionStats

    store = CompressionStore(
        backend=InMemoryBackend(),
        spill=_UnavailableSpill(),  # type: ignore[arg-type]
        enable_feedback=False,
    )
    server = object.__new__(FurlMCPServer)
    server._local_store = store
    server._stats = SessionStats()

    result = server._retrieve_content_sync(OLD_HASH, "needle")

    assert result.get("status") == "unavailable"
    assert "error" in result
    assert "unavailable" in result["error"].lower()


def test_mcp_full_retrieve_surfaces_spill_unavailability() -> None:
    """A no-query read outage must not be formatted as eviction/absence."""
    pytest.importorskip("mcp")
    from furl_ctx.ccr.mcp_server import FurlMCPServer, SessionStats

    store = CompressionStore(
        backend=InMemoryBackend(),
        spill=_UnavailableSpill(),  # type: ignore[arg-type]
        enable_feedback=False,
    )
    server = object.__new__(FurlMCPServer)
    server._local_store = store
    server._stats = SessionStats()

    result = server._retrieve_content_sync(OLD_HASH, None)

    assert result.get("status") == "unavailable"
    assert "unavailable" in result["error"].lower()
    assert "No entry for this hash" not in result["error"]


def test_cross_store_search_marks_spill_index_outage_partial() -> None:
    """Cross-store search must never call an incomplete scan an authoritative no-match."""
    pytest.importorskip("mcp")
    from furl_ctx.ccr.mcp_server import FurlMCPServer

    store = CompressionStore(
        backend=InMemoryBackend(),
        spill=_UnavailableSpill(),  # type: ignore[arg-type]
        enable_feedback=False,
    )
    server = object.__new__(FurlMCPServer)
    server._local_store = store

    result = server._search_all_content_sync("needle")

    assert result.get("partial") is True
    assert "error" in result, "zero hits from an incomplete scan cannot be presented as no-match"
    assert "incomplete" in result["error"].lower() or "unavailable" in result["error"].lower()


def test_divergent_legacy_unbound_rows_are_never_served_as_foreign_content(
    tmp_path: Any,
) -> None:
    """Pre-upgrade same-key replicas with different bytes are quarantined."""
    spill = SqliteBackend(db_path=tmp_path / "legacy-spill.sqlite3", max_rows=100)
    spill.set(OLD_HASH, _entry(OLD_HASH, "legacy-A"))
    primary = InMemoryBackend()
    primary.set(OLD_HASH, _entry(OLD_HASH, "legacy-B"))
    store = CompressionStore(
        backend=primary,
        spill=spill,
        enable_feedback=False,
    )

    assert store.retrieve(OLD_HASH) is None
    status = store.get_entry_status(OLD_HASH)
    assert status["status"] == "unsafe"


def test_sqlite_binding_claim_is_atomic_across_backend_instances(tmp_path: Any) -> None:
    """Two processes sharing one DB cannot both claim one explicit key for different bytes."""
    db_path = tmp_path / "binding.sqlite3"
    left = SqliteBackend(db_path=db_path, max_rows=100)
    right = SqliteBackend(db_path=db_path, max_rows=100)

    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def claim(backend: SqliteBackend, fingerprint: str) -> None:
        try:
            barrier.wait(timeout=2)
            results.append(backend.claim_binding(OLD_HASH, fingerprint))
        except BaseException as exc:  # pragma: no cover - assertion reports the concrete error
            errors.append(exc)

    threads = [
        threading.Thread(target=claim, args=(left, "1" * 64)),
        threading.Thread(target=claim, args=(right, "2" * 64)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert sorted(results) == ["claimed", "conflict"]
    record = left.get_binding(OLD_HASH)
    assert record is not None and record[1] is True, "a conflicting reuse poisons the key"


def test_cascade_and_duplicate_store_are_linearizable() -> None:
    """A same-key graph rewrite cannot slip between cascade preflight and apply."""
    store = CompressionStore(enable_feedback=False)
    store.store("A", "A", explicit_hash=CHILD_HASH)
    store.store("B", "B", explicit_hash=SECOND_CHILD_HASH)
    store.store("P", f"P <<ccr:{CHILD_HASH}>>", explicit_hash=PARENT_HASH)

    preflight_done = threading.Event()
    allow_apply = threading.Event()
    original_preflight = store._preflight_cascade_graph

    def paused_preflight(*args: Any, **kwargs: Any) -> Any:
        graph = original_preflight(*args, **kwargs)
        preflight_done.set()
        allow_apply.wait(timeout=2)
        return graph

    store._preflight_cascade_graph = paused_preflight  # type: ignore[method-assign]
    purge_outcome: list[Any] = []

    def purge() -> None:
        purge_outcome.append(store.delete_cascade_detailed(PARENT_HASH))

    writer_finished = threading.Event()

    def rewrite() -> None:
        store.store(
            "P",
            f"P <<ccr:{CHILD_HASH}>> <<ccr:{SECOND_CHILD_HASH}>>",
            explicit_hash=PARENT_HASH,
        )
        writer_finished.set()

    purge_thread = threading.Thread(target=purge)
    purge_thread.start()
    assert preflight_done.wait(timeout=2)
    writer_thread = threading.Thread(target=rewrite)
    writer_thread.start()

    # Give the old implementation a deterministic window to perform the rewrite
    # while cascade is paused. A mutation guard makes the writer block instead.
    writer_finished.wait(timeout=0.25)
    allow_apply.set()
    purge_thread.join(timeout=3)
    writer_thread.join(timeout=3)

    assert purge_outcome
    parent_present = store.exists_any_tier(PARENT_HASH)
    second_child_present = store.exists_any_tier(SECOND_CHILD_HASH)
    assert parent_present or not second_child_present, (
        "non-linearizable outcome: rewritten parent vanished while its newly referenced child survived"
    )
