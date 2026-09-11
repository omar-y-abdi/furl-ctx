from __future__ import annotations

import hashlib
import sqlite3
import time
from typing import Any

from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import CompressionEntry, CompressionStore

HASH = "9" * 24


def _entry(original: str, *, hash_key: str = HASH) -> CompressionEntry:
    return CompressionEntry(
        hash=hash_key,
        original_content=original,
        compressed_content="view",
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


def _legacy_fingerprint(original: str) -> str:
    return hashlib.sha256(original.encode("utf-8", "surrogatepass")).hexdigest()


def _write_legacy_binding(db_path: Any, original: str | None) -> str:
    fingerprint = _legacy_fingerprint(original or "retired-original")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ccr_bindings "
            "(hash_key, content_fingerprint, conflicted) VALUES (?, ?, 0)",
            (HASH.encode("utf-8"), fingerprint),
        )
    return fingerprint


def test_new_binding_uses_opaque_provenance_id_not_content_digest(tmp_path: Any) -> None:
    db_path = tmp_path / "opaque-binding.sqlite3"
    backend = SqliteBackend(db_path=db_path, max_rows=100)
    store = CompressionStore(backend=backend, enable_feedback=False)
    original = "producer-A"
    try:
        store.store(original, "view", explicit_hash=HASH, require_durable=True)
        entry = backend.get(HASH)
        binding = backend.get_binding(HASH)

        assert entry is not None
        assert entry.binding_id is not None
        assert binding == (entry.binding_id, False)
        assert binding[0] != _legacy_fingerprint(original)
    finally:
        backend.close()


def test_live_legacy_binding_is_migrated_without_losing_retrievability(tmp_path: Any) -> None:
    db_path = tmp_path / "legacy-live.sqlite3"
    original = "legacy-live-original"
    backend = SqliteBackend(db_path=db_path, max_rows=100)
    backend.set(HASH, _entry(original))
    backend.close()
    legacy = _write_legacy_binding(db_path, original)

    reopened_backend = SqliteBackend(db_path=db_path, max_rows=100)
    store = CompressionStore(backend=reopened_backend, enable_feedback=False)
    try:
        migrated = reopened_backend.get(HASH)
        binding = reopened_backend.get_binding(HASH)
        recovered = store.retrieve(HASH)

        assert migrated is not None and migrated.binding_id is not None
        assert binding == (migrated.binding_id, False)
        assert binding[0] != legacy
        assert recovered is not None and recovered.original_content == original
    finally:
        reopened_backend.close()


def test_spill_only_legacy_binding_migrates_against_the_live_spill_row(tmp_path: Any) -> None:
    primary_path = tmp_path / "legacy-primary.sqlite3"
    spill_path = tmp_path / "legacy-spill.sqlite3"
    original = "legacy-spill-original"

    primary = SqliteBackend(db_path=primary_path, max_rows=100)
    primary.close()
    legacy = _write_legacy_binding(primary_path, original)

    spill = SqliteBackend(db_path=spill_path, max_rows=100)
    spill.set(HASH, _entry(original))
    spill.close()

    reopened_primary = SqliteBackend(db_path=primary_path, max_rows=100)
    reopened_spill = SqliteBackend(db_path=spill_path, max_rows=100)
    store = CompressionStore(
        backend=reopened_primary,
        spill=reopened_spill,
        enable_feedback=False,
    )
    try:
        migrated = reopened_spill.get(HASH)
        binding = reopened_primary.get_binding(HASH)
        recovered = store.retrieve(HASH)

        assert migrated is not None and migrated.binding_id is not None
        assert binding == (migrated.binding_id, False)
        assert binding[0] != legacy
        assert recovered is not None and recovered.original_content == original
    finally:
        reopened_primary.close()
        reopened_spill.close()


def test_orphaned_legacy_tombstone_is_retired_without_content_verifier(tmp_path: Any) -> None:
    db_path = tmp_path / "legacy-orphan.sqlite3"
    backend = SqliteBackend(db_path=db_path, max_rows=100)
    backend.close()
    legacy = _write_legacy_binding(db_path, None)

    reopened_backend = SqliteBackend(db_path=db_path, max_rows=100)
    CompressionStore(backend=reopened_backend, enable_feedback=False)
    try:
        binding = reopened_backend.get_binding(HASH)
        assert binding is not None
        assert binding[0] != legacy
        assert binding[1] is True
    finally:
        reopened_backend.close()
