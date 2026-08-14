"""The PR shapes the floor guard has to tell apart.

Each test constructs a real PR shape rather than asserting on a hand-made
report, because the whole point of the guard is what it does to a PR.

READ THE FIRST THREE TESTS FIRST. They are the regression tests for the DESIGN,
not for the code. Everything below them checks that the guard behaves; those
three check that it still closes the holes which forced the guard into its final
shape. All three are named PRIMARY.

The first: the original proposal was DRIFT alone, with a failure message telling
authors to regenerate the floor in the same PR — and regenerating is WHOLESALE,
so a PR that improves one dataset while regressing another passes cleanly once
the floor is regenerated, the gate then comparing the candidate against a floor
derived from that same candidate. DRIFT alone would have printed the exploit as
its own remedy.

The second: ``compare_baseline`` floors THREE quantities — ``tokens_after`` and
``information_retention`` per dataset, and recall per regime — and early versions
of this guard read only the first, then only the first two. A guard that names
the loosening class and defends part of it is the same defect it was built to
catch. Both gaps were MEASURED blind before being fixed, never assumed.

The third: every loop in ``evaluate`` is a no-op over an empty mapping, so the
guard once reported a verified floor after reading nothing. Green that does not
separate "checked and held" from "nothing to check" is not a guard — assert the
POSITIVE count.

If any of the three goes green by being deleted or weakened, the guard has been
returned to a state that made it necessary.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path

import pytest

from benchmarks.compare_baseline import DatasetMetrics, RegimeRow
from benchmarks.floor_guard import (
    BASELINE_ARTIFACTS,
    FloorGuardInputError,
    FloorSnapshot,
    GuardReport,
    evaluate,
    is_baseline_only,
    load_snapshot,
    main,
)

_MAIN_TOKENS = {"logs@90": 603, "search@90": 336, "code@7": 1678}
_MAIN_REGIMES = {
    "naming/overall/output_or_ccr": 1.0,
    "naming/overall/visible_only": 1.0,
    "naming/logs/output_or_ccr": 1.0,
    "naming/logs/visible_only": 1.0,
}
_FEATURE_DIFF = ["furl_ctx/transforms/log_compressor.py", "tests/test_log_compressor.py"]
_REFLOOR_DIFF = sorted(BASELINE_ARTIFACTS)


def _snap(
    tokens: Mapping[str, int] | None = None,
    regimes: Mapping[str, float] | None = None,
    retention: Mapping[str, float] | None = None,
) -> FloorSnapshot:
    """A snapshot defaulting to the healthy floor, so each test varies one thing.

    ``retention`` defaults to a perfect 1.0 for whatever datasets are present,
    which is what the real artifact carries today.
    """
    token_map = dict(_MAIN_TOKENS if tokens is None else tokens)
    return FloorSnapshot(
        tokens=token_map,
        retention=dict.fromkeys(token_map, 1.0) if retention is None else dict(retention),
        regimes=dict(_MAIN_REGIMES if regimes is None else regimes),
    )


_MAIN = _snap()


def _checks(report: GuardReport) -> set[tuple[str, str]]:
    return {(f.check, f.scope) for f in report.findings}


def _gated_quantities() -> set[str]:
    """Every quantity ``compare_baseline`` floors, READ from its types.

    Not retyped. The whole point is that adding a fourth floored quantity to the
    gate breaks this derivation, and therefore breaks the completeness test that
    depends on it, on the PR that adds it — rather than leaving a hand-written
    list quietly covering three quarters of the gate. That has already happened
    twice on this guard.

    DATASET AXIS: ``DatasetMetrics`` is the parsed shape of one dataset row, so
    its non-name fields ARE the floored per-dataset quantities.

    REGIME AXIS: ``RegimeRow`` carries its floored scalar once as ``baseline``
    and once as ``candidate``, so the regime side floors exactly ONE quantity.
    That is asserted rather than assumed — a second regime scalar cannot be
    added without growing ``RegimeRow``, which trips the check below.
    """
    regime_values = {f.name for f in fields(RegimeRow)} - {"key", "verdict", "note"}
    assert regime_values == {"baseline", "candidate"}, (
        f"RegimeRow's value fields changed to {sorted(regime_values)}. The regime axis no "
        f"longer floors exactly one scalar; give the new one a case and a check."
    )
    return ({f.name for f in fields(DatasetMetrics)} - {"name"}) | {"recall"}


def test_PRIMARY_regenerating_the_floor_cannot_launder_a_token_regression() -> None:
    """PRIMARY SHAPE 1 — the attack that forced DIRECTION to exist.

    One PR improves logs@90 and regresses search@90, then regenerates the floor
    wholesale. ``compare_baseline`` is blind to this by construction: candidate
    == committed floor, so it reports failures=0 improvements=0. The masked
    regression is visible only as an UPWARD floor move, which must FAIL here.
    """
    mixed = _snap({"logs@90": 560, "search@90": 420, "code@7": 1678})  # search 336 -> 420
    report = evaluate(
        floor_main=_MAIN,
        floor_pr=mixed,  # wholesale regenerate hides the regression from compare_baseline
        candidate=mixed,
        changed_files=[*_FEATURE_DIFF, *_REFLOOR_DIFF],
    )
    assert not report.ok, "a regression laundered through a floor regenerate slipped past"
    assert _checks(report) == {("DIRECTION", "dataset")}
    finding = report.findings[0]
    assert finding.subject == "search@90"
    assert "LOOSENS the gate" in finding.message
    assert "standalone PR" in finding.message


def test_PRIMARY_no_quantity_the_gate_floors_is_left_unguarded() -> None:
    """PRIMARY SHAPE 2 — the half-guarded failure, measured twice before fixing.

    ``compare_baseline`` floors THREE quantities: ``tokens_after`` and
    ``information_retention`` per dataset, and recall per regime. Early versions
    of this guard read only ``tokens_after``, then only tokens and recall — each
    time naming the loosening class and defending part of it. Both gaps were
    MEASURED blind, not theorised: loosen either of the other two on both sides
    at once and compare_baseline prints "failures=0 / no regressions", exit 0.

    Every gated quantity must produce a finding when loosened inside a PR that
    changes other files. The expected set is DERIVED from ``compare_baseline``'s
    own types rather than retyped here, so a fourth floored quantity turns this
    red on the PR that adds it — see ``_gated_quantities``. An earlier version
    of this docstring promised that redness while iterating a hand-written dict
    that could never deliver it, which is the same defect one level up: a
    completeness claim is only as good as the enumeration behind it, and a
    retyped enumeration goes stale silently.
    """
    loosened = {
        "tokens_after": _snap({"logs@90": 900, "search@90": 336, "code@7": 1678}),
        "recall": _snap(regimes={**_MAIN_REGIMES, "naming/logs/visible_only": 0.75}),
        "information_retention": _snap(
            retention={**dict.fromkeys(_MAIN_TOKENS, 1.0), "logs@90": 0.60}
        ),
    }
    assert set(loosened) == _gated_quantities(), (
        "the gate's floored quantities and this test's cases have diverged. Add a case "
        "for the new quantity AND a check for it in floor_guard.evaluate — the guard is "
        "blind to anything absent from both."
    )

    for quantity, floor_pr in loosened.items():
        report = evaluate(
            floor_main=_MAIN,
            floor_pr=floor_pr,  # wholesale regenerate: candidate agrees with the floor
            candidate=floor_pr,
            changed_files=[*_FEATURE_DIFF, *_REFLOOR_DIFF],
        )
        assert not report.ok, f"a loosened {quantity} floor drew no finding — guard is half-blind"
        assert {f.check for f in report.findings} == {"DIRECTION"}, quantity

    retired = _snap(regimes={k: v for k, v in _MAIN_REGIMES.items() if "/logs/" not in k})
    report = evaluate(
        floor_main=_MAIN, floor_pr=retired, candidate=retired, changed_files=_FEATURE_DIFF
    )
    assert not report.ok, "a retired recall regime drew no finding"
    assert _checks(report) == {("DIRECTION", "regime")}
    assert {f.subject for f in report.findings} == {
        "naming/logs/output_or_ccr",
        "naming/logs/visible_only",
    }
    assert "UNBOUNDED loosening" in report.findings[0].message


def test_PRIMARY_a_vacant_comparison_cannot_report_a_verified_floor() -> None:
    """PRIMARY SHAPE 3 — the guard turned on itself.

    Every loop in ``evaluate`` is a no-op over an empty mapping, so before this
    check the guard rendered ``datasets=0 regimes=0 findings=0 / RESULT: floor
    still describes reality`` and returned ok. That sentence reads as a verified
    floor and means an unread file: green that does not distinguish "I checked
    and it held" from "there was nothing for me to check".

    The same shape was found the same night in an unrelated subsystem, where a
    route-coverage guard counted MISSES and would have passed with the whole
    family deleted. Both fixes are the same instruction: ASSERT THE POSITIVE
    COUNT. Here that is "there were entries on both axes to compare".
    """
    empty = FloorSnapshot(tokens={}, retention={}, regimes={})
    for role, snapshots in {
        "candidate": {"floor_main": _MAIN, "floor_pr": _MAIN, "candidate": empty},
        "floor_pr": {"floor_main": _MAIN, "floor_pr": empty, "candidate": _MAIN},
        "floor_main": {"floor_main": empty, "floor_pr": _MAIN, "candidate": _MAIN},
    }.items():
        with pytest.raises(FloorGuardInputError, match="nothing to compare"):
            evaluate(**snapshots, changed_files=_FEATURE_DIFF)  # type: ignore[arg-type]
        assert role  # names which side was hollowed out, for the failure message

    # Half-empty counts as empty: one populated axis must not license the other.
    with pytest.raises(FloorGuardInputError, match="nothing to compare"):
        evaluate(
            floor_main=_MAIN,
            floor_pr=_MAIN,
            candidate=FloorSnapshot(tokens=dict(_MAIN_TOKENS), retention={}, regimes={}),
            changed_files=_FEATURE_DIFF,
        )


def test_shape_a_output_moved_but_floor_untouched_fails() -> None:
    """SHAPE A — the #172 situation. Compression improves, nobody re-floors, so
    the floor keeps forgiving inflation nobody voted to forgive. Must FAIL."""
    report = evaluate(
        floor_main=_MAIN,
        floor_pr=_MAIN,  # untouched
        candidate=_snap({"logs@90": 500, "search@90": 336, "code@7": 1678}),
        changed_files=_FEATURE_DIFF,
    )
    assert not report.ok
    assert _checks(report) == {("DRIFT", "dataset")}
    finding = report.findings[0]
    assert finding.subject == "logs@90"
    assert "regenerate the floor IN THIS PR" in finding.message
    assert "cannot hide a regression" in finding.message  # the remedy states why it is safe


def test_shape_b_output_moved_and_floor_moved_with_it_passes() -> None:
    """SHAPE B — the improvement recorded honestly. Floor regenerated to the
    candidate, every movement downward. Must PASS even though it rides along
    with feature work: a tightening is self-justifying."""
    improved = _snap({"logs@90": 500, "search@90": 330, "code@7": 1678})
    report = evaluate(
        floor_main=_MAIN,
        floor_pr=improved,  # regenerated in this PR
        candidate=improved,
        changed_files=[*_FEATURE_DIFF, *_REFLOOR_DIFF],
    )
    assert report.ok, [f.render() for f in report.findings]


def test_shape_c_sub_threshold_improvement_stays_quiet() -> None:
    """SHAPE C — a one-token gain must not nag. 1/2701 is 0.04%, far under the
    2% the gate already forgives, so there is no headroom worth a red build."""
    report = evaluate(
        floor_main=_snap({"diff_raw@238": 2701}),
        floor_pr=_snap({"diff_raw@238": 2701}),
        candidate=_snap({"diff_raw@238": 2700}),
        changed_files=_FEATURE_DIFF,
    )
    assert report.ok, [f.render() for f in report.findings]


def test_a_standalone_re_floor_may_raise_the_floor() -> None:
    """The deliberate, argued loosening. Same upward movement as PRIMARY 1, but
    the PR touches nothing else, so it is reviewed as itself. The guard draws
    its line at what rode along, never at direction alone. PASS."""
    raised = _snap({"logs@90": 700, "search@90": 420, "code@7": 1678})
    report = evaluate(
        floor_main=_MAIN,
        floor_pr=raised,
        candidate=raised,
        changed_files=_REFLOOR_DIFF,
    )
    assert report.ok, [f.render() for f in report.findings]
    assert report.baseline_only_pr


# # Entries present on only one side — the shapes someone would reach for to walk past a per-entry comparison.


def test_retiring_a_dataset_is_treated_as_an_upward_move() -> None:
    """Dropping a dataset removes its ceiling entirely, which is a STRONGER
    loosening than raising it, so it needs the same standalone PR."""
    retired = _snap({"logs@90": 603, "code@7": 1678})  # search@90 retired
    report = evaluate(
        floor_main=_MAIN,
        floor_pr=retired,
        candidate=retired,
        changed_files=_FEATURE_DIFF,
    )
    assert not report.ok, "a dataset silently dropped from the floor drew no finding"
    assert _checks(report) == {("DIRECTION", "dataset")}
    assert report.findings[0].subject == "search@90"
    assert "UNBOUNDED loosening" in report.findings[0].message


def test_retiring_is_allowed_in_a_standalone_re_floor_for_both_sets() -> None:
    """Retirement is legitimate — it just has to be reviewed as itself. Same
    exemption for a dataset and for a regime; one rule, not two."""
    retired = _snap(
        tokens={"logs@90": 603, "code@7": 1678},
        regimes={k: v for k, v in _MAIN_REGIMES.items() if "/logs/" not in k},
    )
    report = evaluate(
        floor_main=_MAIN,
        floor_pr=retired,
        candidate=retired,
        changed_files=_REFLOOR_DIFF,
    )
    assert report.ok, [f.render() for f in report.findings]


def test_a_new_entry_is_silent_for_direction_but_still_covered_by_drift() -> None:
    """A new dataset or regime has no previous floor to have moved FROM, so
    DIRECTION is silent BY CONSTRUCTION rather than by accident. Neither is
    unguarded: a first floor set off from what it actually measures fails
    DRIFT — above reality for tokens, below it for recall."""
    honest = _snap(
        tokens={**_MAIN_TOKENS, "new@50": 400},
        regimes={**_MAIN_REGIMES, "naming/new/visible_only": 1.0},
    )
    clean = evaluate(
        floor_main=_MAIN, floor_pr=honest, candidate=honest, changed_files=_FEATURE_DIFF
    )
    assert clean.ok, [f.render() for f in clean.findings]

    inflated = evaluate(
        floor_main=_MAIN,
        floor_pr=honest,  # both floors set away from reality
        candidate=_snap(
            tokens={**_MAIN_TOKENS, "new@50": 100},  # measures far under its token floor
            regimes={**_MAIN_REGIMES, "naming/new/visible_only": 1.0},
        ),
        changed_files=_FEATURE_DIFF,
    )
    assert _checks(inflated) == {("DRIFT", "dataset")}
    assert inflated.findings[0].subject == "new@50"


# --------------------------------------------------------------------------- # Recall regimes: same two checks, inverted arithmetic. Bigger recall is
# BETTER, so the loosening direction is DOWN and DRIFT is a floor BELOW reality. --------------------------------------------------------------------------- #


def test_lowering_a_recall_floor_inside_a_feature_pr_fails() -> None:
    """The regime mirror of the token laundering attack. A wholesale regenerate
    rewrites recall floors too, so a recall regression would launder exactly the
    same way if DIRECTION only understood 'up'."""
    degraded = _snap(regimes={**_MAIN_REGIMES, "naming/logs/visible_only": 0.75})
    report = evaluate(
        floor_main=_MAIN,
        floor_pr=degraded,
        candidate=degraded,
        changed_files=[*_FEATURE_DIFF, *_REFLOOR_DIFF],
    )
    assert not report.ok, "a laundered recall regression slipped past"
    assert _checks(report) == {("DIRECTION", "regime")}
    finding = report.findings[0]
    assert finding.subject == "naming/logs/visible_only"
    assert "1.0000 -> 0.7500" in finding.message
    assert "DOWN is the loosening direction" in finding.message


def test_a_recall_floor_below_reality_is_drift() -> None:
    """The #172 shape for recall: the engine recalls better than the floor
    demands, so the gate silently forgives a drop back to the stale floor."""
    report = evaluate(
        floor_main=_MAIN,
        floor_pr=_snap(regimes={**_MAIN_REGIMES, "naming/logs/visible_only": 0.80}),
        candidate=_snap(),  # measures 1.0
        changed_files=_REFLOOR_DIFF,  # baseline-only, so DIRECTION cannot fire
    )
    assert _checks(report) == {("DRIFT", "regime")}
    assert "would forgive a drop all the way back to 0.8000" in report.findings[0].message


def test_raising_a_recall_floor_is_a_tightening_and_may_ride_along() -> None:
    """The mirror of the token rule: for recall, UP is the tightening. It is
    self-justifying and must not need a standalone PR."""
    report = evaluate(
        floor_main=_snap(regimes={**_MAIN_REGIMES, "naming/logs/visible_only": 0.90}),
        floor_pr=_snap(),  # raised back to 1.0
        candidate=_snap(),
        changed_files=_FEATURE_DIFF,
    )
    assert report.ok, [f.render() for f in report.findings]


def test_lowering_a_retention_floor_inside_a_feature_pr_fails() -> None:
    """The third gated quantity, and the third blind spot. Dropping the
    information_retention floor 1.00 -> 0.60 on both sides launders a 40% loss
    of retained information past compare_baseline entirely."""
    degraded = _snap(retention={**dict.fromkeys(_MAIN_TOKENS, 1.0), "logs@90": 0.60})
    report = evaluate(
        floor_main=_MAIN,
        floor_pr=degraded,
        candidate=degraded,
        changed_files=[*_FEATURE_DIFF, *_REFLOOR_DIFF],
    )
    assert _checks(report) == {("DIRECTION", "dataset")}
    finding = report.findings[0]
    assert finding.subject == "logs@90"
    assert "information_retention floor 1.0000 -> 0.6000" in finding.message


def test_a_retention_floor_below_reality_is_drift() -> None:
    """#172's shape for retention: the engine keeps more than the floor demands,
    so the gate silently forgives a fall back to the stale number."""
    report = evaluate(
        floor_main=_MAIN,
        floor_pr=_snap(retention={**dict.fromkeys(_MAIN_TOKENS, 1.0), "logs@90": 0.80}),
        candidate=_snap(),  # measures 1.0
        changed_files=_REFLOOR_DIFF,  # baseline-only, so DIRECTION cannot fire
    )
    assert _checks(report) == {("DRIFT", "dataset")}
    assert "information_retention floor is 0.8000" in report.findings[0].message


def test_baseline_only_needs_an_actual_diff() -> None:
    """An empty diff is not a deliberate re-floor — nothing was argued for."""
    assert is_baseline_only(_REFLOOR_DIFF)
    assert is_baseline_only(["benchmarks/BASELINE.md"])
    assert not is_baseline_only([])
    assert not is_baseline_only(["benchmarks/baseline_results.json", "README.md"])


def test_every_check_is_reported_together() -> None:
    """A PR can be wrong in several ways at once; no check may mask another."""
    report = evaluate(
        floor_main=_MAIN,
        floor_pr=FloorSnapshot(
            tokens={"logs@90": 700, "search@90": 336, "code@7": 1678},  # raised
            retention={"logs@90": 0.60, "search@90": 1.0, "code@7": 1.0},  # lowered
            regimes={**_MAIN_REGIMES, "naming/logs/visible_only": 0.75},  # lowered
        ),
        candidate=_snap({"logs@90": 500, "search@90": 336, "code@7": 1678}),  # drifted
        changed_files=_FEATURE_DIFF,
    )
    assert _checks(report) == {
        ("DRIFT", "dataset"),
        ("DIRECTION", "dataset"),
        ("DRIFT", "regime"),
        ("DIRECTION", "regime"),
    }
    # All three quantities are represented, not just the two that share a scope.
    assert {f.subject for f in report.findings if f.check == "DIRECTION"} == {
        "logs@90",  # raised tokens AND lowered retention, same dataset
        "naming/logs/visible_only",
    }
    # FOUR findings on logs@90, not three: its token floor was both raised (DIRECTION) and left above what the run
    # measures (DRIFT), and its retention floor was both lowered (DIRECTION) and left below what the run measures (DRIFT).
    assert sum(1 for f in report.findings if f.subject == "logs@90") == 4


# --------------------------------------------------------------------------- # Artifact handling. A broken input
# must never read as a clean run. --------------------------------------------------------------------------- #


def _write_results(
    path: Path,
    tokens: Mapping[str, int],
    logs_recall: float = 1.0,
    retention: float = 1.0,
) -> Path:
    """A real ``baseline_results.json`` shape, parsed by the production flattener."""
    path.write_text(
        json.dumps(
            {
                "datasets": [
                    {"name": k, "tokens_after": v, "information_retention": retention}
                    for k, v in tokens.items()
                ],
                "needle_recall": {
                    "overall_output_or_ccr": 1.0,
                    "overall_visible_only": 1.0,
                    "by_family": {
                        "logs": {
                            "recall_output_or_ccr": 1.0,
                            "recall_visible_only": logs_recall,
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_malformed_input_is_an_input_error_not_a_clean_run(tmp_path: Path) -> None:
    """Missing, broken, or half-a-file must all raise rather than read clean."""
    bad = tmp_path / "bad.json"
    bad.write_text('{"datasets": [{"name": "x"}]}', encoding="utf-8")
    with pytest.raises(FloorGuardInputError):
        load_snapshot(bad)
    with pytest.raises(FloorGuardInputError):
        load_snapshot(tmp_path / "does_not_exist.json")

    # Datasets present, needle_recall absent: the half-file that would have let
    # the regime checks silently no-op if they defaulted instead of failing.
    half = tmp_path / "half.json"
    half.write_text(
        json.dumps({"datasets": [{"name": "x", "tokens_after": 1, "information_retention": 1.0}]}),
        encoding="utf-8",
    )
    with pytest.raises(FloorGuardInputError, match="needle_recall"):
        load_snapshot(half)

    # tokens_after present, information_retention absent: the same silent no-op
    # one field over. A floor we cannot read is never a floor we may skip.
    partial = tmp_path / "partial.json"
    partial.write_text(
        json.dumps({"datasets": [{"name": "x", "tokens_after": 1}], "needle_recall": {}}),
        encoding="utf-8",
    )
    with pytest.raises(FloorGuardInputError, match="information_retention"):
        load_snapshot(partial)


def test_the_loader_reads_every_floored_quantity(tmp_path: Path) -> None:
    """All three come back populated, with regime keys in the gate's own
    ``arm/scope/metric`` naming rather than this module's invention."""
    snapshot = load_snapshot(_write_results(tmp_path / "r.json", _MAIN_TOKENS, retention=0.5))
    assert snapshot.tokens == _MAIN_TOKENS
    assert snapshot.retention == dict.fromkeys(_MAIN_TOKENS, 0.5)
    assert snapshot.regimes == {
        "naming/overall/output_or_ccr": 1.0,
        "naming/overall/visible_only": 1.0,
        "naming/logs/output_or_ccr": 1.0,
        "naming/logs/visible_only": 1.0,
    }


def test_cli_exit_codes(tmp_path: Path) -> None:
    """0 clean, 1 findings, 2 input error — the same contract compare_baseline
    uses, so a broken artifact can never be mistaken for a green gate."""
    floor = _write_results(tmp_path / "floor.json", _MAIN_TOKENS)
    clean = _write_results(tmp_path / "clean.json", _MAIN_TOKENS)
    drifted = _write_results(
        tmp_path / "drifted.json", {"logs@90": 500, "search@90": 336, "code@7": 1678}
    )
    regime_drift = _write_results(tmp_path / "recall.json", _MAIN_TOKENS, logs_recall=0.5)
    changed = tmp_path / "changed.txt"
    changed.write_text("\n".join(_FEATURE_DIFF), encoding="utf-8")

    argv = ["--floor-main", str(floor), "--floor-pr", str(floor), "--changed-files", str(changed)]
    assert main([*argv, "--candidate", str(clean)]) == 0
    assert main([*argv, "--candidate", str(drifted)]) == 1
    assert main([*argv, "--candidate", str(tmp_path / "missing.json")]) == 2
    # A recall floor above what the run measures is a REGRESSION, which is compare_baseline's
    # job, not this guard's: it must stay quiet rather than double-report.
    assert main([*argv, "--floor-pr", str(regime_drift), "--candidate", str(clean)]) == 1
    assert (
        main(
            [
                "--floor-main",
                str(floor),
                "--floor-pr",
                str(floor),
                "--changed-files",
                str(changed),
                "--candidate",
                str(regime_drift),
            ]
        )
        == 0
    )
