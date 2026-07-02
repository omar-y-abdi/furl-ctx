"""COR-24 regression pins: constant bounded columns are not score fields.

The score-field heuristic lives in Rust
(``crates/furl-core/src/transforms/smart_crusher/field_detect.rs``);
the retired pure-Python twin is gone and ``furl_ctx.transforms.smart_crusher``
delegates to ``furl_ctx._core``. These tests pin the cross-boundary behavior
through the public bridge — playing the parity-test role for this heuristic
now that the recorded parity fixtures (``tests/parity/fixtures/smart_crusher/``)
were removed along with the Python source.

Defect being pinned
-------------------
Ties counted as "descending" (``w[0] >= w[1]`` over adjacent pairs), so a
CONSTANT bounded column (``progress: 50`` ×30, 0-100 bucket) gained the +0.3
descending bonus on top of the +0.25 bounded-range bonus → 0.55 ≥ 0.4 →
classified as a score field → the array matched the ``search_results``
pattern → TopN "sorted" on the constant — silently degrading to positional
keep-first-K. A fractional [0,1] constant (``progress: 0.5``) crossed the
threshold even without the descending bonus (0.4 bounded + 0.1 float).

The fix gates ALL score-confidence bonuses (bounded-range, descending,
float-fraction) on variation: ``unique_count > 1`` AND ``variance > 0``.
A rank signal requires variation.

Bite evidence
-------------
Both constant tests were confirmed RED against the pre-fix extension:
``crush_array_json`` returned ``strategy_info == "top_n"`` for the constant
fixtures (4 of 30 rows kept, first-K positional). The descending control
returned ``"top_n"`` before AND after the fix.
"""

from __future__ import annotations

import json

from furl_ctx.transforms.smart_crusher import SmartCrusher, SmartCrusherConfig

# 30 rows: clears min_items_to_analyze (5) and forces real row selection
# against max_items_after_crush (15). ``msg`` varies so the fixture has
# content beyond the probed column; the probed column is the only numeric.
_N_ITEMS = 30


def _crusher() -> SmartCrusher:
    # min_tokens_to_crush=1: the fixtures are deliberately small; the token
    # gate is not what these tests probe. with_compaction=False: keep
    # strategy_info on the pure lossy path (no ``lossless:``/``+compact:``
    # variants) so the strategy assertion stays crisp.
    return SmartCrusher(
        config=SmartCrusherConfig(min_tokens_to_crush=1),
        with_compaction=False,
    )


def _constant_int_items() -> str:
    return json.dumps(
        [{"progress": 50, "msg": f"step {i} of the long batch run"} for i in range(_N_ITEMS)]
    )


def _constant_float_items() -> str:
    return json.dumps(
        [{"progress": 0.5, "msg": f"step {i} of the long batch run"} for i in range(_N_ITEMS)]
    )


def _descending_score_items() -> str:
    # Genuinely ranked data: distinct [0,1] floats, descending in array
    # order, non-sequential steps (0.03 — outside the [0.5, 2.0]
    # sequential-diff band the ID detector looks for).
    return json.dumps(
        [
            {"score": round(0.95 - 0.03 * i, 4), "msg": f"result {i} for the query"}
            for i in range(_N_ITEMS)
        ]
    )


def test_constant_int_column_is_not_a_score_field() -> None:
    # RED pre-COR-24: strategy_info was "top_n" — TopN sorted a constant
    # and kept the first K rows positionally.
    result = _crusher().crush_array_json(_constant_int_items(), query="", bias=1.0)
    assert "top_n" not in result["strategy_info"], (
        f"constant progress column classified as a score field: {result['strategy_info']!r}"
    )


def test_constant_fractional_column_is_not_a_score_field() -> None:
    # RED pre-COR-24 even without the descending bonus: 0.4 (bounded
    # [0,1]) + 0.1 (float fraction) crossed the 0.4 threshold on its own.
    result = _crusher().crush_array_json(_constant_float_items(), query="", bias=1.0)
    assert "top_n" not in result["strategy_info"], (
        f"constant fractional column classified as a score field: {result['strategy_info']!r}"
    )


def test_descending_scores_still_get_top_n() -> None:
    # Control: the variation gate must not suppress REAL ranked data.
    result = _crusher().crush_array_json(_descending_score_items(), query="", bias=1.0)
    assert "top_n" in result["strategy_info"], (
        f"genuinely descending score column lost TopN treatment: {result['strategy_info']!r}"
    )
