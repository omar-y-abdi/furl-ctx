"""CCR recovery invariant on the public ``ContentRouter.compress()`` path.

The invariant under test:

    Whenever the engine drops or substitutes a distinct item, that item
    is recoverable by a consumer holding ONLY the output — the output
    carries a surfaced ``<<ccr:HASH>>`` pointer AND the original is in the
    CCR store under that hash (both the Rust process store via the crusher
    and the Python ``compression_store`` the proxy ``/v1/retrieve`` uses).

Two historical silent-loss classes are pinned here:

* **Defect 1** — marker-off / CCR-disabled drops. With
  ``ccr_inject_marker=False`` (or ``ccr_enabled=False``) the lossy
  row-drop path used to drop items, write the Rust store, but surface NO
  hash in the output → unrecoverable. The recovery pointer is now
  ALWAYS appended on a drop, regardless of the flag.

* **Defect 2** — lossless:table opaque-blob substitution. A long opaque
  blob field on the lossless path used to be replaced by a
  ``<<ccr:HASH,...>>`` marker whose original was NEVER persisted →
  unrecoverable. The original is now persisted under the marker hash.
"""

from __future__ import annotations

import base64
import json
import random
import re

import pytest

import furl_ctx.compress as _compress_mod
from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store
from furl_ctx.ccr import marker_grammar
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig

# Shared load-bearing fixtures (TEST-19): the tuned lossy fixture, its drop canary, and the recovery-comparison helpers live in one canonical
# place instead of being duplicated across recovery tests.
from tests._fixtures import assert_fixture_drops
from tests._fixtures import canonical_repr as _repr
from tests._fixtures import decode_csv_schema_into as _decode_csv_schema
from tests._fixtures import log_shaped_rows as _log_shaped_rows

# Recovery-floor parsers. These deliberately use a LOOSER lower bound (``{6,}``) than the strict consumer set ``marker_grammar.HASH_WIDTHS`` ({12, 24}):
# the recovery invariant must catch ANY surfaced ``<<ccr:`` pointer of plausible width, not just the two canonical widths the strict scanner accepts.
_PREFIX = re.escape(marker_grammar.CCR_PREFIX)
# Row-drop pointer:   <<ccr:HASH N_rows_offloaded>>
_DROP_RE = re.compile(rf"{_PREFIX}({marker_grammar.HEX_CLASS}{{6,}}) (\d+)_rows_offloaded>>")
# Opaque-blob pointer: <<ccr:HASH,KIND,SIZE>>
_OPAQUE_RE = re.compile(rf"{_PREFIX}({marker_grammar.HEX_CLASS}{{6,}}),[a-z0-9]+,[0-9.]+\w+>>")

# Every (ccr_enabled, ccr_inject_marker) combination that turns the retrieval-tool advertisement off. None of them may turn a drop into a silent loss.
_MARKER_OFF_MATRIX = [
    pytest.param(True, False, id="enabled-True_marker-False"),
    pytest.param(False, False, id="enabled-False_marker-False"),
    pytest.param(False, True, id="enabled-False_marker-True"),
]


def test_log_shaped_fixture_still_drops() -> None:
    """TEST-19 canary: the shared tuned fixture still routes lossy.

    Every recovery test below that consumes ``log_shaped_rows`` is vacuous
    if the fixture drifts onto the lossless path; this fails loudly first.
    """
    assert_fixture_drops()


def _collect(node: object, scalars: set[str], hashes: set[str]) -> None:
    if isinstance(node, list):
        for x in node:
            _collect(x, scalars, hashes)
    elif isinstance(node, dict):
        for v in node.values():
            _collect(v, scalars, hashes)
    elif isinstance(node, str):
        hashes.update(h for h, _n in _DROP_RE.findall(node))
        hashes.update(_OPAQUE_RE.findall(node))
        if "<<ccr:" not in node:
            scalars.add(_repr(node))
    else:
        scalars.add(_repr(node))


def _recover_from_output(
    items: list,
    *,
    ccr_enabled: bool,
    ccr_inject_marker: bool,
    store_scope: str = "union",
) -> set[str]:
    """Run the PUBLIC ``compress()`` path and return the set of distinct
    input reprs recoverable from the OUTPUT ALONE: kept scalars, lossless
    CSV rows, and CCR-store payloads keyed by a hash found in the output.

    ``store_scope`` selects which store(s) a surfaced hash may recover from:

    * ``"python"`` — the Python ``CompressionStore`` ONLY. This is the
      PRODUCTION path: MCP ``furl_retrieve`` (``ccr/mcp_server.py``) and
      ``furl_ctx.retrieve`` read ``store.retrieve(hash)`` and NOTHING else, so a
      recovery scored this way cannot be masked by a value that only survives in
      the process-local Rust crusher store.
    * ``"rust"`` — the Rust crusher store ONLY (``crusher.ccr_get``).
    * ``"union"`` — either store (the historical default). Strictly WEAKER than
      ``"python"`` for a production-fidelity claim: a Python (production) miss
      passes as long as the Rust copy happens to hold the value. Kept only for
      callers that assert "recoverable from somewhere", never for a production
      invariant.
    """
    cfg = ContentRouterConfig(ccr_enabled=ccr_enabled, ccr_inject_marker=ccr_inject_marker)
    router = ContentRouter(cfg)
    py_store = get_compression_store()

    result = router.compress(json.dumps(items, ensure_ascii=False))
    rendered = result.compressed

    try:
        tree = json.loads(rendered)
    except (json.JSONDecodeError, ValueError):
        tree = rendered

    scalars: set[str] = set()
    hashes: set[str] = set()
    _collect(tree, scalars, hashes)

    recovered = set(scalars)
    if isinstance(tree, str):
        _decode_csv_schema(tree, recovered)

    crusher = router._get_smart_crusher()
    for h in hashes:
        sources: list[str | None] = []
        if store_scope in ("union", "rust"):
            sources.append(crusher.ccr_get(h) if crusher is not None else None)
        if store_scope in ("union", "python"):
            sources.append(_py_payload(py_store, h))
        for src in sources:
            if src is None:
                continue
            try:
                parsed = json.loads(src)
            except (json.JSONDecodeError, ValueError):
                recovered.add(_repr(src))
                continue
            if isinstance(parsed, list):
                recovered.update(_repr(x) for x in parsed)
            else:
                recovered.add(_repr(parsed))
    return recovered


def _py_payload(store: object, h: str) -> str | None:
    entry = store.retrieve(h)
    if entry is not None and getattr(entry, "original_content", None):
        return entry.original_content
    return None


# --------------------------------------------------------------------------- # Defect 1 — non-dict drops surface a
# recovery pointer regardless of the flag. --------------------------------------------------------------------------- #

_NON_DICT_CASES = {
    "strings": [f"log-line-{i}-payload" for i in range(1000)],
    "numbers": list(range(1000)),
    "mixed": [f"event-{i}" if i % 2 == 0 else i for i in range(700)],
}


@pytest.fixture(params=["memory", "sqlite"])
def production_store(request, tmp_path, monkeypatch):
    """Score recovery against the Python ``CompressionStore`` production reads —
    under BOTH its in-memory backend AND the durable ``sqlite`` backend that
    production actually runs (``FURL_CCR_BACKEND=sqlite``, the MCP server's
    default). ``furl_retrieve`` resolves the Python store and NOTHING else, so
    this is the store whose miss the invariant must catch.

    Sandbox ``FURL_WORKSPACE_DIR`` (a per-test ``ccr.sqlite3``) and drop the
    store singleton + compression pipeline before and after, so the backend
    switch is clean, the sqlite leg is isolated, and no entry leaks across
    tests or backends. Yields the backend name for assertion messages.
    """
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("FURL_CCR_BACKEND", request.param)
    _compress_mod._pipeline = None
    reset_compression_store()
    yield request.param
    reset_compression_store()
    _compress_mod._pipeline = None


@pytest.mark.parametrize("ccr_enabled, ccr_inject_marker", _MARKER_OFF_MATRIX)
@pytest.mark.parametrize("shape", sorted(_NON_DICT_CASES))
def test_non_dict_drop_recovers_100pct_with_marker_off(
    shape: str, ccr_enabled: bool, ccr_inject_marker: bool, production_store: str
) -> None:
    # PRODUCTION-FIDELITY: score recovery against the Python store ALONE (the store furl_retrieve reads), under both its memory and sqlite backends. The
    # old Rust-OR-Python union could pass while the Python (production) mirror had regressed, whenever the process-local Rust copy still held the value
    items = _NON_DICT_CASES[shape]
    recovered = _recover_from_output(
        items,
        ccr_enabled=ccr_enabled,
        ccr_inject_marker=ccr_inject_marker,
        store_scope="python",
    )
    distinct = {_repr(x) for x in items}
    lost = distinct - recovered
    assert not lost, (
        f"{shape}: {len(lost)} of {len(distinct)} distinct items unrecoverable from the "
        f"PRODUCTION Python store alone (backend={production_store}, enabled={ccr_enabled}, "
        f"marker={ccr_inject_marker}); first: {list(lost)[:3]}"
    )


@pytest.mark.parametrize("ccr_enabled, ccr_inject_marker", _MARKER_OFF_MATRIX)
def test_dict_array_recovers_100pct_with_marker_off(
    ccr_enabled: bool, ccr_inject_marker: bool, production_store: str
) -> None:
    # Short distinct dict rows take the lossless:table path (CSV) — every row is present verbatim in the output, recoverable without CCR.
    items = [{"id": i, "msg": f"record-{i}-distinct-payload"} for i in range(1000)]
    recovered = _recover_from_output(
        items,
        ccr_enabled=ccr_enabled,
        ccr_inject_marker=ccr_inject_marker,
        store_scope="python",
    )
    distinct = {_repr(x) for x in items}
    lost = distinct - recovered
    assert not lost, (
        f"dict: {len(lost)} of {len(distinct)} rows unrecoverable from the PRODUCTION "
        f"Python store alone (backend={production_store}); {list(lost)[:3]}"
    )


def test_marker_off_actually_surfaces_pointer_in_output() -> None:
    # Directly assert the OUTPUT carries the `<<ccr:` pointer with the
    # flag off — the exact thing that was missing pre-fix (Defect 1).
    items = [f"log-line-{i}-payload" for i in range(1000)]
    cfg = ContentRouterConfig(ccr_enabled=False, ccr_inject_marker=False)
    result = ContentRouter(cfg).compress(json.dumps(items))
    assert "<<ccr:" in result.compressed
    assert _DROP_RE.search(result.compressed), "row-drop recovery pointer must be in the output"


# --------------------------------------------------------------------------- #
# Defect 2 — lossless:table opaque-blob substitutions persist the original.
# --------------------------------------------------------------------------- #


def _opaque_rows(n: int = 50) -> list[dict]:
    # Use fixed-seed opaque blobs so every matrix configuration deterministically takes the opaque-substitution
    # path. Random blobs can validly route to row-drop recovery and make this opaque-specific assertion flaky.
    rng = random.Random(0)
    return [
        {
            "id": i,
            "tag": "x",
            "data": base64.b64encode(bytes(rng.getrandbits(8) for _ in range(600))).decode(),
        }
        for i in range(n)
    ]


@pytest.mark.parametrize("ccr_enabled, ccr_inject_marker", _MARKER_OFF_MATRIX)
def test_opaque_blob_recovers_from_output_marker(
    ccr_enabled: bool, ccr_inject_marker: bool
) -> None:
    items = _opaque_rows()
    blobs = {it["data"] for it in items}

    cfg = ContentRouterConfig(ccr_enabled=ccr_enabled, ccr_inject_marker=ccr_inject_marker)
    router = ContentRouter(cfg)
    py_store = get_compression_store()

    result = router.compress(json.dumps(items))
    hashes = set(_OPAQUE_RE.findall(result.compressed))
    assert hashes, "opaque-blob substitution must surface <<ccr:HASH,...>> markers in the output"

    crusher = router._get_smart_crusher()
    rust_recovered = {crusher.ccr_get(h) for h in hashes if crusher.ccr_get(h) is not None}
    py_recovered = {p for h in hashes if (p := _py_payload(py_store, h)) is not None}

    assert blobs <= rust_recovered, (
        f"{len(blobs - rust_recovered)} opaque blobs unrecoverable from the Rust store "
        f"(enabled={ccr_enabled}, marker={ccr_inject_marker})"
    )
    assert blobs <= py_recovered, (
        f"{len(blobs - py_recovered)} opaque blobs unrecoverable from the Python "
        f"compression_store (enabled={ccr_enabled}, marker={ccr_inject_marker})"
    )


@pytest.mark.parametrize("ccr_enabled, ccr_inject_marker", _MARKER_OFF_MATRIX)
def test_lossy_survivor_table_recovers_100pct(
    ccr_enabled: bool, ccr_inject_marker: bool, production_store: str
) -> None:
    # The lossy-survivor CSV rendering (drop + sentinel LINE inside a JSON string) must satisfy the same invariant as every
    # other shape every distinct dropped row recoverable from the output alone. Scored PYTHON-ONLY, like the scalar/dict legs
    items = _log_shaped_rows()
    recovered = _recover_from_output(
        items,
        ccr_enabled=ccr_enabled,
        ccr_inject_marker=ccr_inject_marker,
        store_scope="python",
    )
    distinct = {_repr(x) for x in items}
    lost = distinct - recovered
    assert not lost, (
        f"lossy-survivor table: {len(lost)} of {len(distinct)} rows unrecoverable "
        f"from the PRODUCTION Python store alone (backend={production_store}, "
        f"enabled={ccr_enabled}, marker={ccr_inject_marker}); first: {list(lost)[:3]}"
    )


def test_row_drop_recovers_from_python_store_only(production_store: str) -> None:
    # Verify lossy row-drop recovery through the Python production store only, on both backends. This catches
    # mirror and durable surrogatepass/BLOB round-trip regressions that Rust-store recovery would hide.
    items = _log_shaped_rows()
    router = ContentRouter()
    py_store = get_compression_store()

    result = router.compress(json.dumps(items, ensure_ascii=False))
    tree = json.loads(result.compressed)
    assert isinstance(tree, str), "survivor compaction should ship a string rendering"
    sentinel = json.loads(tree.split("\n")[-1])
    assert "_ccr_dropped" in sentinel, "lossy drop must surface the _ccr_dropped sentinel"

    # Whole-blob recovery uses the bare row-drop hash. Python does not store the `HASH#rows`
    # key because its suffix is non-hex; retrieval therefore serves the full offloaded blob.
    drop_hashes = [h for h, _n in _DROP_RE.findall(sentinel["_ccr_dropped"])]
    assert drop_hashes, "row-drop sentinel must carry a <<ccr:HASH N_rows_offloaded>> pointer"

    # Recover via the Python CompressionStore ONLY — this is the production call (store.retrieve(hash).original_content). We deliberately
    # do NOT call crusher.ccr_get: a Python-mirror regression must fail here even while the Rust store still holds the bytes.
    recovered_rows: set[str] = set()
    for h in drop_hashes:
        payload = _py_payload(py_store, h)
        assert payload is not None, (
            f"row-drop hash {h} did NOT recover from the Python compression_store "
            f"via store.retrieve() — the production retrieval path is broken for "
            f"the lossy row-drop case (Rust ccr_get is NOT consulted here, by design)"
        )
        parsed = json.loads(payload)
        assert isinstance(parsed, list), "offloaded row-drop blob must be a JSON array of rows"
        recovered_rows.update(_repr(x) for x in parsed)

    # The mirror must actually carry the dropped rows.
    assert recovered_rows, "Python-store recovery yielded no rows (no-op mirror?)"
    distinct = {_repr(x) for x in items}
    assert recovered_rows <= distinct, (
        "recovered rows are not byte-exact inputs — Python-store payload is "
        "corrupted or re-encoded, not the original content"
    )

    # Compute the rows that survived in the output (present outside the sentinel) and confirm every dropped row is recoverable from the Python
    # store alone. ``_collect`` gathers kept scalars/rows; here we decode the survivor CSV body (everything before the sentinel line) and subtract.
    survivor_body = "\n".join(tree.split("\n")[:-1])
    survivors: set[str] = set()
    _decode_csv_schema(survivor_body, survivors)
    dropped = distinct - survivors
    assert dropped, "fixture must actually drop rows (lossy path) for this test to bite"
    lost = dropped - recovered_rows
    assert not lost, (
        f"{len(lost)} of {len(dropped)} dropped rows unrecoverable from the Python "
        f"compression_store ALONE (production path); first: {list(lost)[:3]}"
    )


def test_opaque_blob_default_config_recovers(production_store: str) -> None:
    # Default ContentRouter (markers on) — the production default.
    items = _opaque_rows()
    blobs = {it["data"] for it in items}
    router = ContentRouter()
    py_store = get_compression_store()
    result = router.compress(json.dumps(items))
    hashes = set(_OPAQUE_RE.findall(result.compressed))
    assert hashes
    crusher = router._get_smart_crusher()
    recovered = {crusher.ccr_get(h) for h in hashes if crusher.ccr_get(h) is not None}
    py_recovered = {p for h in hashes if (p := _py_payload(py_store, h)) is not None}
    assert blobs <= recovered
    # The PRODUCTION-default config asserted only the Rust store, so a total failure of ``CompressionStore.retrieve`` — the single call
    # production's ``furl_retrieve`` makes — passed here while its marker-off sibling (which already carries this assertion) went red.
    assert blobs <= py_recovered, (
        f"{len(blobs - py_recovered)} opaque blobs unrecoverable from the PRODUCTION "
        f"Python compression_store under the DEFAULT config (backend={production_store})"
    )


# .

_OPAQUE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _alphabet_blob(i: int, size: int) -> str:
    blob = (_OPAQUE_ALPHABET[i % 32 :] + _OPAQUE_ALPHABET * 8)[:size]
    assert len(blob) == size
    return blob


@pytest.mark.parametrize(
    ("cell_bytes", "expect_opaque"),
    [
        (255, False),  # below the floor: never opaque
        (256, False),  # AT the floor: still not opaque — the gate is `len <= 256`
        (257, True),  # above: every cell substituted with <<ccr:HASH,base64,SIZE>>
    ],
    ids=["below", "at", "above"],
)
def test_opaque_floor_boundary_triple(cell_bytes: int, expect_opaque: bool) -> None:
    items = [{"id": i, "tag": "x", "data": _alphabet_blob(i, cell_bytes)} for i in range(50)]
    result = ContentRouter(ContentRouterConfig()).compress(json.dumps(items))

    opaque_markers = _OPAQUE_RE.findall(result.compressed)
    if not expect_opaque:
        assert not opaque_markers, (
            f"{cell_bytes}B cells must NOT be opaque-substituted "
            f"(floor is inclusive-skip at 256), got {len(opaque_markers)} markers"
        )
        return

    assert len(opaque_markers) == len(items), (
        f"every {cell_bytes}B cell must be opaque-substituted, "
        f"got {len(opaque_markers)} of {len(items)}"
    )
    # Recovery invariant: every surfaced opaque hash resolves byte-exactly to
    # one of the ORIGINAL cell payloads — not merely "some entry exists".
    py_store = get_compression_store()
    original_blobs = {item["data"] for item in items}
    for hash_key in opaque_markers:
        payload = _py_payload(py_store, hash_key)
        assert payload is not None, f"opaque hash {hash_key} unbacked in the Python store"
        assert payload in original_blobs, (
            f"opaque hash {hash_key} resolves to bytes that are not any original cell"
        )
