"""Full-retrieve byte-exactness is split by ROUTE, not by content type.

``LIBRARY.md`` ("Retrieve — full or sliced") and ``CCR-RETENTION.md`` (the
delivered-guarantee block) both state this contract in prose. Nothing executed
it, so a change to the crush path's canonical serializer
(``canonical_array_json`` == ``serde_json::to_string``, e.g. a serializer swap,
enabling ``preserve_order``, or a separator change) would silently turn both
documents into lies with no test going red. This file is the executable form of
what they promise:

* **WHOLE-BLOB route** — raw text, or JSON too irregular to crush. The router's
  last-resort offload (``router_engine._ccr_offload``) stores the original
  VERBATIM, so a full ``retrieve(hash)`` is BYTE-EXACT, and the key is
  ``SHA-256(original)[:24]`` over the raw bytes — a BYTE identifier: change one
  code point, get a different hash.

* **CRUSH route** — a tabular JSON array the SmartCrusher drops rows from. It
  stores ``canonical_array_json(items)`` (``smart_crusher/persist.rs:202``), the
  re-serialized canonical form, and keys it with
  ``SHA-256(canonical)[:24]`` — a SEMANTIC identifier. So a full retrieve returns
  CANONICAL bytes rather than the caller's bytes (identical only if the input was
  already canonical), and two byte-different inputs that canonicalize identically
  collapse to ONE stored entry under ONE hash.

The practical consequence for a caller, and the reason this is worth pinning: a
hash means different things on the two paths. On the whole-blob path it
identifies BYTES; on the crush path it identifies a VALUE. Code that compares
hashes across routes expecting one meaning is wrong on the other.

CONTENT CAPABILITY. Reaching the code is necessary but not sufficient — the
payload has to be able to EXPRESS the failure. An ASCII payload cannot see an
encoding defect, so the whole-blob fixture carries NUL, CJK, bidi overrides and
latin-1-sourced code points, and the crush fixture carries non-ASCII values.

BACKEND. Memory only, deliberately. The route split is backend-independent, and
hostile-byte fidelity against the durable sqlite backend is already covered by
nine legs in ``tests/matrix/test_encoding_shapes.py``. A sqlite fan here would
duplicate tests that cannot fail differently — cost without coverage.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import furl_ctx.compress as _compress_mod
from furl_ctx import compress, retrieve
from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store

_MODEL = "gpt-4o"


@pytest.fixture(autouse=True)
def _isolate_ccr_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh store AND fresh pipeline per test, in a throwaway workspace.

    ``compress()`` reuses a process-wide pipeline whose Tier-2 result cache
    outlives a store reset, so a second compress of identical content would hit
    the cache and skip the persist. Dropping the singleton gives each test
    fresh-process semantics.
    """
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("FURL_CCR_BACKEND", "memory")
    _compress_mod._pipeline = None
    reset_compression_store()
    yield
    reset_compression_store()
    _compress_mod._pipeline = None


def _run(content: str) -> Any:
    return compress([{"role": "tool", "content": content}], model=_MODEL)


def _entry_count() -> int:
    """Live entry count via the PUBLIC stats surface.

    Deliberately not ``_backend.keys()``: ``keys`` is not on the
    ``CompressionStoreBackend`` Protocol, so reaching for it both breaks typing
    and couples this test to one backend implementation.
    """
    return int(get_compression_store().get_stats()["entry_count"])


# ── fixtures ────────────────────────────────────────────────────────────────


def _hostile_text(marker: str = "") -> str:
    """Whole-blob fixture: past the 4000-char offload floor, and carrying bytes
    an encoding defect can actually mangle (NUL, CJK, bidi, latin-1)."""
    latin1 = bytes(range(0x80, 0x100)).decode("latin-1")
    return (
        "".join(
            f"row{i}\x00field{i} 记录{i} ユーザー ‮rtl{i}‬ café{i} {latin1}\n" for i in range(200)
        )
        + marker
    )


def _tabular_rows() -> list[dict]:
    """Crush fixture: tabular enough to route structurally and drop rows, with
    non-ASCII values so an encoding defect is expressible here too."""
    return [
        {"id": i, "level": "INFO", "msg": f"记录{i % 7} ユーザー ‮rtl‬ café", "ms": i * 3}
        for i in range(400)
    ]


def _compact(rows: list[dict]) -> str:
    return json.dumps(rows, separators=(",", ":"), ensure_ascii=False)


def _spaced(rows: list[dict]) -> str:
    """Byte-different from :func:`_compact`, canonically identical to it."""
    return json.dumps(rows, separators=(", ", ": "), ensure_ascii=False)


# ── WHOLE-BLOB route: verbatim bytes, byte identifier ───────────────────────


def test_whole_blob_route_full_retrieve_is_byte_exact() -> None:
    """A1: the whole-blob route stores VERBATIM, so full retrieve is byte-exact."""
    content = _hostile_text()
    result = _run(content)

    assert result.error is None, f"unexpected fail-open: {result.error!r}"
    assert result.ccr_hashes, (
        "whole-blob fixture did not offload — the byte-exactness claim below would "
        "be vacuous; regrow the fixture past the offload floor"
    )
    payload = retrieve(result.ccr_hashes[0])
    assert payload is not None, "surfaced hash dangles — original unrecoverable"
    # Route guard: the whole-blob route stores raw text, NOT a JSON array.
    with pytest.raises((json.JSONDecodeError, ValueError)):
        json.loads(payload)

    assert payload == content, (
        "whole-blob full retrieve is NOT byte-exact — LIBRARY.md promises this "
        "route returns the stored original verbatim"
    )
    # The bytes an encoding defect would mangle actually made the round trip.
    assert "\x00" in payload and "记录" in payload and "‮" in payload


def test_whole_blob_hash_is_a_byte_identifier() -> None:
    """A2: on the whole-blob path the key is SHA-256 over the RAW bytes, so a
    one-code-point edit must produce a different hash."""
    a = _hostile_text()
    b = _hostile_text().replace("café0", "cafe0", 1)
    assert a != b and len(a) == len(b), "fixture bug: inputs must differ by one code point"

    ha = _run(a).ccr_hashes
    assert ha, "fixture did not offload"
    # Fresh pipeline+store so the second compress is a genuine cold pass.
    _compress_mod._pipeline = None
    reset_compression_store()
    hb = _run(b).ccr_hashes
    assert hb, "fixture did not offload"

    assert ha != hb, (
        "whole-blob hash did NOT change for byte-different content — it is "
        "supposed to be a BYTE identifier on this route"
    )


# ── CRUSH route: canonical bytes, semantic identifier ───────────────────────


def test_crush_route_full_retrieve_is_canonical_not_input_bytes() -> None:
    """B1: the crush route stores the canonical re-serialization, so a full
    retrieve of a NON-canonical input is NOT byte-identical to that input —
    while remaining semantically complete."""
    rows = _tabular_rows()
    spaced = _spaced(rows)
    # Vacuity guard: if the input were already canonical, "differs from input"
    # would be trivially false and this test would prove nothing.
    assert spaced != _compact(rows), "fixture bug: input must NOT already be canonical"

    result = _run(spaced)
    assert result.error is None, f"unexpected fail-open: {result.error!r}"
    assert result.ccr_hashes, "crush fixture did not drop rows — assertion would be vacuous"

    payload = retrieve(result.ccr_hashes[0])
    assert payload is not None, "drop pointer dangles — dropped rows unrecoverable"
    # Route guard: the crush route stores a JSON array.
    parsed = json.loads(payload)
    assert isinstance(parsed, list), "crush payload must be a JSON array of rows"

    assert payload != spaced, (
        "crush full retrieve came back byte-identical to a NON-canonical input — "
        "either the route now stores verbatim, or the fixture stopped crushing. "
        "LIBRARY.md documents this route as returning canonical bytes"
    )
    assert parsed == rows, (
        "crush retrieve is not semantically complete — the rows must survive even "
        "though the bytes are re-serialized (no silent loss)"
    )
    assert "记录" in payload, "non-ASCII values did not survive the canonical round trip"


def test_crush_route_hash_is_a_semantic_identifier() -> None:
    """B2: two byte-different but canonically identical inputs get the SAME hash,
    because the crush key is taken over the canonical form."""
    rows = _tabular_rows()
    compact, spaced = _compact(rows), _spaced(rows)
    assert compact != spaced, "fixture bug: renderings must differ in bytes"
    assert json.loads(compact) == json.loads(spaced), "fixture bug: must be canonically equal"

    h_compact = _run(compact).ccr_hashes
    assert h_compact, "compact fixture did not drop rows"
    _compress_mod._pipeline = None
    reset_compression_store()
    h_spaced = _run(spaced).ccr_hashes
    assert h_spaced, "spaced fixture did not drop rows"

    assert h_compact == h_spaced, (
        f"crush hashes diverged for canonically identical inputs "
        f"({h_compact} vs {h_spaced}) — the crush key is documented as a SEMANTIC "
        f"identifier taken over canonical_array_json, not over the input bytes"
    )


def test_crush_route_semantic_identity_is_separator_only_not_key_order() -> None:
    """B2-boundary: "canonical" here is the serializer's normal form, which
    PRESERVES key insertion order — it is not a sorted-key canonicalisation
    (JCS/RFC-8785). So the semantic identity absorbs separator differences but
    NOT key reordering: two dict-equal inputs whose keys are written in a
    different order canonicalise DIFFERENTLY and get different hashes.

    This is the boundary of the claim above, and it is the assertion that moves
    if ``canonical_array_json``'s serializer ever changes its ordering behaviour
    (e.g. serde_json's ``preserve_order`` being turned off, which would make
    these two collapse). Without this pin, that change is silent.
    """
    rows = _tabular_rows()
    reordered = [{"ms": r["ms"], "msg": r["msg"], "level": r["level"], "id": r["id"]} for r in rows]
    assert rows == reordered, "fixture bug: the two must be dict-equal"
    normal_json, reordered_json = _compact(rows), _compact(reordered)
    assert normal_json != reordered_json, "fixture bug: key order must differ in bytes"

    h_normal = _run(normal_json).ccr_hashes
    assert h_normal, "normal-order fixture did not drop rows"
    _compress_mod._pipeline = None
    reset_compression_store()
    h_reordered = _run(reordered_json).ccr_hashes
    assert h_reordered, "reordered fixture did not drop rows"

    assert h_normal != h_reordered, (
        f"key-reordered inputs collapsed to the same hash ({h_normal}) — the crush "
        f"key is taken over an ORDER-PRESERVING serialization, so this is the "
        f"boundary of its semantic identity. If the serializer now sorts keys, "
        f"the documented 'canonicalize identically' set just widened and "
        f"LIBRARY.md needs updating with it"
    )


def test_crush_route_canonically_identical_inputs_collapse_to_one_entry() -> None:
    """B3: the semantic key means both renderings resolve to ONE stored entry —
    the consequence LIBRARY.md spells out for a caller."""
    rows = _tabular_rows()

    first = _run(_compact(rows))
    assert first.ccr_hashes, "compact fixture did not drop rows"
    count_after_first = _entry_count()
    assert count_after_first, "nothing persisted — collapse assertion would be vacuous"

    second = _run(_spaced(rows))
    assert second.ccr_hashes, "spaced fixture did not drop rows"
    count_after_second = _entry_count()

    assert count_after_second == count_after_first, (
        f"the second, byte-different rendering added "
        f"{count_after_second - count_after_first} store entr(ies) — canonically "
        f"identical inputs are documented to collapse to one"
    )
    # Same entry, therefore necessarily the same key.
    assert set(second.ccr_hashes) == set(first.ccr_hashes), (
        "the two renderings surfaced different hashes despite not growing the store"
    )
    # And the surviving entry serves both markers.
    payload = retrieve(second.ccr_hashes[0])
    assert payload is not None and json.loads(payload) == rows
