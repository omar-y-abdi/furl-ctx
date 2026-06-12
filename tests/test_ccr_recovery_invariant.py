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
import os
import re

import pytest

from headroom.cache.compression_store import get_compression_store
from headroom.transforms.content_router import ContentRouter, ContentRouterConfig

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
    JSON objects. Those rows are present verbatim in the output — lossless
    — so they count as recovered-from-output-alone."""
    if not text.startswith("["):
        return
    lines = text.split("\n")
    header = re.match(r"\[\d+\]\{(.+)\}$", lines[0])
    if not header:
        return
    cols = [c.split(":")[0] for c in header.group(1).split(",")]
    for line in lines[1:]:
        if not line:
            continue
        parts = line.split(",", len(cols) - 1)
        if len(parts) != len(cols):
            continue
        row = {}
        for col, raw in zip(cols, parts):
            try:
                row[col] = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                row[col] = raw
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
    return [
        {"id": i, "tag": "x", "data": base64.b64encode(os.urandom(600)).decode()}
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
