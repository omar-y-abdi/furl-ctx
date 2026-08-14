"""Regression pins for PR review findings in the CCR spill/cascade paths."""

from __future__ import annotations

import pytest

from furl_ctx.cache.backends.memory import InMemoryBackend
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
