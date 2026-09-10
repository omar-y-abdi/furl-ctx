"""Surrogate-hash totality for CompressionStore.store().

The default-hash path hashed ``original.encode()`` (strict UTF-8), so a lone
surrogate anywhere in the original — deliverable through the MCP
``furl_compress`` tool, since JSON strings carry them via ``\\uD800`` escapes
— raised UnicodeEncodeError before the entry was stored, whenever no
``explicit_hash`` was supplied.

Fix: hash ``original.encode("utf-8", "surrogatepass")``. For every valid-UTF8
original the emitted bytes (and therefore the key) are identical to the
strict encode, so existing keys are unchanged; lone-surrogate originals now
hash instead of raising. ``InMemoryBackend.get_stats()`` had the same
strict-encode hole on stored content (reachable via ``store.get_stats()`` —
the MCP ``furl_stats`` path) and gets the same treatment.
"""

from __future__ import annotations

import hashlib

import pytest

from furl_ctx.cache.compression_store import CompressionStore

# Lone high + lone low surrogate, unpaired: valid in a Python str (and
# deliverable via JSON \uD800 escapes), NOT encodable by strict UTF-8.
SURROGATE_ORIGINAL = "prefix \ud800 middle \udfff suffix"


def test_surrogate_original_stores_without_explicit_hash() -> None:
    store = CompressionStore(max_entries=10)

    hash_key = store.store(original=SURROGATE_ORIGINAL, compressed="marker")

    assert hash_key
    assert len(hash_key) == 24


def test_surrogate_original_retrieves_byte_exact() -> None:
    store = CompressionStore(max_entries=10)

    hash_key = store.store(original=SURROGATE_ORIGINAL, compressed="marker")
    entry = store.retrieve(hash_key)

    assert entry is not None
    assert entry.original_content == SURROGATE_ORIGINAL


def test_surrogate_store_is_deterministic() -> None:
    # Same content twice → same key (the duplicate-store branch, not a
    # collision refusal).
    store = CompressionStore(max_entries=10)

    first = store.store(original=SURROGATE_ORIGINAL, compressed="marker")
    second = store.store(original=SURROGATE_ORIGINAL, compressed="marker")

    assert first == second


@pytest.mark.parametrize(
    "original",
    [
        "plain ascii",
        "ümlauts — and em-dashes",
        "日本語のテキスト",
        "emoji 🎉 astral \U0001f680",
        '{"json": "payload", "n": 42}',
    ],
)
def test_valid_utf8_hash_identical_to_strict_encode(original: str) -> None:
    # The key for every valid-UTF8 original must equal the OLD strict-encode digest — surrogatepass
    # emits identical bytes for all valid input, so every existing marker keeps resolving.
    store = CompressionStore(max_entries=10)

    expected = hashlib.sha256(original.encode()).hexdigest()[:24]

    assert store.store(original=original, compressed="c") == expected


def test_get_stats_survives_surrogate_original() -> None:
    # InMemoryBackend.get_stats() strict-encoded stored content for its bytes
    # estimate — the same hole, one call away via the MCP furl_stats path.
    store = CompressionStore(max_entries=10)
    store.store(original=SURROGATE_ORIGINAL, compressed="marker")

    stats = store.get_stats()

    assert stats["entry_count"] == 1
    assert stats["backend"]["bytes_used"] > 0
