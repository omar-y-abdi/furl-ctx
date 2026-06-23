"""Regression tests for the store-guard bugs #21, #22, #23.

#21 explicit_hash min-length: a 1-char hex key was accepted (collidable).
#22 ttl<=0: an immediately-expired entry was stored and leaked.
#23 _evict_if_needed stale-heap path: the store could exceed max_entries.

All three are validation/guard fixes at the store boundary or in eviction; they
must NOT weaken the byte-exact recovery invariant (the live entries that survive
eviction stay retrievable; evicted ones report the loud miss elsewhere).
"""
from __future__ import annotations

import heapq
import json

import pytest

from headroom.cache.compression_store import (
    CompressionStore,
    _MIN_EXPLICIT_HASH_LEN,
)


def _store(items, h):
    return json.dumps(items), f"<<ccr:{h}>>"


# --------------------------------------------------------------------------- #
# #21 — explicit_hash minimum length
# --------------------------------------------------------------------------- #


def test_explicit_hash_one_char_rejected() -> None:
    store = CompressionStore(max_entries=10)
    orig, comp = _store([{"id": 0}], "a")
    with pytest.raises(ValueError, match="at least"):
        store.store(orig, comp, explicit_hash="a")


@pytest.mark.parametrize("h", ["a", "ab", "abc", "abcd", "abcde"])
def test_explicit_hash_below_floor_rejected(h: str) -> None:
    store = CompressionStore(max_entries=10)
    with pytest.raises(ValueError):
        store.store(json.dumps([{"id": 0}]), "<<ccr:x>>", explicit_hash=h)


def test_explicit_hash_at_floor_accepted() -> None:
    # The floor matches the recovery regex {6,}: a 6-char hash MUST be accepted
    # so the store accepts every hash retrieval can recognize.
    store = CompressionStore(max_entries=10)
    h = "a" * _MIN_EXPLICIT_HASH_LEN
    key = store.store(json.dumps([{"id": 0}]), f"<<ccr:{h}>>", explicit_hash=h)
    assert key == h
    assert store.retrieve(h) is not None


def test_explicit_hash_real_producer_width_accepted() -> None:
    # Real producers emit 12-char hashes — must always be accepted (no regression).
    store = CompressionStore(max_entries=10)
    h = "abcdef123456"
    key = store.store(json.dumps([{"id": 0}]), f"<<ccr:{h}>>", explicit_hash=h)
    assert key == h
    assert store.retrieve(h) is not None


# --------------------------------------------------------------------------- #
# #22 — non-positive ttl rejected (no immediately-expired leak)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad_ttl", [0, -1, -300])
def test_nonpositive_ttl_rejected(bad_ttl: int) -> None:
    store = CompressionStore(max_entries=10)
    with pytest.raises(ValueError, match="positive"):
        store.store(json.dumps([{"id": 0}]), "<<ccr:aaaaaa>>", explicit_hash="aaaaaa", ttl=bad_ttl)


def test_ttl_zero_does_not_leak_into_backend() -> None:
    # The leak symptom: a ttl=0 entry residing in the backend. With the guard it
    # never gets stored at all (rejected), so the backend stays empty.
    store = CompressionStore(max_entries=10)
    with pytest.raises(ValueError):
        store.store(json.dumps([{"id": 0}]), "<<ccr:aaaaaa>>", explicit_hash="aaaaaa", ttl=0)
    assert store._backend.count() == 0, "rejected ttl=0 entry must not reside in the backend"


def test_positive_ttl_and_none_unaffected() -> None:
    store = CompressionStore(max_entries=10)
    k1 = store.store(json.dumps([{"id": 0}]), "<<ccr:aaaaaa>>", explicit_hash="aaaaaa", ttl=300)
    k2 = store.store(json.dumps([{"id": 1}]), "<<ccr:bbbbbb>>", explicit_hash="bbbbbb")  # ttl=None
    assert store.retrieve(k1) is not None
    assert store.retrieve(k2) is not None


# --------------------------------------------------------------------------- #
# #23 — eviction must never leave the store over capacity (stale-heap path)
# --------------------------------------------------------------------------- #


def test_stale_heap_eviction_respects_max_entries() -> None:
    store = CompressionStore(max_entries=2)
    a = store.store(json.dumps([{"id": 0}]), "<<ccr:aaaaaa>>", explicit_hash="aaaaaa")
    b = store.store(json.dumps([{"id": 1}]), "<<ccr:bbbbbb>>", explicit_hash="bbbbbb")

    # Force the stale-heap state: heap entries with WRONG timestamps (so they
    # are popped as stale, evicting nothing real) and stale_ratio < 0.5 so the
    # rebuild guard does not pre-emptively fire.
    store._eviction_heap = [(0.0, a), (0.0, b)]
    heapq.heapify(store._eviction_heap)
    store._stale_heap_entries = 0

    c = store.store(json.dumps([{"id": 2}]), "<<ccr:cccccc>>", explicit_hash="cccccc")

    # #23: the store must NOT exceed max_entries.
    assert store._backend.count() <= store._max_entries, (
        f"store over capacity: {store._backend.count()} > {store._max_entries}"
    )
    # Recovery-safe: the newest entry survives and is retrievable; eviction was
    # oldest-first, not a side-door that drops live-referenced data silently.
    assert store.retrieve(c) is not None, "newest entry must survive eviction"
