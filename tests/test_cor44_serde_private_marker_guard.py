"""COR-44 regression: serde_json magic-key payloads must be declined.

Background
----------
With ``arbitrary_precision`` + ``raw_value`` features enabled in the workspace
``Cargo.toml``, serde_json treats ``{"$serde_json::private::Number":"123"}``
as the number literal ``123`` and ``{"$serde_json::private::RawValue":…}``
unwraps to its payload.  On the four production parse entry points this means
adversarial or tool-echoed content is silently mutated — ``was_modified=True``
on the live path, poisoned CCR recovery on the array path, cross-language shape
divergence visible to Python's ``json.loads``.

Fix
---
A ``has_serde_private_marker`` guard in ``furl_core::transforms::
smart_crusher::compaction::walker`` intercepts every path before ``from_str``
is called.  The two Rust entry points (``try_parse_json_container``,
``smart_crush_content_collecting``) are covered by inline Rust tests; this
file covers the two pyo3 FFI entry points exposed through
``SmartCrusher.crush_array_json`` and ``SmartCrusher.compact_document_json``.
"""

from __future__ import annotations

import json

import pytest

from furl_ctx.transforms.smart_crusher import SmartCrusher, SmartCrusherConfig

_MAGIC_NUMBER = '{"$serde_json::private::Number":"123"}'
_MAGIC_RAW = '{"$serde_json::private::RawValue":"true"}'


def _crusher() -> SmartCrusher:
    return SmartCrusher(config=SmartCrusherConfig(min_tokens_to_crush=1))


# ── crush_array_json (lib.rs:891) ──────────────────────────────────────────


def test_crush_array_json_declines_number_magic_key() -> None:
    """COR-44: crush_array_json must raise ValueError on the Number magic key.

    Pre-fix this would silently parse the payload as the integer 123 —
    a type mutation that breaks CCR recovery and shifts message hashes.
    """
    crusher = _crusher()
    with pytest.raises(ValueError, match="serde_json internal key"):
        crusher.crush_array_json(_MAGIC_NUMBER)


def test_crush_array_json_declines_raw_value_magic_key() -> None:
    """COR-44: crush_array_json must raise ValueError on the RawValue magic key."""
    crusher = _crusher()
    with pytest.raises(ValueError, match="serde_json internal key"):
        crusher.crush_array_json(_MAGIC_RAW)


# ── compact_document_json (lib.rs:953) ─────────────────────────────────────


def test_compact_document_json_declines_number_magic_key() -> None:
    """COR-44: compact_document_json must raise ValueError on the Number magic key.

    Pre-fix the walker would parse the magic payload and re-serialize —
    mutating data at zero savings, contradicting the module's own contract.
    """
    crusher = _crusher()
    with pytest.raises(ValueError, match="serde_json internal key"):
        crusher.compact_document_json(_MAGIC_NUMBER)


def test_compact_document_json_declines_raw_value_magic_key() -> None:
    """COR-44: compact_document_json must raise ValueError on the RawValue magic key."""
    crusher = _crusher()
    with pytest.raises(ValueError, match="serde_json internal key"):
        crusher.compact_document_json(_MAGIC_RAW)


# ── Passthrough: normal JSON must still work ────────────────────────────────


def test_crush_array_json_still_accepts_normal_json() -> None:
    """Guard must not affect non-magic input."""
    crusher = _crusher()
    items = json.dumps([{"id": i, "val": f"v{i}"} for i in range(5)])
    result = crusher.crush_array_json(items)
    assert "items" in result, f"expected items key in result: {result}"


def test_compact_document_json_still_accepts_normal_json() -> None:
    """Guard must not affect non-magic input."""
    crusher = _crusher()
    doc = json.dumps({"key": "value", "num": 42})
    result = crusher.compact_document_json(doc)
    assert isinstance(result, str), f"expected str result: {result!r}"
