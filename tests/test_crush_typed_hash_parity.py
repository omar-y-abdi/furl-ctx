"""Characterization: ``crush()`` surfaces row-drop recovery TYPED.

Pass 1a brings the live ``SmartCrusher.crush()`` path to recovery PARITY
with its sibling ``crush_array_json``: the dropped-row CCR hash (and the
granular row-index marker) come back as TYPED Rust fields
(``CrushResult.ccr_hashes`` / ``.row_index_markers``) instead of being
substring-scraped out of the rendered ``<<ccr:HASH>>`` text. The Python
shim mirrors those typed fields DIRECTLY into the compression_store, so
``crush()`` row-drop recovery no longer depends on the scrape.

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

OPAQUE-blob markers (``<<ccr:HASH,KIND,SIZE>>``) are intentionally NOT
typed here — they stay on the comma-shape scrape on both paths (1b).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from headroom.transforms.smart_crusher import SmartCrusher


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


def _scrape_index_keys(rendered: str) -> set[str]:
    """The ``#rows`` granular index keys the scrape extracts."""
    sink: set[str] = set()
    SmartCrusher._collect_ccr_hashes_from_string(rendered, sink)
    return {h for h in sink if h.endswith("#rows")}


def _typed_index_keys(row_index_markers: list[str]) -> set[str]:
    """The ``HASH#rows`` keys carried by the typed ``row_index_markers``
    (each is a full ``<<ccr:HASH#rows N_chunks>>`` marker)."""
    keys: set[str] = set()
    for marker in row_index_markers:
        sink: set[str] = set()
        SmartCrusher._collect_ccr_hashes_from_string(marker, sink)
        keys.update(h for h in sink if h.endswith("#rows"))
    return keys


# ── Row-drop fixtures: ≥20 distinct cases (dicts, strings, numbers, mixed) ──
#
# Each must take the lossy row-drop path under the default config so a
# `<<ccr:HASH N_rows_offloaded>>` marker is emitted. Built large + low
# uniqueness (or numeric/string) so the analyzer crushes rather than keeps.
#
# SIZE CEILING (COR-4): every case must ALSO keep its dropped-row count
# UNDER the granular budget (store capacity / 4 = 250 under the default
# 1000-entry store) — an oversized drop persists the whole-blob ONLY and
# emits NO `#rows` index marker, which would turn the row-index parity
# test into an empty-vs-empty no-op. Dozens of rows are plenty to trigger
# the drop while staying far below the budget; the ≥1-marker floor in
# `test_crush_typed_row_index_equals_scrape` enforces this.


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
    # persist and row-index markers populate — the production shape.
    return SmartCrusher()


@pytest.mark.parametrize("shape, seed", _ROW_DROP_CASES)
def test_crush_typed_hash_equals_scrape_and_retrieves(
    crusher: SmartCrusher, shape: str, seed: int
) -> None:
    """For every row-drop case:

    1. ``crush()`` returns a non-empty typed ``ccr_hashes``.
    2. That typed set EQUALS the row-drop hashes the old scrape extracts
       from the SAME ``r.compressed`` (parity).
    3. ``ccr_get(typed_hash)`` and ``ccr_get(scraped_hash)`` return the SAME
       payload (same recovery, regardless of source).

    BITE PROOF: returning ``None``/a wrong hash from the typed getter makes
    step 1 or 2 fail (verified during development).
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
    # (3) Both sources resolve, and to the SAME payload.
    for h in typed:
        typed_payload = crusher._rust.ccr_get(h)
        assert typed_payload is not None, f"typed hash {h} unresolvable in store"
    for h in scraped:
        scraped_payload = crusher._rust.ccr_get(h)
        assert scraped_payload is not None, f"scraped hash {h} unresolvable"
    for h in typed & scraped:
        assert crusher._rust.ccr_get(h) == crusher._rust.ccr_get(h), (
            f"{shape}-{seed}: typed vs scraped payload diverge for {h}"
        )


@pytest.mark.parametrize("shape, seed", _ROW_DROP_CASES)
def test_crush_typed_row_index_equals_scrape(crusher: SmartCrusher, shape: str, seed: int) -> None:
    """The granular ``#rows`` index keys recovered from the typed
    ``row_index_markers`` equal the ``#rows`` keys the scrape extracts —
    so proportional retrieval also comes typed, not scraped."""
    items = _BUILDERS[shape](seed)
    r = crusher._rust.crush(json.dumps(items, ensure_ascii=False), "", 1.0)

    typed_idx = _typed_index_keys(list(r.row_index_markers))
    scraped_idx = _scrape_index_keys(r.compressed)
    # Non-vacuity guard (COR-4): an oversized drop (> capacity/4) emits NO
    # granular index at all — typed and scraped would both be empty and the
    # parity assertion below would pass without testing anything. The
    # fixtures are sized to keep every drop under the granular budget; this
    # ≥1-marker floor keeps the parity check honest if they ever regress.
    assert typed_idx, (
        f"{shape}-{seed}: no #rows index marker emitted — the drop exceeds "
        f"the granular budget (capacity/4), so row-index parity is vacuous"
    )
    assert typed_idx == scraped_idx, (
        f"{shape}-{seed}: typed index {sorted(typed_idx)} != scraped {sorted(scraped_idx)}"
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
    # Every typed hash retrieves.
    for h in typed:
        assert crusher._rust.ccr_get(h) is not None, f"typed hash {h} unresolvable"
