"""Mutation-resistance hardening for compression_store.

Targets gaps NOT covered by test_compression_store_guards.py or
test_compression_store_redaction.py:
  - Recovery byte-exact literal pin (store → retrieve → original_content exact match).
  - Explicit hash boundary: 5 chars rejected, 6 accepted (the MIN floor boundary).
  - Non-hex explicit_hash rejected.
  - ttl=1 boundary (just above the forbidden 0).
  - exists() reports False for unknown key, True for live key.
  - Retrieved entry attribute pins (hash, original_content, compressed_content).
"""

from __future__ import annotations

import pytest

from furl_ctx.cache.compression_store import CompressionStore

# Fixed-width hash literals pinned to the contract (#21: 6 accepted, 5 rejected).
# Hard-coded (NOT derived from _MIN_EXPLICIT_HASH_LEN) so the test fails loudly if
# the production floor moves, rather than silently auto-retargeting to the new value.
_HASH_AT_FLOOR = "aaaaaa"  # 6 chars — the floor; MUST be accepted
_HASH_ONE_BELOW = "aaaaa"  # 5 chars — one below the floor; MUST be rejected

# Pinned original content literal — any mutation of the store/retrieve round-trip
# changes this exact string and fails the test.
_ORIGINAL = '[{"id": 0, "name": "alice", "score": 42}]'
_COMPRESSED = f"<<ccr:{_HASH_AT_FLOOR}>>"


# ---------------------------------------------------------------------------
# B1: byte-exact recovery round-trip with pinned literal
# ---------------------------------------------------------------------------


def test_b1_store_retrieve_byte_exact_literal() -> None:
    """store → retrieve.original_content == exact pinned literal.

    Mutation-sensitive: any change to the store path that corrupts, encodes,
    or truncates the stored content makes this test fail.
    """
    store = CompressionStore(max_entries=10)
    key = store.store(_ORIGINAL, _COMPRESSED, explicit_hash=_HASH_AT_FLOOR)
    entry = store.retrieve(key)
    assert entry is not None
    assert entry.original_content == _ORIGINAL
    # Also pin the hash stored — ensures explicit_hash is used verbatim
    assert entry.hash == _HASH_AT_FLOOR


def test_b1_store_retrieve_compressed_content_pinned() -> None:
    """Compressed content is stored byte-for-byte and returned unchanged."""
    store = CompressionStore(max_entries=10)
    key = store.store(_ORIGINAL, _COMPRESSED, explicit_hash=_HASH_AT_FLOOR)
    entry = store.retrieve(key)
    assert entry is not None
    assert entry.compressed_content == _COMPRESSED


# ---------------------------------------------------------------------------
# Hash boundary: min-length floor (5 rejected, 6 accepted)
# ---------------------------------------------------------------------------


def test_hash_one_below_floor_rejected_boundary() -> None:
    """_MIN_EXPLICIT_HASH_LEN - 1 = 5 chars: rejected.  Boundary at 5 vs 6."""
    store = CompressionStore(max_entries=10)
    with pytest.raises(ValueError):
        store.store(_ORIGINAL, _COMPRESSED, explicit_hash=_HASH_ONE_BELOW)


def test_hash_at_floor_accepted_boundary() -> None:
    """_MIN_EXPLICIT_HASH_LEN = 6 chars: accepted.  The exact boundary."""
    store = CompressionStore(max_entries=10)
    key = store.store(_ORIGINAL, _COMPRESSED, explicit_hash=_HASH_AT_FLOOR)
    assert key == _HASH_AT_FLOOR
    assert store.retrieve(key) is not None


def test_non_hex_explicit_hash_rejected() -> None:
    """Non-hex characters in explicit_hash must raise."""
    store = CompressionStore(max_entries=10)
    with pytest.raises(ValueError):
        store.store(_ORIGINAL, _COMPRESSED, explicit_hash="zzzzzz")


# ---------------------------------------------------------------------------
# TTL boundary: 0 forbidden, 1 accepted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_ttl", [0, -1])
def test_ttl_at_or_below_zero_rejected(bad_ttl: int) -> None:
    """ttl=0 and ttl<0: boundary — 0 forbidden, 1 accepted."""
    store = CompressionStore(max_entries=10)
    with pytest.raises(ValueError):
        store.store(_ORIGINAL, _COMPRESSED, explicit_hash=_HASH_AT_FLOOR, ttl=bad_ttl)


def test_ttl_one_accepted_boundary() -> None:
    """ttl=1: just above the forbidden 0 — must be accepted and retrievable."""
    store = CompressionStore(max_entries=10)
    key = store.store(_ORIGINAL, _COMPRESSED, explicit_hash=_HASH_AT_FLOOR, ttl=1)
    # Immediately retrievable (1-second TTL, retrieved within the same test)
    assert store.retrieve(key) is not None


# ---------------------------------------------------------------------------
# exists() boundary: unknown → False, live → True
# ---------------------------------------------------------------------------


def test_exists_unknown_key_returns_false() -> None:
    """exists() for a key never stored returns False."""
    store = CompressionStore(max_entries=10)
    assert store.exists("deadbeef") is False


def test_exists_live_key_returns_true() -> None:
    """exists() for a live key returns True."""
    store = CompressionStore(max_entries=10)
    key = store.store(_ORIGINAL, _COMPRESSED, explicit_hash=_HASH_AT_FLOOR)
    assert store.exists(key) is True


def test_retrieve_unknown_key_returns_none() -> None:
    """retrieve() for a key never stored returns None."""
    store = CompressionStore(max_entries=10)
    assert store.retrieve("deadbeef") is None


# ---------------------------------------------------------------------------
# Parametrized real-producer hash widths (12 and 24)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "h",
    [
        "abcdef123456",  # 12-char (real engine default)
        "abcdef1234567890abcd",  # 20-char
        "a" * 24,  # 24-char (tool_injection.py anti-spoof width)
    ],
)
def test_wider_explicit_hash_accepted(h: str) -> None:
    """All real-producer hash widths (≥6) are accepted."""
    store = CompressionStore(max_entries=10)
    compressed = f"<<ccr:{h}>>"
    key = store.store(_ORIGINAL, compressed, explicit_hash=h)
    assert key == h.lower()
    assert store.retrieve(key) is not None
