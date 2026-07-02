"""COR-56: a true hash collision must keep the FIRST binding.

The store detected a same-key/different-content collision and warned
("Hash collision detected") — then proceeded with the destructive overwrite
anyway, silently rebinding the earlier, already-emitted marker to the newer
content. Keep-first: the overwrite is refused and logged at ERROR, so the
NEW caller's marker dangles loudly instead of corrupting the old binding.
Expired same-key entries are reaped before the collision check, so a dead
binding can never wedge its key.
"""

from __future__ import annotations

import logging

from furl_ctx.cache.compression_store import CompressionStore

H = "abcdef123456"
STORE_LOGGER = "furl_ctx.cache.compression_store"


def _collide(store: CompressionStore) -> str:
    store.store(original="first content", compressed=f"<<ccr:{H}>>", explicit_hash=H)
    return store.store(original="SECOND content", compressed=f"<<ccr:{H}>>", explicit_hash=H)


def test_collision_keeps_first_binding() -> None:
    store = CompressionStore(max_entries=10)
    returned = _collide(store)
    assert returned == H  # signature unchanged: the key is still returned
    entry = store.retrieve(H)
    assert entry is not None
    assert entry.original_content == "first content"


def test_collision_logged_at_error(caplog) -> None:
    store = CompressionStore(max_entries=10)
    with caplog.at_level(logging.ERROR, logger=STORE_LOGGER):
        _collide(store)
    records = [r for r in caplog.records if "Hash collision detected" in r.getMessage()]
    assert records, "collision must be logged loudly"
    assert all(r.levelno == logging.ERROR for r in records)


def test_duplicate_same_content_still_updates(caplog) -> None:
    # Same key + SAME content is the normal duplicate-store path: no error,
    # entry refreshed (created_at bumps, restarting the TTL window).
    store = CompressionStore(max_entries=10)
    store.store(original="same content", compressed=f"<<ccr:{H}>>", explicit_hash=H)
    first_created = store._backend.get(H).created_at
    with caplog.at_level(logging.DEBUG, logger=STORE_LOGGER):
        store.store(original="same content", compressed=f"<<ccr:{H}>>", explicit_hash=H)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("Duplicate store" in r.getMessage() for r in caplog.records)
    entry = store.retrieve(H)
    assert entry is not None
    assert entry.original_content == "same content"
    assert entry.created_at >= first_created


def test_expired_first_binding_does_not_wedge_the_key() -> None:
    # Keep-first must not let a DEAD binding block its key: an expired
    # same-key entry is reaped by _evict_if_needed() before the collision
    # check runs, so different content can bind after expiry.
    store = CompressionStore(max_entries=10)
    store.store(original="first content", compressed=f"<<ccr:{H}>>", explicit_hash=H, ttl=60)
    store._backend.get(H).created_at -= 120.0  # age the entry past its ttl
    store.store(original="SECOND content", compressed=f"<<ccr:{H}>>", explicit_hash=H)
    entry = store.retrieve(H)
    assert entry is not None
    assert entry.original_content == "SECOND content"
