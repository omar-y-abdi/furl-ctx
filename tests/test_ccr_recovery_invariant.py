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

from headroom.cache.compression_store import get_compression_store
from headroom.transforms.content_router import ContentRouter, ContentRouterConfig
from headroom.transforms.csv_schema_decoder import decode_csv_schema_rows

# Row-drop pointer:   <<ccr:HASH N_rows_offloaded>>
_DROP_RE = re.compile(r"<<ccr:([a-f0-9]{6,}) (\d+)_rows_offloaded>>")
# Opaque-blob pointer: <<ccr:HASH,KIND,SIZE>>
_OPAQUE_RE = re.compile(r"<<ccr:([a-f0-9]{6,}),[a-z0-9]+,[0-9.]+\w+>>")

# Every (ccr_enabled, ccr_inject_marker) combination that turns the
# retrieval-tool advertisement off. None of them may turn a drop into a
# silent loss.
_MARKER_OFF_MATRIX = [
    pytest.param(True, False, id="enabled-True_marker-False"),
    pytest.param(False, False, id="enabled-False_marker-False"),
    pytest.param(False, True, id="enabled-False_marker-True"),
]


def _repr(x: object) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def _decode_csv_schema(text: str, recovered: set[str]) -> None:
    """Decode a lossless CSV-schema body (``[N]{cols}\\n<rows>``) back to
    JSON objects via the documented reference decoder
    (``headroom.transforms.csv_schema_decoder``). Those rows are exactly
    reconstructible from the output — lossless — so they count as
    recovered-from-output-alone.

    The decoder understands every column encoding the CSV-schema
    formatter emits (constant fold, ditto marks, and the reversible
    column encodings); "recoverable" here means decode-and-compare
    equality, not verbatim string presence.
    """
    rows = decode_csv_schema_rows(text)
    if rows is None:
        return
    for row in rows:
        recovered.add(_repr(row))


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


def _recover_from_output(items: list, *, ccr_enabled: bool, ccr_inject_marker: bool) -> set[str]:
    """Run the PUBLIC ``compress()`` path and return the set of distinct
    input reprs recoverable from the OUTPUT ALONE: kept scalars, lossless
    CSV rows, and CCR-store payloads keyed by a hash found in the output.
    Recovery is checked against BOTH the Rust store and the Python store.
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
        sources = [
            crusher.ccr_get(h) if crusher is not None else None,
            _py_payload(py_store, h),
        ]
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


# --------------------------------------------------------------------------- #
# Defect 1 — non-dict drops surface a recovery pointer regardless of the flag.
# --------------------------------------------------------------------------- #

_NON_DICT_CASES = {
    "strings": [f"log-line-{i}-payload" for i in range(1000)],
    "numbers": list(range(1000)),
    "mixed": [f"event-{i}" if i % 2 == 0 else i for i in range(700)],
}


@pytest.mark.parametrize("ccr_enabled, ccr_inject_marker", _MARKER_OFF_MATRIX)
@pytest.mark.parametrize("shape", sorted(_NON_DICT_CASES))
def test_non_dict_drop_recovers_100pct_with_marker_off(
    shape: str, ccr_enabled: bool, ccr_inject_marker: bool
) -> None:
    items = _NON_DICT_CASES[shape]
    recovered = _recover_from_output(
        items, ccr_enabled=ccr_enabled, ccr_inject_marker=ccr_inject_marker
    )
    distinct = {_repr(x) for x in items}
    lost = distinct - recovered
    assert not lost, (
        f"{shape}: {len(lost)} of {len(distinct)} distinct items unrecoverable "
        f"(enabled={ccr_enabled}, marker={ccr_inject_marker}); first: {list(lost)[:3]}"
    )


@pytest.mark.parametrize("ccr_enabled, ccr_inject_marker", _MARKER_OFF_MATRIX)
def test_dict_array_recovers_100pct_with_marker_off(
    ccr_enabled: bool, ccr_inject_marker: bool
) -> None:
    # Short distinct dict rows take the lossless:table path (CSV) — every
    # row is present verbatim in the output, recoverable without CCR.
    items = [{"id": i, "msg": f"record-{i}-distinct-payload"} for i in range(1000)]
    recovered = _recover_from_output(
        items, ccr_enabled=ccr_enabled, ccr_inject_marker=ccr_inject_marker
    )
    distinct = {_repr(x) for x in items}
    lost = distinct - recovered
    assert not lost, f"dict: {len(lost)} of {len(distinct)} rows unrecoverable; {list(lost)[:3]}"


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
    # base64 blobs > 256 bytes → CellClass::Opaque → substituted on the
    # lossless:table path. A short shared `tag` keeps the table tabular so
    # lossless wins and the markers reach the output.
    #
    # DETERMINISM (#26): the blobs are drawn from a FIXED seed, not
    # ``os.urandom``. With random blobs, ~2% of blob sets per config routed to
    # the lossy row-drop path instead of the lossless:table opaque-substitution
    # path, emitting NO ``<<ccr:HASH,KIND,SIZE>>`` markers — which made the
    # opaque-marker assertion below ~7.5% flaky across the 3-config matrix
    # (1-(1-0.02)^3). The data was ALWAYS fully recoverable (the row-drop path
    # has its own recovery markers, covered by the lossy-survivor tests); only
    # this opaque-specific fixture was flaky. Seed 0 is verified to route every
    # blob through the opaque-substitution path across all three matrix configs
    # AND the default config, and routing is run-to-run deterministic once the
    # blobs are fixed (no PYTHONHASHSEED sensitivity). os.urandom also violated
    # the determinism contract (rule 8).
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


_SUBJECT_PREFIXES = ["feat", "fix", "docs", "chore", "refactor", "test", "perf", "ci"]
_SUBJECT_AREAS = [
    "crusher", "proxy", "ccr", "router", "bench",
    "tokenizer", "store", "pipeline", "compaction", "relevance",
]
_SUBJECT_VERBS = [
    "add", "remove", "rework", "guard", "pin",
    "extend", "isolate", "deflake", "speed up", "harden",
]
_SUBJECT_THINGS = [
    "the lossy budget", "novelty fill", "sentinel emission", "marker parsing",
    "store mirroring", "field-role gates", "ditto marks", "schema folding",
    "query anchors", "drop accounting", "TTL handling", "thread-local state",
    "import guards", "error surfaces", "byte parity",
]


def _log_shaped_rows(n: int = 90) -> list[dict]:
    # High-entropy distinct rows (git-log shaped): hex identity columns,
    # low-cardinality author, genuinely varied unique subjects (uniformly
    # templated subjects trip the engine's `skip:unique_entities_no_signal`
    # crushability gate and never reach the lossy path). Forces the LOSSY
    # path, then the survivor-compaction rendering: a JSON string whose
    # final line is the `{"_ccr_dropped": ...}` sentinel.
    #
    # The dates carry MICROSECOND precision deliberately: strict-shape
    # second-precision ISO columns now delta-encode losslessly, which
    # pushed the previous fixture over the 0.30 lossless gate and off the
    # lossy path this test exists to pin. Fractional seconds are entirely
    # realistic for logs and are (honestly) refused by the strict
    # encoder, keeping this fixture lossy. The assertions are unchanged.
    return [
        {
            "commit": f"{i * 2654435761 + 12345:040x}",
            "author": f"Author {i % 7}",
            "date": (
                f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
                f"T{i % 24:02d}:{(i * 13) % 60:02d}:00.{(i * 104729) % 1000000:06d}+02:00"
            ),
            "subject": (
                f"{_SUBJECT_PREFIXES[i % 8]}({_SUBJECT_AREAS[i % 10]}): "
                f"{_SUBJECT_VERBS[i % 10]} {_SUBJECT_THINGS[i % 15]} #{i + 100}"
            ),
        }
        for i in range(n)
    ]


@pytest.mark.parametrize("ccr_enabled, ccr_inject_marker", _MARKER_OFF_MATRIX)
def test_lossy_survivor_table_recovers_100pct(
    ccr_enabled: bool, ccr_inject_marker: bool
) -> None:
    # The lossy-survivor CSV rendering (drop + sentinel LINE inside a JSON
    # string) must satisfy the same invariant as every other shape: every
    # distinct dropped row recoverable from the output alone.
    items = _log_shaped_rows()
    recovered = _recover_from_output(
        items, ccr_enabled=ccr_enabled, ccr_inject_marker=ccr_inject_marker
    )
    distinct = {_repr(x) for x in items}
    lost = distinct - recovered
    assert not lost, (
        f"lossy-survivor table: {len(lost)} of {len(distinct)} rows unrecoverable "
        f"(enabled={ccr_enabled}, marker={ccr_inject_marker}); first: {list(lost)[:3]}"
    )


def test_lossy_survivor_table_surfaces_sentinel_line() -> None:
    # Pin the shape itself: lossy drop + survivor compaction ships a JSON
    # string whose final line is the sentinel object carrying the pointer.
    items = _log_shaped_rows()
    router = ContentRouter()
    result = router.compress(json.dumps(items, ensure_ascii=False))
    tree = json.loads(result.compressed)
    assert isinstance(tree, str), "survivor compaction should ship a string rendering"
    last_line = tree.split("\n")[-1]
    sentinel = json.loads(last_line)
    assert isinstance(sentinel, dict) and "_ccr_dropped" in sentinel
    assert _DROP_RE.search(sentinel["_ccr_dropped"]), "sentinel carries the drop pointer"


def test_opaque_blob_default_config_recovers() -> None:
    # Default ContentRouter (markers on) — the production default. Same
    # invariant: every opaque blob recoverable from the output's marker.
    items = _opaque_rows()
    blobs = {it["data"] for it in items}
    router = ContentRouter()
    result = router.compress(json.dumps(items))
    hashes = set(_OPAQUE_RE.findall(result.compressed))
    assert hashes
    crusher = router._get_smart_crusher()
    recovered = {crusher.ccr_get(h) for h in hashes if crusher.ccr_get(h) is not None}
    assert blobs <= recovered
