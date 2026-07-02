"""Regression test for #24: empty sole-var-column row dropped + arith misalign.

The Rust formatter renders a single-variable-column table whose other columns
are arithmetic/const folds, e.g. ``[N]{msg:string,seq:int=0+1}``. A row with an
empty ``msg`` renders as a blank physical line. The old decoder did
``if not line: continue`` — which (a) dropped that empty-string row entirely
and (b) failed to advance the arith ``ordinal``, so EVERY later row's ``seq``
was shifted by one. Silent, byte-inexact loss on the recovery path.

Fix: a blank line is a real empty-string value when there is exactly one var
column; emit it (incrementing ordinal), bounded by the declared row count so
the trailing-newline artifact never becomes a phantom row.

This is a FULL-PIPELINE parity test: real Rust-encode (ContentRouter.compress)
→ Python decode_csv_schema_rows → byte-exact deep equality.
"""

from __future__ import annotations

import json

from headroom.transforms.content_router import ContentRouter, ContentRouterConfig
from headroom.transforms.csv_schema_decoder import decode_csv_schema_rows

_LOSSLESS = ContentRouterConfig(smart_crusher_routing_policy="lossless-first")


def _compress_to_csv_text(items: list[dict]) -> str:
    text = ContentRouter(_LOSSLESS).compress(json.dumps(items, ensure_ascii=False)).compressed
    try:
        parsed = json.loads(text)
        if isinstance(parsed, str):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return text


def test_empty_sole_var_col_row_round_trips_byte_exact() -> None:
    # seq becomes an arith fold (0+1); msg is the sole var column with an
    # empty value at index 1. Pad to 40 rows so the lossless CSV path engages.
    items = [{"seq": 0, "msg": "hello"}, {"seq": 1, "msg": ""}, {"seq": 2, "msg": "world"}] + [
        {"seq": i, "msg": f"m{i}"} for i in range(3, 40)
    ]
    text = _compress_to_csv_text(items)
    # Confirm we hit the single-var-col + arith-fold shape this bug needs.
    assert text.startswith("[40]{msg:string,seq:int=0+1}"), f"unexpected encoding: {text[:60]!r}"

    rows = decode_csv_schema_rows(text)
    assert rows is not None
    # Byte-exact: count preserved AND the empty row present AND seq not shifted.
    assert len(rows) == 40, f"expected 40 rows, got {len(rows)} (empty row dropped?)"
    assert {"seq": 0, "msg": "hello"} in rows
    assert {"seq": 1, "msg": ""} in rows, "the empty-msg row must survive (#24)"
    assert {"seq": 2, "msg": "world"} in rows, "world must keep seq=2, not shift to 1"
    # Full deep equality (order-independent compare against input).
    assert sorted(rows, key=lambda r: r["seq"]) == sorted(items, key=lambda r: r["seq"])


def test_trailing_newline_does_not_invent_phantom_empty_row() -> None:
    # An all-present single-var table (no internal empties) must NOT gain a
    # phantom empty row from the trailing '\n' that ends every rendered table.
    items = [{"seq": i, "msg": f"line{i}"} for i in range(40)]
    text = _compress_to_csv_text(items)
    rows = decode_csv_schema_rows(text)
    assert rows is not None
    assert len(rows) == 40, f"trailing newline created a phantom row: {len(rows)}"
    assert all(r["msg"] for r in rows), "no row should have an empty msg here"
