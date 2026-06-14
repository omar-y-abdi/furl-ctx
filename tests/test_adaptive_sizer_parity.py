"""Tests for adaptive_sizer n<=8 fast path max_k fix (U9) and Python/Rust parity.

Mirrors the Rust inline tests in adaptive_sizer.rs and verifies that
compute_optimal_k returns identical values in both Python and Rust for a
shared fixture set (Contract #3 parity).
"""

from __future__ import annotations

import pytest

from headroom.transforms.adaptive_sizer import compute_optimal_k


# ---------------------------------------------------------------------------
# Fast-path (n<=8) + max_k correctness tests (mirror of Rust tests)
# ---------------------------------------------------------------------------


def test_n_le_8_max_k_none_returns_n() -> None:
    """Fast path, no cap: max_k=None must return n."""
    items = ["a", "b", "c", "d", "e"]
    assert compute_optimal_k(items, 1.0, 1, None) == 5


def test_n_le_8_max_k_greater_than_n_returns_n() -> None:
    """max_k > n: cap does not bite, still return n."""
    items = ["a", "b", "c", "d", "e"]
    assert compute_optimal_k(items, 1.0, 1, 10) == 5


def test_n_le_8_respects_max_k_when_less_than_n() -> None:
    """Fast path MUST apply max_k cap: n=5, max_k=2 → must return 2, not 5.

    This is the core regression test for U9: before the fix both Python and
    Rust returned n=5 outright, ignoring effective_max=2.
    """
    items = ["a", "b", "c", "d", "e"]
    result = compute_optimal_k(items, 1.0, 1, 2)
    assert result == 2, (
        f"n<=8 fast path must cap at max_k=2, got {result} — "
        "this is the U9 regression"
    )


def test_n_le_8_max_k_1_returns_1() -> None:
    """Edge: max_k=1 with n=5 must return 1."""
    items = ["a", "b", "c", "d", "e"]
    assert compute_optimal_k(items, 1.0, 1, 1) == 1


def test_n_le_8_max_k_equals_n_returns_n() -> None:
    """max_k == n: cap does not reduce, return n."""
    items = ["a", "b", "c", "d", "e"]
    assert compute_optimal_k(items, 1.0, 1, 5) == 5


# ---------------------------------------------------------------------------
# Parity fixture tests: Python output must match Rust output for the same
# (items, bias, min_k, max_k) inputs.
#
# Rust golden values are the values the FIXED Rust code now produces.
# We record them explicitly so any future divergence is immediately visible.
# ---------------------------------------------------------------------------


# fmt: off
PARITY_FIXTURES: list[tuple[list[str], float, int, int | None, int]] = [
    # (items, bias, min_k, max_k, expected_k)
    # n<=8 cases — fast path
    (["a", "b", "c", "d", "e"], 1.0, 1, None, 5),   # max_k=None → n
    (["a", "b", "c", "d", "e"], 1.0, 1, 10,  5),    # max_k>n → n
    (["a", "b", "c", "d", "e"], 1.0, 1, 2,   2),    # max_k<n → cap at 2 (U9 fix)
    (["a", "b", "c", "d", "e"], 1.0, 1, 5,   5),    # max_k==n → n
    (["x"],                      1.0, 1, None, 1),   # n=1
    (["x", "y"],                 1.0, 1, 1,   1),    # n=2, max_k=1
    (["x", "y", "z"],            1.0, 1, None, 3),   # n=3, no cap
    # n>8 cases — standard path with cap
    (
        [f"item number {i} with enough content to be distinct" for i in range(20)],
        1.0, 3, 10, None,  # None means: just assert result <= 10
    ),
]
# fmt: on


@pytest.mark.parametrize(
    "items,bias,min_k,max_k,expected_k",
    [f for f in PARITY_FIXTURES if f[4] is not None],
    ids=[
        "fast_no_cap",
        "fast_cap_gt_n",
        "fast_cap_lt_n_U9",
        "fast_cap_eq_n",
        "n1_no_cap",
        "n2_max1",
        "n3_no_cap",
    ],
)
def test_python_matches_rust_fixture(
    items: list[str],
    bias: float,
    min_k: int,
    max_k: int | None,
    expected_k: int,
) -> None:
    """Python compute_optimal_k must return the same k as the Rust golden value."""
    result = compute_optimal_k(items, bias, min_k, max_k)
    assert result == expected_k, (
        f"Python returned {result}, expected {expected_k} "
        f"(items[:3]={items[:3]!r}, bias={bias}, min_k={min_k}, max_k={max_k})"
    )


def test_n_gt_8_max_k_respected() -> None:
    """n>8 case: max_k cap is respected (already worked, regression guard)."""
    items = [f"item {i} with content that is sufficiently long to matter" for i in range(20)]
    result = compute_optimal_k(items, 1.0, 3, 10)
    assert result <= 10, f"expected result <= 10, got {result}"
    assert result >= 3, f"expected result >= 3, got {result}"
