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

from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig


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


@pytest.mark.parametrize(
    "marginal_ratio,interior_pressure",
    [
        # 0.90 sits EXACTLY at the band midpoint min_ratio(0.5)=0.90; on its own a
        # boundary fixture can let an operator-flip / interpolation mutation survive.
        (0.90, 0.4),
        # 0.92 is an OFF-boundary interior point: rejected at mid pressure (0.5 ->
        # 0.90) but accepted at full (0.95). Pairing it with 0.90 pins both the
        # boundary AND a strict-interior point, so a collapse-to-endpoint or
        # inverted-interpolation mutation can no longer pass.
        (0.92, 0.5),
    ],
)
def test_marginal_compression_accepted_only_at_high_pressure(
    marginal_ratio: float, interior_pressure: float
) -> None:
    # A marginal compression sits BETWEEN the two thresholds [0.85, 0.95):
    # accepted at high pressure (ratio < 0.95) but rejected at low pressure
    # (ratio >= 0.85). The accept rule is `ratio < min_ratio`. This is the
    # behavior the inversion got backwards — pre-fix, high pressure used the
    # LOWER threshold (0.65) and would have rejected this marginal save.
    r = _router()
    assert marginal_ratio < r._adaptive_min_ratio(1.0), "marginal must be accepted when full"
    assert marginal_ratio >= r._adaptive_min_ratio(0.0), "marginal must be rejected when empty"
    # Strict-interior pin: at a sub-peak pressure the threshold is still below the
    # ratio, so the save is rejected. A mutation that flattens the ramp to the
    # aggressive endpoint (0.95 everywhere) would wrongly ACCEPT here and fail this.
    assert marginal_ratio >= r._adaptive_min_ratio(interior_pressure), (
        "marginal must be rejected at sub-peak pressure (threshold below ratio)"
    )


def test_clamp_bounds() -> None:
    # Out-of-range pressures clamp to [relaxed, aggressive].
    r = _router()
    assert r._adaptive_min_ratio(-1.0) == pytest.approx(0.85)
    assert r._adaptive_min_ratio(2.0) == pytest.approx(0.95)
