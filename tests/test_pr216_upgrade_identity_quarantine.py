"""Upgrade/retention identity regressions for PR #216 explicit hashes."""

from __future__ import annotations

from typing import Any

from furl_ctx.cache.backends.memory import InMemoryBackend
from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import CompressionEntry, CompressionStore

HASH_KEY = "a" * 24


def _entry(original: str) -> CompressionEntry:
    return CompressionEntry(
        hash=HASH_KEY,
        original_content=original,
        compressed_content="view",
        original_tokens=1,
        compressed_tokens=1,
        original_item_count=1,
        compressed_item_count=1,
        tool_name=None,
        tool_call_id=None,
        query_context=None,
        created_at=100.0,
        ttl=3600,
    )


def test_legacy_spill_only_unbound_explicit_hash_is_quarantined(tmp_path: Any) -> None:
    """A surviving pre-upgrade spill row cannot be canonized after its primary disappears."""
    spill = SqliteBackend(db_path=tmp_path / "legacy-spill.sqlite3", max_rows=100)
    spill.set(HASH_KEY, _entry("legacy-A"))
    store = CompressionStore(
        backend=InMemoryBackend(),
        spill=spill,
        now_fn=lambda: 101.0,
        enable_feedback=False,
    )
    try:
        assert spill.get_binding(HASH_KEY) is None, "precondition: this is a legacy unbound row"
        assert store.retrieve(HASH_KEY) is None
        assert store.get_entry_status(HASH_KEY)["status"] == "unsafe"
    finally:
        spill.close()


def test_expiry_does_not_release_non_content_addressed_explicit_identity() -> None:
    """An expired marker identity must loud-miss forever instead of resolving to new bytes."""
    now = [100.0]
    store = CompressionStore(
        backend=InMemoryBackend(),
        now_fn=lambda: now[0],
        enable_feedback=False,
    )
    store.store("producer-A", "view-A", explicit_hash=HASH_KEY, ttl=1)

    now[0] = 102.0
    # The next write reaps expired payloads on the hot path. That must not erase
    # the published identity: a stale marker for A can still exist outside the store.
    store.store("producer-B", "view-B", explicit_hash=HASH_KEY)

    assert store.retrieve(HASH_KEY) is None
    binding = store._backend.get_binding(HASH_KEY)  # type: ignore[attr-defined]
    assert binding is not None and binding[1] is True


def test_sqlite_expiry_gc_retains_non_content_addressed_identity(tmp_path: Any) -> None:
    """The durable expiry GC must retire payload bytes without freeing their marker key."""
    now = [100.0]
    backend = SqliteBackend(db_path=tmp_path / "expiry-identity.sqlite3", max_rows=100)
    store = CompressionStore(
        backend=backend,
        now_fn=lambda: now[0],
        enable_feedback=False,
    )
    try:
        store.store("producer-A", "view-A", explicit_hash=HASH_KEY, ttl=1)
        now[0] = 102.0

        # Any write runs the backend's indexed expiry GC before collision checks.
        store.store("unrelated", "view", explicit_hash="b" * 24)
        assert backend.get(HASH_KEY) is None, "precondition: the expired payload was reaped"
        binding = backend.get_binding(HASH_KEY)
        assert binding is not None and binding[1] is False

        store.store("producer-B", "view-B", explicit_hash=HASH_KEY)
        assert store.retrieve(HASH_KEY) is None
        binding = backend.get_binding(HASH_KEY)
        assert binding is not None and binding[1] is True
    finally:
        backend.close()
