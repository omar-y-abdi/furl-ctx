"""Py↔Rust parity lock for the CCR row-drop recovery key (``hash_canonical``).

The recovery key that crosses the FFI is ``SHA-256(canonical)[:12]`` over
``canonical_array_json(items)`` (``serde_json::to_string`` of the original
rows). Both the Rust producer and this Python reference must agree on it byte
for byte, or a dropped row is stored under one key and retrieved under another
— silent, unrecoverable loss.

This is the PYTHON half of the lock. The RUST half pins the same four literals
in ``crusher.rs::tests::hash_canonical_pinned_vectors``. The two halves use
DIFFERENT hash implementations (Rust ``sha2`` crate vs Python ``hashlib``), so
a truncation / byte-order / hex-format drift on either side flips one of the
pins — this is a genuine cross-implementation check, NOT the parallel-mutation
blindness rule 2 warns about.
"""

from __future__ import annotations

import hashlib
import json

from furl_ctx.transforms.smart_crusher import SmartCrusher

# Canonical form = serde_json::to_string(items): compact (no spaces), keys as
# emitted. For these vectors (empty / scalars / single-key dicts) Python's
# compact dump is byte-identical to serde_json's.
_VECTORS: list[tuple[list[object], str]] = [
    ([], "4f53cda18c2b"),
    (["alpha", "beta", "gamma"], "a3e185260009"),
    ([1, 2, 3, 4, 5], "f5baf0e4336f"),
    ([{"id": 1}, {"id": 2}, {"id": 3}], "d99179347cb1"),
]


def _canonical(items: list[object]) -> str:
    return json.dumps(items, separators=(",", ":"), ensure_ascii=False)


def _hash_canonical(items: list[object]) -> str:
    return hashlib.sha256(_canonical(items).encode("utf-8")).hexdigest()[:12]


def test_pinned_vectors_match_the_rust_literals() -> None:
    """Each literal is the SHA-256[:12] of the exact canonical bytes — pinned
    identically in ``crusher.rs::tests::hash_canonical_pinned_vectors``. A typo
    in either side's literal, or a hashing/truncation change, fails here."""
    for items, expected in _VECTORS:
        assert _hash_canonical(items) == expected, (
            f"canonical {_canonical(items)!r} → {_hash_canonical(items)} ≠ pinned {expected}"
        )


def test_canonical_form_is_compact_no_spaces() -> None:
    """The hash is over the COMPACT serde_json form. If this drifts to a
    spaced/pretty form, every parity literal above is wrong — pin it."""
    assert _canonical([{"id": 1}, {"id": 2}]) == '[{"id":1},{"id":2}]'
    assert _canonical([]) == "[]"


def test_rust_crush_emits_the_python_reference_hash() -> None:
    """Cross-language lock: drive a fixed dropping array through the production
    Rust ``crush()`` and assert the typed ``ccr_hashes`` it emits CONTAINS the
    key this Python reference computes over the same canonical input. Rust's
    ``sha2`` and Python's ``hashlib`` are independent implementations, so this
    catches a Rust-side hasher drift the Rust-only pin cannot (and vice versa).
    """
    # 600 distinct lines → a guaranteed row-drop on the live crush() path
    # (the 1a parity suite uses the same shape). Fixed seed → deterministic
    # canonical → a stable expected hash.
    items: list[object] = [f"ccr-parity-line-{i}" for i in range(600)]
    expected = _hash_canonical(items)  # SHA-256[:12] over the full-array canonical

    crusher = SmartCrusher()
    r = crusher._rust.crush(json.dumps(items, ensure_ascii=False), "", 1.0)

    assert r.ccr_hashes, "fixed 600-line array must drop and surface a typed hash"
    assert expected in set(r.ccr_hashes), (
        f"Rust crush() emitted {sorted(set(r.ccr_hashes))}, which does NOT "
        f"contain the Python-reference key {expected} over the same canonical "
        f"— Py↔Rust hash_canonical parity is broken (silent recovery loss)."
    )
