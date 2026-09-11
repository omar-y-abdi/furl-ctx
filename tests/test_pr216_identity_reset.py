"""Namespace-reset semantics for PR #216 explicit-hash identity safety."""

from __future__ import annotations

from typing import Any

from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import CompressionStore

HASH_KEY = "9" * 24


def test_full_clear_resets_retired_explicit_identity(tmp_path: Any) -> None:
    """Per-key deletion preserves identity; an intentional full reset may reuse it."""
    backend = SqliteBackend(db_path=tmp_path / "identity-reset.sqlite3", max_rows=100)
    store = CompressionStore(backend=backend, enable_feedback=False)
    try:
        store.store("producer-A", "view-A", explicit_hash=HASH_KEY)
        assert store.delete(HASH_KEY) is True

        # A stale marker for A can still exist outside the store, so deleting
        # only A's bytes must not make HASH_KEY available for foreign content.
        store.store("producer-B", "view-B", explicit_hash=HASH_KEY)
        assert store.retrieve(HASH_KEY) is None

        # A whole-store clear is the explicit namespace reset boundary: it
        # removes payloads and identity claims together, so a fresh session can
        # intentionally reuse the key without inheriting old collision history.
        assert store.clear() == 0
        store.store("producer-B", "view-B", explicit_hash=HASH_KEY)
        entry = store.retrieve(HASH_KEY)
        assert entry is not None
        assert entry.original_content == "producer-B"
    finally:
        backend.close()
