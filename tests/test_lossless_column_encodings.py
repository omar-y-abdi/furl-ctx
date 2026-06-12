"""Lossless per-column encodings on the public ``compress()`` path.

Covers the CSV-schema formatter's column encodings:

* **Constant-column fold** — a column holding the identical scalar in
  every row declares ``name:type=value`` once in the ``[N]{...}`` header
  and is omitted from the rows. The value is verbatim in the output and
  every row is reconstructible from the output alone (zero loss).

The reconstruction contract is the SAME decoder the CCR recovery
invariant uses (``tests/test_ccr_recovery_invariant.py``): a consumer
holding ONLY the output must be able to recover every distinct row.
"""

from __future__ import annotations

import json

from headroom.transforms.content_router import ContentRouter

from tests.test_ccr_recovery_invariant import _decode_csv_schema, _repr


def _compress_to_text(items: list) -> str:
    result = ContentRouter().compress(json.dumps(items, ensure_ascii=False))
    rendered = result.compressed
    try:
        parsed = json.loads(rendered)
    except (json.JSONDecodeError, ValueError):
        return rendered
    assert isinstance(parsed, str), f"expected lossless rendering, got: {type(parsed)}"
    return parsed


def _reconstruct(text: str) -> set[str]:
    recovered: set[str] = set()
    _decode_csv_schema(text, recovered)
    return recovered


def test_constant_columns_fold_and_round_trip() -> None:
    # Shape mirrors the real `ping` benchmark capture: three constant
    # columns + a monotone counter + a varying float.
    items = [
        {
            "bytes": 64,
            "from": "127.0.0.1",
            "icmp_seq": i,
            "ttl": 64,
            "time_ms": round(0.031 + (i % 7) * 0.013, 3),
        }
        for i in range(60)
    ]
    text = _compress_to_text(items)
    decl = text.split("\n", 1)[0]

    # Constants are folded into the declaration, verbatim, exactly once.
    assert "bytes:int=64" in decl, decl
    assert "from:string=127.0.0.1" in decl, decl
    assert "ttl:int=64" in decl, decl
    assert text.count("127.0.0.1") == 1, "constant must appear exactly once"

    # Zero loss: every distinct row reconstructible from the output alone.
    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_constant_fold_does_not_fire_on_varying_columns() -> None:
    items = [{"id": i, "msg": f"record-{i}-distinct-payload"} for i in range(50)]
    text = _compress_to_text(items)
    decl = text.split("\n", 1)[0]
    assert "=" not in decl, f"no constant exists, nothing may fold: {decl}"
    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing
