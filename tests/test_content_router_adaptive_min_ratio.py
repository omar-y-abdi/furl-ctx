"""Regression test for #12: adaptive min_ratio was inverted.

A compression is ACCEPTED when ``ratio < min_ratio`` (the router rejects
ratios >= min_ratio as "ratio_too_high"). A HIGHER min_ratio therefore accepts
MORE compressions, including marginal ones.

Bug: min_ratio_aggressive (0.65) was BELOW min_ratio_relaxed (0.85), and the
interpolation drove min_ratio DOWN as context pressure rose — so the router
rejected marginal compressions exactly when context was nearly full (when the
agent most needs them). The documented intent (and the field comments) was the
opposite: "at high pressure, accept anything helpful."

Fix: aggressive is now the HIGHER (more-permissive) threshold (0.95), relaxed
stays 0.85, so min_ratio RISES with pressure. Low-pressure behavior is
unchanged (0.85) — no compression tradeoff — and high-pressure accepts more.

These assert the FIXED behavior and are mutation-sensitive: reverting the field
ordering, the interpolation, or the clamp breaks the monotonicity / endpoint /
acceptance assertions.
"""
from __future__ import annotations

import pytest

from headroom.transforms.content_router import ContentRouter, ContentRouterConfig


def _router() -> ContentRouter:
    return ContentRouter(ContentRouterConfig())


@pytest.mark.parametrize(
    "pressure,expected",
    [
        (0.0, 0.85),  # relaxed endpoint — MUST stay at the pre-fix low-pressure value
        (0.5, 0.90),  # midpoint of [0.85, 0.95]
        (1.0, 0.95),  # aggressive endpoint — permissive when context is full
    ],
)
def test_adaptive_min_ratio_endpoints(pressure: float, expected: float) -> None:
    assert _router()._adaptive_min_ratio(pressure) == pytest.approx(expected)


def test_adaptive_min_ratio_is_monotone_increasing() -> None:
    r = _router()
    vals = [r._adaptive_min_ratio(p / 10) for p in range(11)]
    assert vals == sorted(vals), f"min_ratio must rise with pressure, got {vals}"
    assert vals[0] < vals[-1], "high pressure must be strictly more permissive than low"


def test_low_pressure_no_degradation() -> None:
    # The no-tradeoff guard: an empty-context request must keep the SAME
    # acceptance threshold as before the fix (0.85), so nothing that compressed
    # at low pressure stops compressing.
    assert _router()._adaptive_min_ratio(0.0) == pytest.approx(0.85)


def test_marginal_compression_accepted_only_at_high_pressure() -> None:
    # A marginal compression (ratio 0.90) sits BETWEEN the two thresholds
    # [0.85, 0.95): accepted at high pressure (0.90 < 0.95) but rejected at low
    # pressure (0.90 >= 0.85). The accept rule is `ratio < min_ratio`. This is
    # the behavior the inversion got backwards — pre-fix, high pressure used
    # the LOWER threshold (0.65) and would have rejected this marginal save.
    r = _router()
    marginal_ratio = 0.90
    assert marginal_ratio < r._adaptive_min_ratio(1.0), "marginal must be accepted when full"
    assert marginal_ratio >= r._adaptive_min_ratio(0.0), "marginal must be rejected when empty"


def test_clamp_bounds() -> None:
    # Out-of-range pressures clamp to [relaxed, aggressive].
    r = _router()
    assert r._adaptive_min_ratio(-1.0) == pytest.approx(0.85)
    assert r._adaptive_min_ratio(2.0) == pytest.approx(0.95)
