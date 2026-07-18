"""Audit #9 (supersedes COR-56): a true hash collision must never serve FOREIGN
content.

A same-key / different-content collision is unresolvable: both producers'
``<<ccr:HASH>>`` markers point at the SAME key and ``retrieve()`` — a bare
``backend.get(hash_key)`` with no per-marker content identity — cannot tell them
apart. The earlier keep-first design's comment promised the new marker would
"dangle LOUDLY", but the bare get actually served the FIRST producer's content
to the SECOND producer's marker (foreign content, silent corruption — audit #9).

Fix: on collision the store DROPS the ambiguous binding — it deletes the stored
entry and refuses the new one — so every marker on the key resolves to a LOUD,
cause-honest miss (recompute) rather than foreign content. A true collision is
astronomically rare (48-bit for the explicit 12-hex path, 96-bit for the
computed default) and a loud miss is recoverable; foreign bytes are not.
"""

from __future__ import annotations

import logging

import pytest

from furl_ctx.cache.compression_store import CompressionStore, DurableWriteError

H = "abcdef123456"
STORE_LOGGER = "furl_ctx.cache.compression_store"


def _collide(store: CompressionStore) -> str:
    store.store(original="first content", compressed=f"<<ccr:{H}>>", explicit_hash=H)
    return store.store(original="SECOND content", compressed=f"<<ccr:{H}>>", explicit_hash=H)


def test_collision_drops_binding_retrieve_is_loud_miss() -> None:
    store = CompressionStore(max_entries=10)
    returned = _collide(store)
    assert returned == H  # signature unchanged: the key is still returned
    # The ambiguous binding is dropped → retrieve is a loud miss (None), which
    # the retrieval callers surface as an explicit error — NOT foreign content.
    assert store.retrieve(H) is None


def test_collision_never_serves_either_producers_content() -> None:
    # The core anti-corruption invariant: after the collision the key must not
    # resolve to the OTHER (foreign) producer's bytes — nor to anyone's.
    store = CompressionStore(max_entries=10)
    _collide(store)
    entry = store.retrieve(H)
    assert entry is None, "a collided key must not resolve to any stored content"
    # get_entry_status reports the miss the caller turns into a loud error.
    assert store.get_entry_status(H)["status"] == "missing"


def test_collision_logged_at_error(caplog) -> None:
    store = CompressionStore(max_entries=10)
    with caplog.at_level(logging.ERROR, logger=STORE_LOGGER):
        _collide(store)
    records = [r for r in caplog.records if "Hash collision detected" in r.getMessage()]
    assert records, "collision must be logged loudly"
    assert all(r.levelno == logging.ERROR for r in records)


def test_duplicate_same_content_still_updates(caplog) -> None:
    # Same key + SAME content is the normal duplicate-store path, NOT a
    # collision: no error, entry refreshed (created_at bumps, TTL restarts),
    # and it still resolves.
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


def test_collision_with_require_durable_vetoes_bug6() -> None:
    # Bug-6: the collision-drop path used to `return hash_key` BEFORE the
    # require_durable check, so a durable caller was handed a hash for content
    # that was actually dropped (a marker that loud-misses, contract broken).
    # Now the drop honors the veto: require_durable raises DurableWriteError so
    # the caller reverts to the original uncompressed content.
    store = CompressionStore(max_entries=10)
    store.store(original="first content", compressed=f"<<ccr:{H}>>", explicit_hash=H)
    with pytest.raises(DurableWriteError, match="collision"):
        store.store(
            original="SECOND content",
            compressed=f"<<ccr:{H}>>",
            explicit_hash=H,
            require_durable=True,
        )
    # Still dropped (no foreign content served) — a loud miss, as before.
    assert store.retrieve(H) is None


def test_non_durable_collision_keeps_returning_hash_bug6() -> None:
    # The non-durable path is unchanged: it returns the key (which now loud-misses)
    # rather than raising — only require_durable callers get the veto.
    store = CompressionStore(max_entries=10)
    assert _collide(store) == H
    assert store.retrieve(H) is None


def test_expired_first_binding_does_not_wedge_the_key() -> None:
    # An expired same-key entry is reaped by _evict_if_needed() BEFORE the
    # collision check, so it is not a collision at all: different content binds
    # cleanly after expiry and resolves normally (the key is not wedged).
    store = CompressionStore(max_entries=10)
    store.store(original="first content", compressed=f"<<ccr:{H}>>", explicit_hash=H, ttl=60)
    store._backend.get(H).created_at -= 120.0  # age the entry past its ttl
    store.store(original="SECOND content", compressed=f"<<ccr:{H}>>", explicit_hash=H)
    entry = store.retrieve(H)
    assert entry is not None
    assert entry.original_content == "SECOND content"
