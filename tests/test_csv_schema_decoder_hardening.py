"""Mutation-resistance hardening for csv_schema_decoder.decode_csv_schema_rows.

All tests use the PUBLIC path (decode_csv_schema_rows) — A2-friendly.
Every test is a pinned literal or boundary case: passing the test today AND
failing when the pinned behavior is deliberately mutated.

Hardening targets:
  CD-B1a: pin exact literal decode for the #24 repro case (empty var row + arith ordinal).
  CD-B1b: pin exact literal decode for boundary between var-only/const-only/arith-present
          column classes (guards the #25 fix and the degenerate-branch boundary).
  CD-B1c: pin the malformed __affix: current behavior (needs-review / alt-producer only).
  CD-bnd:  boundaries around single-var-col, multi-var-col, and ordinal advancement.
"""

from __future__ import annotations

import pytest

from furl_ctx.transforms.csv_schema_decoder import decode_csv_schema_rows

# ---------------------------------------------------------------------------
# CD-B1a  #24 repro — exact literal output for single-var + arith + empty row
# ---------------------------------------------------------------------------


def test_b1a_bug24_exact_literal_with_empty_middle_row() -> None:
    """Pin literal: single-var (msg), arith (seq), middle row is empty string.

    Row 1: 'hello' seq=0
    Row 2: ''      seq=1  (blank physical line = empty-string value)
    Row 3: 'world' seq=2  (must NOT shift to seq=1 after the empty row)

    Mutation-sensitive: removing the 'emit-blank-as-empty-row' branch → len==2;
    reverting the ordinal-advance on blank → seq of 'world' becomes 1 not 2.
    """
    result = decode_csv_schema_rows("[3]{seq:int=0+1,msg:string}\nhello\n\nworld")
    assert result == [
        {"msg": "hello", "seq": 0},
        {"msg": "", "seq": 1},
        {"msg": "world", "seq": 2},
    ]


def test_phantom_row_not_fabricated_after_skipped_row() -> None:
    """A trailing blank line must NOT become a phantom row when an earlier
    row was skipped-but-counted.

    Single var col ``b`` + arith col ``a``, declared count 2. The first body
    line ``=`` is a leading carry with no prior value → bad cell, skipped but
    counts toward ``ordinal``. ``z`` is the one real row; the trailing newline
    is an artifact, not a third value.

    Before the fix the blank-line guard bounded on ``len(rows) < declared_count``
    (kept-count, which lagged the skipped row), so the artifact decoded as a
    phantom row with a fabricated arith value ``a = 0 + 5*2 = 10``. The guard
    now bounds on the CONSUMED ``ordinal``, so no data is invented.

    Mutation-sensitive: reverting the guard to ``len(rows)`` → a second
    ``{"a": 10, "b": ""}`` row reappears.
    """
    result = decode_csv_schema_rows("[2]{a:int=0+5,b:string}\n=\nz\n")
    assert result == [{"b": "z", "a": 5}]


@pytest.mark.parametrize(
    "text,expected",
    [
        # Two rows, no empty — normal path, also pins ordinal
        (
            "[2]{seq:int=0+1,msg:string}\nhello\nworld",
            [{"msg": "hello", "seq": 0}, {"msg": "world", "seq": 1}],
        ),
        # Single row single var col
        ("[1]{col:string}\nhello", [{"col": "hello"}]),
        # Two var cols, no arith (CSV uses comma separator between columns)
        (
            "[2]{a:string,b:string}\nfoo,bar\nbaz,qux",
            [{"a": "foo", "b": "bar"}, {"a": "baz", "b": "qux"}],
        ),
    ],
)
def test_b1a_parametrize_var_col_shapes(text: str, expected: list[dict]) -> None:
    """Parametrized boundary: var-col shapes with no empty rows."""
    assert decode_csv_schema_rows(text) == expected


# ---------------------------------------------------------------------------
# CD-B1b  column-class boundary: var-only / const-only / arith-present (#25)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        # Pure const (no var, no arith) — both cols const
        ("[2]{x:int=5,y:int=10}", [{"x": 5, "y": 10}, {"x": 5, "y": 10}]),
        # Pure arith (zero var, zero const) — just seq
        ("[2]{seq:int=10+5}", [{"seq": 10}, {"seq": 15}]),
        # Const + arith (zero var) — the #25 regression case
        (
            "[3]{x:int=5,seq:int=0+1}",
            [{"x": 5, "seq": 0}, {"x": 5, "seq": 1}, {"x": 5, "seq": 2}],
        ),
        # Mixed: one var col + one const col
        (
            "[2]{label:string,cat:string=A}\nalpha\nbeta",
            [{"label": "alpha", "cat": "A"}, {"label": "beta", "cat": "A"}],
        ),
        # Mixed: one var col + one arith col
        (
            "[2]{msg:string,seq:int=0+1}\nhello\nworld",
            [{"msg": "hello", "seq": 0}, {"msg": "world", "seq": 1}],
        ),
    ],
)
def test_b1b_column_class_boundary(text: str, expected: list[dict]) -> None:
    """Boundary between var-only / const-only / arith-present column classes.

    Mutation-sensitive: removing the zero-var-col branch → const+arith returns [];
    removing the const-only branch → all-const returns [].
    """
    assert decode_csv_schema_rows(text) == expected


# ---------------------------------------------------------------------------
# CD-B1c  malformed __affix: preamble — needs-review (alt-producer only)
# ---------------------------------------------------------------------------


def test_b1c_malformed_affix_preamble_treated_as_data_row() -> None:
    """Pin the current behavior for a malformed __affix: line (< 2 segments).

    The reference Rust formatter never emits a malformed affix preamble, so
    this shape is only reachable from an alt-producer. The decoder currently
    treats the malformed preamble line as a regular data row. This test pins
    that behavior as a contract assertion; a future fix would change the
    expected value.

    Mutation-sensitive: altering the affix-line detection boundary would
    cause a valid __affix: to be misclassified (the converse case).
    """
    # __affix:col= has only 2 parts after split('=', 1) → treated as data row
    result = decode_csv_schema_rows("[1]{col:string^}\n__affix:col=prefix_only\nactual_row")
    # Current behavior: both lines are treated as data rows
    assert result == [{"col": "__affix:col=prefix_only"}, {"col": "actual_row"}]


# ---------------------------------------------------------------------------
# CD-bnd  ordinal advancement boundary
# ---------------------------------------------------------------------------


def test_ordinal_increments_across_empty_rows() -> None:
    """Empty rows advance the arith ordinal — boundary: count==3 for 3 declared rows."""
    # Three rows: first non-empty, second empty, third non-empty
    result = decode_csv_schema_rows("[3]{seq:int=0+1,msg:string}\nfirst\n\nthird")
    assert result is not None
    assert len(result) == 3  # boundary: 3, not 2 (the #24 fix)
    seqs = [r["seq"] for r in result]
    assert seqs == [0, 1, 2], f"ordinal did not advance on empty row: {seqs}"


def test_declared_count_matches_output_count() -> None:
    """Decoded row count == declared count in header — boundary at exact N."""
    for n in [1, 2, 5, 10]:
        lines = "\n".join(f"row{i}" for i in range(n))
        text = f"[{n}]{{msg:string}}\n{lines}"
        result = decode_csv_schema_rows(text)
        assert result is not None
        assert len(result) == n, f"expected {n} rows, got {len(result)}"


def test_returns_none_for_non_csv_schema_input() -> None:
    """Returns None for plain JSON, prose, and empty string — not a CSV-schema text."""
    for bad_input in ['{"key": "value"}', "hello world", "", "[1,2,3]"]:
        assert decode_csv_schema_rows(bad_input) is None, (
            f"should return None for non-csv-schema input: {bad_input!r}"
        )
