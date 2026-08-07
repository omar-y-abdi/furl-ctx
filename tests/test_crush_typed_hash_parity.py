"""Characterization: the engine surfaces CCR recovery TYPED (§4.2).

The live ``SmartCrusher.crush()`` path surfaces every recovery ref as a
typed carrier: the dropped-row CCR hashes AND — since §4.2 R2/R3 — the
opaque-blob substitutions, all carried on ``CrushResult.dropped_refs`` (a
list of ``furl_ctx._core.DroppedRef``) instead of being substring-scraped
out of the rendered ``<<ccr:...>>`` text. The Python shim mirrors those
typed refs DIRECTLY into the compression_store; the back-compat
``ccr_hashes`` getter stays byte-identical to the retired field.

These tests PIN that contract and were written to BITE: stubbing the
typed getter to drop/wrong a hash turns them RED (proven during
development — see the test docstrings). They are deliberately kept in a
SEPARATE file from ``test_ccr_recovery_invariant.py`` so the
recovery-invariant count is unchanged.

Scope note: ``crush()`` recurses and can drop rows from MANY sub-arrays
(dict via ``crush_array``; string/number/mixed via ``ccr_dropped_sentinel``),
so the typed field is PLURAL — unlike the sibling's single ``ccr_hash``
for one top-level array. The multi-array case below pins that multiplicity
(the singular-hash model would silently leave extra drops on the scrape).

The typed-vs-scraped comparison for OPAQUE refs is directional
(typed ⊇ scraped): column-encoding folds (e.g. the CSV Affix fold) can
hide verbatim markers from the raw-text scrape, which the typed path
still carries — see ``crates/furl-core/tests/typed_dropped_refs.rs``
for the Rust-side pin of that discovery. The scrape's OWN false-positive
class (literal ``<<ccr:...>>`` text quoted inside payloads) is pinned
here: scraped but never typed.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from furl_ctx.ccr import marker_grammar
from furl_ctx.transforms.smart_crusher import SmartCrusher


def _scrape_row_drop_hashes(rendered: str) -> set[str]:
    """The pre-1a recovery scrape, restricted to ROW-DROP shapes.

    ``_collect_ccr_hashes_from_string`` returns three shapes: the bare
    row-drop hash (``abc``), the granular index key (``abc#rows``), and the
    comma-delimited opaque hash. This helper keeps only the row-drop bare
    hashes — the exact set ``crush()`` recovery used to depend on the scrape
    for, and the set the typed ``ccr_hashes`` must now reproduce.
    """
    sink: set[str] = set()
    SmartCrusher._collect_ccr_hashes_from_string(rendered, sink)
    return {h for h in sink if not h.endswith("#rows")}


# ── Row-drop fixtures: ≥20 distinct cases (dicts, strings, numbers, mixed) ──
#
# Each must take the lossy row-drop path under the default config so a
# `<<ccr:HASH N_rows_offloaded>>` marker is emitted and a typed ccr_hash is
# surfaced. Built large + low uniqueness (or numeric/string) so the analyzer
# crushes rather than keeps.


def _dict_case(seed: int) -> list[dict]:
    # HIGH-ENTROPY dict rows → the lossy ``smart_sample`` row-drop path (a
    # repetitive array would compact losslessly to a CSV table with NO drop,
    # which is correct but not a row-drop case to characterize). Near-unique
    # id/commit/msg per row from a seeded SHA stream forces row-drop; the
    # survivors are still rendered as a compacted table
    # (``smart_sample+compact:table``), exercising the survivor-compacted
    # DICT drop arm specifically.
    rows: list[dict] = []
    for i in range(60):
        h = hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()
        rows.append(
            {
                "id": h[:32],
                "commit": h[32:64],
                "svc": ["api", "worker"][i % 2],
                "lvl": ["INFO", "WARN"][i % 2],
                "msg": f"req {h[8:20]} done {h[20:28]}",
            }
        )
    return rows


def _string_case(seed: int) -> list[str]:
    return [f"log-line-{seed}-payload" for _ in range(80)]


def _number_case(seed: int) -> list[int]:
    return [seed for _ in range(80)]


def _mixed_case(seed: int) -> list:
    return [f"event-{seed}" if i % 2 == 0 else (seed + i) for i in range(100)]


_BUILDERS = {
    "dict": _dict_case,
    "string": _string_case,
    "number": _number_case,
    "mixed": _mixed_case,
}

# 5 seeds × 4 shapes = 20 distinct row-drop cases.
_ROW_DROP_CASES = [
    pytest.param(shape, seed, id=f"{shape}-{seed}")
    for shape in sorted(_BUILDERS)
    for seed in range(5)
]


@pytest.fixture
def crusher() -> SmartCrusher:
    # Default config wires a CCR store (with_default_ccr_store), so row-drops
    # persist — the production shape.
    return SmartCrusher()


@pytest.mark.parametrize("shape, seed", _ROW_DROP_CASES)
def test_crush_typed_hash_equals_scrape_and_retrieves(
    crusher: SmartCrusher, shape: str, seed: int
) -> None:
    """For every row-drop case:

    1. ``crush()`` returns a non-empty typed ``ccr_hashes``.
    2. That typed set EQUALS the row-drop hashes the old scrape extracts
       from the SAME ``r.compressed`` (parity).
    3. The PAYLOAD retrieved under every typed/scraped hash is the FULL
       ORIGINAL array — parsed-equal to the exact ``items`` this test
       built (TEST-11: the payload assertion is against ground truth,
       not a call compared with itself).

    BITE PROOF: returning ``None``/a wrong hash from the typed getter makes
    step 1 or 2 fail; corrupting the stored payload makes step 3 fail
    (verified during development).
    """
    items = _BUILDERS[shape](seed)
    content = json.dumps(items, ensure_ascii=False)
    r = crusher._rust.crush(content, "", 1.0)

    typed = set(r.ccr_hashes)
    scraped = _scrape_row_drop_hashes(r.compressed)

    # (1) A drop occurred and produced a typed hash.
    assert typed, (
        f"{shape}-{seed}: expected a typed ccr_hash on a row drop; "
        f"strategy={r.strategy!r}, head={r.compressed[:120]!r}"
    )
    # (2) Typed set == scraped row-drop set (byte-for-byte parity).
    assert typed == scraped, f"{shape}-{seed}: typed {sorted(typed)} != scraped {sorted(scraped)}"
    # (3) TEST-11 payload equality against GROUND TRUTH: each fixture is
    # ONE top-level array, so the whole-blob entry under the (single)
    # typed==scraped hash must decode to exactly the original items.
    for h in typed | scraped:
        payload = crusher._rust.ccr_get(h)
        assert payload is not None, f"hash {h} unresolvable in store"
        assert json.loads(payload) == items, (
            f"{shape}-{seed}: payload under {h} must be the FULL original "
            f"array (typed and scraped recovery resolve the same entry)"
        )


def test_crush_multi_array_surfaces_one_hash_per_drop(crusher: SmartCrusher) -> None:
    """★ Multiplicity pin: an object with TWO independent droppable
    sub-arrays yields TWO distinct typed hashes — the case the singular
    ``ccr_hash`` spec would silently leave half-recovered on the scrape.

    Asserts the typed set equals the scraped row-drop set (so NOTHING is
    left to the scrape) AND that there really are ≥2 distinct hashes.
    """
    arr_a = [{"id": i, "kind": "a", "status": "ok"} for i in range(300)]
    arr_b = [{"ref": i, "kind": "b", "level": "INFO"} for i in range(300)]
    doc = {"alpha": arr_a, "beta": arr_b}
    r = crusher._rust.crush(json.dumps(doc, ensure_ascii=False), "", 1.0)

    typed = set(r.ccr_hashes)
    scraped = _scrape_row_drop_hashes(r.compressed)
    assert len(typed) >= 2, (
        f"two droppable sub-arrays must surface ≥2 typed hashes, got {sorted(typed)} "
        f"(strategy={r.strategy!r})"
    )
    # Typed set fully covers the scrape — no drop is left scrape-only.
    assert typed == scraped, f"typed {sorted(typed)} != scraped {sorted(scraped)}"
    # Every typed hash retrieves, and each payload is one of the ORIGINAL
    # sub-arrays (ground-truth payload assertion, TEST-11).
    originals = [arr_a, arr_b]
    for h in typed:
        payload = crusher._rust.ccr_get(h)
        assert payload is not None, f"typed hash {h} unresolvable"
        assert json.loads(payload) in originals, (
            f"payload under {h} must be one of the original sub-arrays"
        )


# ─── §4.2 R3/R4: the typed FFI surface (DroppedRef across the boundary) ────


_BLOB_BASE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="


def _blob(seed: int, repeats: int = 8) -> str:
    return f"{seed}:" + _BLOB_BASE * repeats


def _scrape_opaque_hashes(rendered: str) -> set[str]:
    """The comma-shape (opaque) scrape — Python's pre-§4.2 discovery path.

    Same no-regex walk + hex alphabet as
    ``SmartCrusher._collect_ccr_hashes_from_string``, but a hash is collected
    ONLY when the character immediately after the hex run is a comma — that
    delimiter is what distinguishes the opaque shape (``<<ccr:HASH,KIND,SIZE>>``)
    from the space-delimited row-drop and ``#rows`` shapes. Lives here rather
    than on ``SmartCrusher`` because no production path has called it since
    §4.2: every fresh-output site consumes typed refs, and the result-cache
    bridge scrapes ALL shapes at once via ``_collect_ccr_hashes``.
    """
    sink: set[str] = set()
    idx = 0
    prefix = marker_grammar.CCR_PREFIX
    n = len(rendered)
    while True:
        start = rendered.find(prefix, idx)
        if start == -1:
            return sink
        cursor = start + len(prefix)
        end = cursor
        while end < n and rendered[end] in marker_grammar.HEX_ALPHABET:
            end += 1
        if end == cursor:
            # No hex after `<<ccr:` — not a real marker.
            idx = cursor
            continue
        # OPAQUE shape iff the delimiter after the hash is a comma.
        if end < n and rendered[end] == ",":
            sink.add(rendered[cursor:end].lower())
        idx = end


def _scrape_row_drop_only(rendered: str) -> set[str]:
    """Row-drop hashes STRICTLY: the full scrape minus ``#rows`` index
    keys minus comma-shape opaque hashes. (The module-level
    ``_scrape_row_drop_hashes`` only excludes ``#rows`` — adequate for
    the opaque-free row-drop fixtures above, wrong for mixed ones.)"""
    return _scrape_row_drop_hashes(rendered) - _scrape_opaque_hashes(rendered)


def _typed_by_kind(refs: list) -> tuple[set[str], set[str]]:
    """(row_drop hashes, opaque hashes) from typed refs."""
    row_drop = {d.hash for d in refs if d.kind_tag == "row_drop"}
    opaque = {d.hash for d in refs if d.kind_tag == "opaque"}
    return row_drop, opaque


def test_crush_dropped_refs_carry_row_drop_and_opaque(crusher: SmartCrusher) -> None:
    """``CrushResult.dropped_refs`` carries BOTH planes typed: the row
    drop (hash) and the opaque substitution (hash + wire kind + EXACT
    byte size)."""
    doc = {"payload": _blob(3), "rows": _dict_case(1)}
    r = crusher._rust.crush(json.dumps(doc, ensure_ascii=False), "", 1.0)

    row_drop, opaque = _typed_by_kind(list(r.dropped_refs))
    # Row-drop plane: typed refs == back-compat getter == scrape.
    assert row_drop == set(r.ccr_hashes) == _scrape_row_drop_only(r.compressed)
    # Opaque plane: typed covers the scrape (directional — encoding folds
    # can hide markers from the scrape, never from the typed path).
    scraped_opaque = _scrape_opaque_hashes(r.compressed)
    assert scraped_opaque <= opaque, (
        f"scrape-only opaque refs would be lost: {sorted(scraped_opaque - opaque)}"
    )
    assert opaque, "fixture must exercise the opaque substitution"
    # Exact byte size: the payload under each opaque hash is exactly
    # byte_size bytes (the marker only carries the humanized form).
    for d in r.dropped_refs:
        if d.kind_tag == "opaque":
            payload = crusher._rust.ccr_get(d.hash)
            assert payload is not None, f"opaque hash {d.hash} unresolvable"
            assert len(payload.encode()) == d.byte_size


def test_smart_crush_content_typed_carries_refs_covering_the_scrape(
    crusher: SmartCrusher,
) -> None:
    """The typed sibling surfaces every recovery ref the rendered output's
    text scrape would otherwise have to reconstruct: row-drops match the
    scrape exactly and the opaque hashes in the render are a subset of the
    typed opaque refs."""
    doc = {"payload": _blob(7), "rows": _dict_case(2)}
    content = json.dumps(doc, ensure_ascii=False)

    crushed, _was_modified, _info, refs = crusher._rust.smart_crush_content_typed(content, "", 1.0)

    row_drop, opaque = _typed_by_kind(list(refs))
    assert row_drop == _scrape_row_drop_only(crushed)
    assert _scrape_opaque_hashes(crushed) <= opaque
    assert opaque, "fixture must exercise the opaque substitution"


def test_compact_document_json_typed_carries_opaque_refs(
    crusher: SmartCrusher,
) -> None:
    """The document walker's typed sibling surfaces the opaque refs of
    every substitution the walk shipped — each resolvable in the CCR store
    and covering the hashes scraped from the rendered JSON."""
    doc = json.dumps({"summary": "ok", "payload": _blob(11)}, ensure_ascii=False)

    compacted, refs = crusher._rust.compact_document_json_typed(doc)
    _row_drop, opaque = _typed_by_kind(list(refs))
    assert opaque, "fixture must exercise the opaque substitution"
    assert _scrape_opaque_hashes(compacted) <= opaque
    for d in refs:
        assert d.kind_tag == "opaque", "the lossless walker never row-drops"
        assert crusher._rust.ccr_get(d.hash) is not None


def test_crush_array_json_carries_dropped_refs(
    crusher: SmartCrusher,
) -> None:
    """The ``crush_array_json`` dict surfaces ``dropped_refs`` — the typed
    row-drop ref whose hash agrees with the established ``ccr_hash``."""
    items = _dict_case(3)
    result = crusher.crush_array_json(json.dumps(items, ensure_ascii=False))

    ccr_hash = result["ccr_hash"]
    assert ccr_hash, "fixture must take the lossy row-drop path"
    refs = list(result["dropped_refs"])
    row_drop, _opaque = _typed_by_kind(refs)
    assert ccr_hash in row_drop


def test_literal_marker_text_is_scraped_but_never_typed(crusher: SmartCrusher) -> None:
    """★ The scrape's false-positive class (§4.2 R2): a payload QUOTING
    literal ``<<ccr:...>>`` text is picked up by the raw-text scrape but
    must never appear in the typed refs — the engine never emitted it."""
    planted = "<<ccr:aaaaaaaaaaaa,base64,2.0KB>>"
    items = [{"id": i, "note": f"saw {planted} in output"} for i in range(40)]
    r = crusher._rust.crush(json.dumps(items, ensure_ascii=False), "", 1.0)

    assert planted in r.compressed, "the planted literal must survive into the output"
    assert "aaaaaaaaaaaa" in _scrape_opaque_hashes(r.compressed), (
        "the scrape must exhibit its false positive for this pin to bite"
    )
    typed_hashes = {d.hash for d in r.dropped_refs}
    assert "aaaaaaaaaaaa" not in typed_hashes, (
        "the typed path must never carry a hash the engine did not emit"
    )
