"""Lossless per-column encodings on the public ``compress()`` path.

Covers the CSV-schema formatter's column encodings:

* **Constant-column fold** — a column holding the identical scalar in
  every row declares ``name:type=value`` once in the ``[N]{...}`` header
  and is omitted from the rows. The value is verbatim in the output and
  every row is reconstructible from the output alone (zero loss).

* **Ditto marks** — a cell identical to the SAME column's cell in the
  previous row renders as a bare ``=``; a literal string cell ``"="`` is
  CSV-quoted so the marker stays unambiguous. Reconstruction is
  carry-forward of the last materialized value (zero loss).

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


def test_ditto_marks_round_trip_consecutive_repeats() -> None:
    # Shape mirrors the real rg-search benchmark capture: `path` repeats
    # in consecutive runs (matches grouped per file), other columns vary.
    items = [
        {
            "path": f"src/module_{i // 10}.py",
            "line_number": 3 * i + 1,
            "lines": f"def handler_{i}(request):",
        }
        for i in range(60)
    ]
    text = _compress_to_text(items)
    body = text.split("\n")[1:]

    # Runs of the same path render as ditto cells after the first row.
    assert any(
        line.startswith("=,") or ",=," in line or line.endswith(",=") for line in body
    ), f"expected ditto marks in rows; first rows: {body[:3]}"
    # Each distinct path appears exactly once (first row of its run).
    for d in range(6):
        assert text.count(f"src/module_{d}.py") == 1

    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_literal_equals_sign_cell_is_not_mistaken_for_ditto() -> None:
    # A real data cell whose value is exactly "=" must render CSV-quoted
    # so bare `=` stays unambiguous, and must round-trip as the literal.
    items = [
        {"id": i, "op": "=" if i % 2 == 0 else f"op-{i}", "v": f"val-{i}"}
        for i in range(40)
    ]
    text = _compress_to_text(items)
    assert '"="' in text, f"literal '=' cell must be quoted: {text.split(chr(10))[:4]}"

    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_repeated_numeric_cells_ditto_and_round_trip() -> None:
    items = [
        {"seq": i, "status_code": 200 if i % 5 else 503, "latency_ms": 12.5 + (i % 3)}
        for i in range(40)
    ]
    text = _compress_to_text(items)
    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"
