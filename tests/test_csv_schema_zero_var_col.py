"""Regression test for #25: const+arith zero-var-column table => [] total loss.

A declaration where EVERY column is a constant or arithmetic fold has no
per-row body cells. The decoder special-cased only the all-const variant
(``const_cols and not arith_cols``) and returned ``[]`` for any table that
also had an arithmetic column, e.g. ``[3]{x:int=5,seq:int=0+1}`` — total
silent loss of all N rows.

Fix: the degenerate-fully-folded branch now emits ``declared_count`` rows
from const values + ``base+step*ordinal`` arith values whenever there are
zero var columns and at least one const/arith column.

★ DEFENSIVE / forward-looking: the reference Rust formatter always reserves
one variable "anchor" column unless every column is const (verified
empirically — sequential-int + const inputs render the int as the var column,
not as an arith fold). So the reference encoder does NOT currently emit the
const+arith zero-var shape; these tests use hand-crafted decoder input. The
fix makes the recovery decoder correct for any conformant producer of the
shape (alt-producer / future-encoder safety), consistent with the
``__affix:`` defensive-contract precedent.
"""

from __future__ import annotations

import pytest

from headroom.transforms.csv_schema_decoder import decode_csv_schema_rows


def test_const_plus_arith_zero_var_emits_rows() -> None:
    # #25 repro: x is const, seq is an arith fold, no var column. No body lines.
    rows = decode_csv_schema_rows("[3]{x:int=5,seq:int=0+1}")
    assert rows == [
        {"x": 5, "seq": 0},
        {"x": 5, "seq": 1},
        {"x": 5, "seq": 2},
    ]


def test_single_arith_only_zero_var_emits_rows() -> None:
    # Pure arith fold, no body, no var, no const.
    rows = decode_csv_schema_rows("[2]{seq:int=10+5}")
    assert rows == [{"seq": 10}, {"seq": 15}]


def test_all_const_zero_var_still_works() -> None:
    # Contrast: the all-const path (the only zero-var shape the reference
    # encoder emits) must keep working — guards against regressing it while
    # generalizing the branch.
    rows = decode_csv_schema_rows("[3]{x:int=5,y:string=A}")
    assert rows == [{"x": 5, "y": "A"}] * 3


@pytest.mark.parametrize(
    "text,expected",
    [
        # Two arith folds, zero var.
        ("[2]{a:int=0+1,b:int=100+10}", [{"a": 0, "b": 100}, {"a": 1, "b": 110}]),
        # Const + two arith.
        (
            "[2]{k:string=K,i:int=0+2,j:int=5+1}",
            [{"k": "K", "i": 0, "j": 5}, {"k": "K", "i": 2, "j": 6}],
        ),
    ],
)
def test_zero_var_fold_variants(text: str, expected: list[dict]) -> None:
    assert decode_csv_schema_rows(text) == expected
