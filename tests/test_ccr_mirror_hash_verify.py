"""T3 (part 3, Python half): the Rust->Python CCR mirror must hash-verify the
bytes it fetched via ``ccr_get`` against the key it is about to store them
under — and refuse a mismatch — so a store collision that slipped the producer
can never persist FOREIGN bytes under a key.

Every content-addressed CCR key the mirror handles is ``SHA-256(payload)``
truncated to the key width (12 hex legacy, 24 hex current), so the check is
``sha256(bytes)[:len(key)] == key``. A mismatch means ``ccr_get`` returned
someone else's bytes; the mirror raises ``CcrMirrorError`` (which
``compress()``'s fail-open boundary turns into a revert-to-original) rather than
writing wrong bytes the retrieval path would later serve as truth.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from furl_ctx.cache.compression_store import get_compression_store
from furl_ctx.transforms.smart_crusher import (
    CcrMirrorError,
    SmartCrusher,
    SmartCrusherConfig,
)


class _LyingRust:
    """Delegating proxy over the pyo3 crusher whose ``ccr_get`` returns
    caller-chosen bytes for specific keys (simulating a store that resolved a
    key to foreign content) while every other attribute passes through."""

    def __init__(self, inner: Any, lies: dict[str, str]) -> None:
        self._inner = inner
        self._lies = lies

    def ccr_get(self, key: str) -> str | None:
        if key in self._lies:
            return self._lies[key]
        return self._inner.ccr_get(key)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_mirror_refuses_bytes_that_do_not_hash_to_key() -> None:
    """A mismatch between the fetched bytes and the key raises and stores
    nothing — never persist foreign content under a key.

    RED before the fix (the mirror stored whatever ``ccr_get`` returned);
    GREEN after (the hash-verify gate raises ``CcrMirrorError``)."""
    crusher = SmartCrusher(config=SmartCrusherConfig())
    foreign = '["not the original content for this key"]'
    key = "abcdef0123456789abcdef01"  # valid 24-hex shape, NOT sha256(foreign)[:24]
    assert hashlib.sha256(foreign.encode("utf-8")).hexdigest()[:24] != key, "precondition"

    crusher._rust = _LyingRust(crusher._rust, {key: foreign})
    with pytest.raises(CcrMirrorError):
        crusher._mirror_single_hash_to_python_store(
            key, strategy="smart_crusher_row_drop", query_context="q", tool_name=None
        )

    entry = get_compression_store().retrieve(key)
    assert entry is None or entry.original_content != foreign, (
        "foreign bytes were persisted under a mismatched key"
    )


def test_mirror_accepts_bytes_that_hash_to_key() -> None:
    """Control: bytes that DO hash to the key mirror cleanly (the verify gate
    must not over-reach and reject legitimate content)."""
    crusher = SmartCrusher(config=SmartCrusherConfig())
    content = '["genuine-row-content"]'
    key = hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]

    crusher._rust = _LyingRust(crusher._rust, {key: content})
    crusher._mirror_single_hash_to_python_store(
        key, strategy="smart_crusher_row_drop", query_context="q", tool_name=None
    )  # must NOT raise

    entry = get_compression_store().retrieve(key)
    assert entry is not None and entry.original_content == content


def test_mirror_accepts_legacy_12hex_key_that_still_hashes() -> None:
    """Backward compatibility: a legacy 12-hex recovery key whose bytes hash to
    it (``sha256(bytes)[:12]``) is still accepted and retrievable after the
    key width was widened — the verify gate truncates to the key's own width."""
    crusher = SmartCrusher(config=SmartCrusherConfig())
    content = '["legacy-twelve-hex-row"]'
    legacy_key = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    assert len(legacy_key) == 12

    crusher._rust = _LyingRust(crusher._rust, {legacy_key: content})
    crusher._mirror_single_hash_to_python_store(
        legacy_key, strategy="smart_crusher_row_drop", query_context="q", tool_name=None
    )  # must NOT raise — 12-hex legacy keys remain valid

    entry = get_compression_store().retrieve(legacy_key)
    assert entry is not None and entry.original_content == content
