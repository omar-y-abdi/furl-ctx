"""COR-14 harness contract: the independent recheck compares UN-FLATTENED.

``compactor.rs::flatten_uniform_nested`` promotes uniformly-nested objects
into dotted columns (``{"cfg": {"k": 1}}`` → column ``cfg.k``) and the wire
grammar records nothing, so ``decode_csv_schema_rows`` returns dotted
TOP-LEVEL keys. The documented contract is therefore value-exact under
dotted keys, not shape-exact. The recheck harness must compare both sides
under the same un-flattening canonicalization, or it counts every
uniformly-nested row as missing (the empirically-confirmed COR-14 failure).
"""

from __future__ import annotations

from furl_ctx.transforms.csv_schema_decoder import decode_csv_schema_rows
from verify.independent_recheck import _canonical, _unflatten_dotted


def test_unflatten_rebuilds_single_level_nesting() -> None:
    assert _unflatten_dotted({"cfg.k": 1, "id": 7}) == {"cfg": {"k": 1}, "id": 7}


def test_unflatten_rebuilds_multi_segment_keys() -> None:
    assert _unflatten_dotted({"a.b.c": "x"}) == {"a": {"b": {"c": "x"}}}


def test_unflatten_merges_sibling_dotted_keys() -> None:
    assert _unflatten_dotted({"m.region": "eu", "m.tier": 2}) == {"m": {"region": "eu", "tier": 2}}


def test_unflatten_is_recursive_so_both_sides_canonicalize_equal() -> None:
    """Literal dotted keys inside ORIGINAL nested values normalize too."""
    original = {"a": {"b.c": 1}}
    decoded_flat = {"a.b.c": 1}
    assert _canonical(_unflatten_dotted(original)) == _canonical(_unflatten_dotted(decoded_flat))


def test_unflatten_recurses_into_lists() -> None:
    assert _unflatten_dotted({"rows": [{"x.y": 1}]}) == {"rows": [{"x": {"y": 1}}]}


def test_unflatten_keeps_literal_key_on_conflict() -> None:
    """A dotted key that cannot nest without clobbering data stays literal."""
    row = {"a": 5, "a.b": 1}
    out = _unflatten_dotted(row)
    assert out["a"] == 5
    assert out["a.b"] == 1


def test_unflatten_keeps_degenerate_dot_keys_literal() -> None:
    assert _unflatten_dotted({".x": 1, "y.": 2, "a..b": 3}) == {".x": 1, "y.": 2, "a..b": 3}


def test_unflatten_is_total_on_non_dicts() -> None:
    assert _unflatten_dotted("plain") == "plain"
    assert _unflatten_dotted(3) == 3
    assert _unflatten_dotted(None) is None


def test_unflatten_never_mutates_its_input() -> None:
    row = {"cfg.k": 1, "nested": {"p.q": 2}}
    snapshot = {"cfg.k": 1, "nested": {"p.q": 2}}
    _unflatten_dotted(row)
    assert row == snapshot


def test_decoded_dotted_rows_match_nested_originals_under_unflatten() -> None:
    """End to end against the documented decoder grammar.

    A CSV-schema render whose columns came from the nested-uniform flatten
    (dotted names) must reconstruct — under the harness canonicalization —
    the SAME multiset as the nested originals.
    """
    originals = [
        {"cfg": {"k": 1, "mode": "fast"}, "id": 1},
        {"cfg": {"k": 2, "mode": "slow"}, "id": 2},
    ]
    wire = "[2]{cfg.k:int,cfg.mode:string,id:int}\n1,fast,1\n2,slow,2"
    decoded = decode_csv_schema_rows(wire)
    assert decoded is not None
    decoded_sigs = {_canonical(_unflatten_dotted(r)) for r in decoded}
    original_sigs = {_canonical(_unflatten_dotted(r)) for r in originals}
    assert decoded_sigs == original_sigs
