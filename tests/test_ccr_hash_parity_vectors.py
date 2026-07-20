"""Py↔Rust parity lock for the CCR row-drop recovery key (``hash_canonical``).

The recovery key that crosses the FFI is ``SHA-256(canonical)[:24]`` over
``canonical_array_json(items)`` (``serde_json::to_string`` of the original
rows). Both the Rust producer and this Python reference must agree on it byte
for byte, or a dropped row is stored under one key and retrieved under another
— silent, unrecoverable loss.

This is the PYTHON half of the lock. The RUST half pins the same literals in
``crusher.rs::tests::hash_canonical_pinned_vectors`` and
``crusher.rs::tests::hash_canonical_wire_form_pinned_vectors``. The two halves
use DIFFERENT hash implementations (Rust ``sha2`` crate vs Python
``hashlib``), so a truncation / byte-order / hex-format drift on either side
flips one of the pins — this is a genuine cross-implementation check, NOT the
parallel-mutation blindness rule 2 warns about.

# Scope of the Python reference (TEST-33)

The AUTHORITATIVE canonical is Rust's: serde_json with ``preserve_order`` +
``arbitrary_precision``. It preserves DECIMAL literals verbatim (``1.50``
stays ``1.50``; digits beyond f64 precision survive) and normalizes only the
EXPONENT spelling (``1E5`` → ``1e+5``: lowercase ``e``, explicit sign —
serde's number scanner, not a float round-trip).

The Python reference below (``json.loads`` → compact ``json.dumps``) is valid
for **Python-normal-form inputs only** — inputs whose numeric literals survive
a float round-trip unchanged (``1.5``, ``-0.0``, integers). For wire-form
numbers it round-trips through float and genuinely diverges from Rust:

    ``[{"price":1.50}]`` → Rust hashes the preserved literal ``1.50``;
    the Python reference parses to ``1.5`` → a DIFFERENT key. Same for
    ``1E5`` (Rust ``1e+5`` vs Python ``100000.0``), ``1e400`` (Rust
    ``1e+400`` vs Python ``Infinity``), and high-precision decimals
    (float rounding).

Production recovery is unaffected — the Python mirror copies the Rust hash
verbatim and never recomputes it — but any future Python-side key
RECOMPUTATION (including §4.2 verification tooling) must reproduce serde's
canonical BYTES (in practice: hash the raw wire text for inputs already in
canonical shape, or keep copying the Rust-emitted hash) — never
``json.loads`` → ``json.dumps`` over parsed values. The wire-form vectors
below pin that contract from the canonical text on both sides;
``test_python_reference_is_scoped_to_python_normal_form`` keeps the
divergence itself executable.
"""

from __future__ import annotations

import hashlib
import json

from furl_ctx.transforms.smart_crusher import SmartCrusher

# Canonical form = serde_json::to_string(items): compact (no spaces), keys as
# emitted. For these vectors (empty / scalars / single-key dicts / non-ASCII /
# control chars — all Python-normal-form) Python's compact dump is
# byte-identical to serde_json's, so the reference below reproduces the Rust
# canonical. Wire-form numbers do NOT belong in this list — see _WIRE_VECTORS.
_VECTORS: list[tuple[list[object], str]] = [
    ([], "4f53cda18c2baa0c0354bb5f"),
    (["alpha", "beta", "gamma"], "a3e185260009ab5be7bb16f3"),
    ([1, 2, 3, 4, 5], "f5baf0e4336fd53b4c82b453"),
    ([{"id": 1}, {"id": 2}, {"id": 3}], "d99179347cb13877fc9057e0"),
    # Non-ASCII: both serializers emit raw UTF-8 (Python via
    # ensure_ascii=False; serde_json always) — the agreeing subset
    # extends beyond ASCII scalars.
    (["café", "日本語", "naïve"], "3a6991f2cdbff9637f9d8ec2"),
    # Control characters: both serializers emit the short escapes \n / \t
    # and \\u00XX escapes for other unprintables — byte-identical text.
    (["line1\nline2", "tab\there", "bell\x07"], "333b058285a5aa142b93c6bd"),
]

# Wire-form vectors (TEST-33): the pinned hash is SHA-256[:24] over the serde
# CANONICAL text — decimal literals preserved verbatim, exponent spelling
# normalized to ``e{sign}`` — NOT over parsed values. Each string here IS the
# canonical (byte-for-byte what the Rust producer hashes); the Rust half pins
# the identical (canonical, hash) pairs AND that parsing the wire input
# re-serializes to exactly these bytes. The Python reference CANNOT reproduce
# the numeric ones (it would float-normalize them) — that is the scoping this
# file's docstring documents.
_WIRE_VECTORS: list[tuple[str, str]] = [
    ('[{"price":1.50}]', "86cf954ca9f301c4cf6f9832"),  # trailing zero preserved verbatim
    ("[1e+5]", "5c20cc153829a59a47596031"),  # canonical of wire `[1E5]` (serde respells)
    ("[1e+400]", "7e9854d86909950904d96294"),  # canonical of wire `[1e400]`; Python → inf
    ("[2.5000000000000000000000000001]", "44a8948fa037883453d1adec"),  # beyond f64 precision
    # Non-ASCII and control-char wire forms — these AGREE with the Python
    # reference (pinned identically in _VECTORS above); listed here too so
    # the canonical-text contract covers the full grammar, not just the
    # divergent numeric corner.
    ('["café","日本語","naïve"]', "3a6991f2cdbff9637f9d8ec2"),
    ('["line1\\nline2","tab\\there","bell\\u0007"]', "333b058285a5aa142b93c6bd"),
]


def _canonical(items: list[object]) -> str:
    """Python REFERENCE canonical — valid for Python-normal-form inputs only
    (see module docstring). Wire-form numbers are normalized by the float
    round-trip and diverge from the authoritative Rust canonical."""
    return json.dumps(items, separators=(",", ":"), ensure_ascii=False)


def _hash_canonical(items: list[object]) -> str:
    return hashlib.sha256(_canonical(items).encode("utf-8")).hexdigest()[:24]


def _hash_raw(canonical_text: str) -> str:
    """The canonical-text form of the recovery key: SHA-256[:24] over the
    exact canonical BYTES (24 hex / 96 bits, T3). This is how a Python-side
    key recomputation must be done — over serde's canonical text (== the raw
    wire text whenever the input is already in canonical shape), never via
    ``json.loads`` → ``json.dumps`` (which float-normalizes numeric literals)."""
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()[:24]


def test_pinned_vectors_match_the_rust_literals() -> None:
    """Each literal is the SHA-256[:24] of the exact canonical bytes — pinned
    identically in ``crusher.rs::tests::hash_canonical_pinned_vectors``. A typo
    in either side's literal, or a hashing/truncation change, fails here."""
    for items, expected in _VECTORS:
        assert _hash_canonical(items) == expected, (
            f"canonical {_canonical(items)!r} → {_hash_canonical(items)} ≠ pinned {expected}"
        )


def test_wire_form_vectors_match_the_rust_literals() -> None:
    """TEST-33: the wire-form half of the lock. Hashes are computed from the
    serde CANONICAL text (decimal literals verbatim, exponents respelled
    ``e{sign}``) and pinned identically in
    ``crusher.rs::tests::hash_canonical_wire_form_pinned_vectors`` — which
    also pins that ``canonical_array_json`` REPRODUCES these exact bytes from
    parsed wire input (the ``arbitrary_precision`` literal preservation this
    contract rides on)."""
    for canonical, expected in _WIRE_VECTORS:
        assert _hash_raw(canonical) == expected, (
            f"canonical {canonical!r} → {_hash_raw(canonical)} ≠ pinned {expected}"
        )


def test_python_reference_is_scoped_to_python_normal_form() -> None:
    """Executable form of the docstring's scope statement: for wire-form
    numbers the Python reference float-NORMALIZES the literal and computes a
    key that differs from the authoritative canonical-text (Rust) key. If
    this ever starts PASSING through the reference path, the two canonicals
    have converged and the scoping (plus §4.2's canonical-text rule) can be
    revisited."""
    raw = '[{"price":1.50}]'
    parsed: list[object] = json.loads(raw)
    # The reference normalizes the literal…
    assert _canonical(parsed) == '[{"price":1.5}]'
    # …and therefore computes a DIFFERENT key than the canonical-text contract.
    assert _hash_canonical(parsed) != _hash_raw(raw), (
        "Python reference unexpectedly reproduced the wire-form key — the "
        "canonicals have converged; update the parity-scope docs"
    )
    # Same divergence class for exponent forms and float overflow: the
    # reference collapses `1E5` to `100000.0` and `1e400` to `Infinity`,
    # never serde's respelled canonicals `1e+5` / `1e+400`.
    assert _canonical(json.loads("[1E5]")) == "[100000.0]"
    assert _hash_canonical(json.loads("[1E5]")) != _hash_raw("[1e+5]")
    assert _canonical(json.loads("[1e400]")) == "[Infinity]"
    assert _hash_canonical(json.loads("[1e400]")) != _hash_raw("[1e+400]")


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

    (Input is Python-normal-form — ASCII strings — so the reference is in
    scope; the wire-form sibling below locks the divergent-number case.)
    """
    # 600 distinct lines → a guaranteed row-drop on the live crush() path
    # (the 1a parity suite uses the same shape). Fixed seed → deterministic
    # canonical → a stable expected hash.
    items: list[object] = [f"ccr-parity-line-{i}" for i in range(600)]
    expected = _hash_canonical(items)  # SHA-256[:24] over the full-array canonical

    crusher = SmartCrusher()
    r = crusher._rust.crush(json.dumps(items, ensure_ascii=False), "", 1.0)

    assert r.ccr_hashes, "fixed 600-line array must drop and surface a typed hash"
    assert expected in set(r.ccr_hashes), (
        f"Rust crush() emitted {sorted(set(r.ccr_hashes))}, which does NOT "
        f"contain the Python-reference key {expected} over the same canonical "
        f"— Py↔Rust hash_canonical parity is broken (silent recovery loss)."
    )


def test_rust_crush_emits_the_raw_text_hash_for_wire_form_numbers() -> None:
    """TEST-33 live-path lock: wire-form numeric input driven through the
    production Rust ``crush()`` emits the key of the LITERAL-PRESERVING
    canonical (SHA-256[:24] over the raw text) — and does NOT emit the key
    the Python reference would compute from parsed-and-normalized values.

    This is the executable proof that a Python-side recomputation for
    arbitrary inputs must hash the raw wire text (``_hash_raw``), never
    ``json.loads`` → ``json.dumps`` output.
    """
    # 600 distinct wire-form decimals, every literal carrying a trailing
    # zero the float round-trip would strip (0.10 → 0.1). Compact from the
    # start, so serde's canonical re-serialization is byte-identical to
    # this exact text (preserve_order + arbitrary_precision).
    raw = "[" + ",".join(f"{i}.10" for i in range(600)) + "]"
    expected_raw = _hash_raw(raw)
    normalized = _hash_canonical(json.loads(raw))  # what the reference would say
    assert expected_raw != normalized, "fixture must exercise the divergent form"

    crusher = SmartCrusher()
    r = crusher._rust.crush(raw, "", 1.0)

    assert r.ccr_hashes, "fixed 600-number array must drop and surface a typed hash"
    emitted = set(r.ccr_hashes)
    assert expected_raw in emitted, (
        f"Rust crush() emitted {sorted(emitted)}, which does NOT contain the "
        f"raw-text key {expected_raw} — the literal-preserving canonical "
        f"contract is broken (silent recovery loss for wire-form numbers)."
    )
    assert normalized not in emitted, (
        f"Rust crush() emitted the PYTHON-NORMALIZED key {normalized} — the "
        "canonicals have converged; update the parity-scope docs and vectors."
    )
