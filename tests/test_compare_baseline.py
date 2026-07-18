"""Tests for ``benchmarks.compare_baseline`` (the benchmark regression gate).

Every candidate fixture is built by deep-copying the REAL committed
``benchmarks/baseline_results.json`` and mutating it in memory, so the fixtures
always track the true schema rather than a hand-written stand-in. The comparison
core is pure, so the logic tests need no mocks; the CLI edge is exercised with
real temp files and the exit code is asserted directly.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from benchmarks.compare_baseline import (
    CompareInputError,
    Verdict,
    compare,
    main,
    parse_results,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BASELINE_PATH = _REPO_ROOT / "benchmarks" / "baseline_results.json"

# A dataset that exists in the committed baseline; used as the mutation target.
_TARGET = "code@7"


@pytest.fixture(scope="module")
def baseline_raw() -> dict[str, Any]:
    """The real committed baseline, parsed once and shared read-only."""
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


def _candidate(baseline_raw: dict[str, Any]) -> dict[str, Any]:
    """A deep, independently-mutable copy of the baseline to shape into a candidate."""
    return copy.deepcopy(baseline_raw)


def _dataset(raw: dict[str, Any], name: str) -> dict[str, Any]:
    """Return the mutable dataset entry named ``name`` from a raw results dict."""
    for entry in raw["datasets"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"dataset {name!r} not present in fixture")


def test_committed_baseline_parses_clean(baseline_raw: dict[str, Any]) -> None:
    results = parse_results(baseline_raw, "baseline")
    assert _TARGET in results.datasets
    # Both arms flatten into named regimes.
    assert "naming/overall/output_or_ccr" in results.regimes
    assert "control/logs/visible_only" in results.regimes


def test_identical_passes(baseline_raw: dict[str, Any]) -> None:
    base = parse_results(baseline_raw, "baseline")
    candidate = parse_results(_candidate(baseline_raw), "candidate")
    report = compare(base, candidate)
    assert report.ok
    assert report.failures == ()
    assert report.improvements == ()
    assert all(row.verdict is Verdict.OK for row in report.dataset_rows)
    assert all(row.verdict is Verdict.OK for row in report.regime_rows)


def test_retention_drop_fails(baseline_raw: dict[str, Any]) -> None:
    candidate_raw = _candidate(baseline_raw)
    _dataset(candidate_raw, _TARGET)["information_retention"] = 0.5
    report = compare(
        parse_results(baseline_raw, "baseline"), parse_results(candidate_raw, "candidate")
    )
    assert not report.ok
    assert any("information_retention regressed" in message for message in report.failures)


def test_token_inflation_beyond_slack_fails(baseline_raw: dict[str, Any]) -> None:
    candidate_raw = _candidate(baseline_raw)
    target = _dataset(candidate_raw, _TARGET)
    # Push tokens clearly past the +2% ceiling.
    target["tokens_after"] = int(target["tokens_after"] * 1.05) + 1
    report = compare(
        parse_results(baseline_raw, "baseline"), parse_results(candidate_raw, "candidate")
    )
    assert not report.ok
    assert any("tokens_after inflated" in message for message in report.failures)


def test_token_inflation_within_slack_passes(baseline_raw: dict[str, Any]) -> None:
    candidate_raw = _candidate(baseline_raw)
    target = _dataset(candidate_raw, _TARGET)
    # +1 token stays well under the 2% ceiling, so it is tolerated, not flagged.
    target["tokens_after"] = target["tokens_after"] + 1
    report = compare(
        parse_results(baseline_raw, "baseline"), parse_results(candidate_raw, "candidate")
    )
    assert report.ok


def test_recall_drop_fails(baseline_raw: dict[str, Any]) -> None:
    candidate_raw = _candidate(baseline_raw)
    candidate_raw["needle_recall"]["overall_visible_only"] = 0.5
    report = compare(
        parse_results(baseline_raw, "baseline"), parse_results(candidate_raw, "candidate")
    )
    assert not report.ok
    assert any(
        "naming/overall/visible_only" in message and "recall regressed" in message
        for message in report.failures
    )


def test_token_improvement_passes_with_note(baseline_raw: dict[str, Any]) -> None:
    candidate_raw = _candidate(baseline_raw)
    target = _dataset(candidate_raw, _TARGET)
    target["tokens_after"] = target["tokens_after"] - 50
    report = compare(
        parse_results(baseline_raw, "baseline"), parse_results(candidate_raw, "candidate")
    )
    assert report.ok
    assert any("tokens_after improved" in message for message in report.improvements)
    row = next(row for row in report.dataset_rows if row.name == _TARGET)
    assert row.verdict is Verdict.IMPROVEMENT


def test_recall_improvement_passes_with_note(baseline_raw: dict[str, Any]) -> None:
    candidate_raw = _candidate(baseline_raw)
    # The control/logs visible-only recall is 0.2222 in the floor; raise it.
    candidate_raw["needle_recall"]["control"]["by_family"]["logs"]["recall_visible_only"] = 0.9
    report = compare(
        parse_results(baseline_raw, "baseline"), parse_results(candidate_raw, "candidate")
    )
    assert report.ok
    assert any("control/logs/visible_only" in message for message in report.improvements)


def test_missing_dataset_fails(baseline_raw: dict[str, Any]) -> None:
    candidate_raw = _candidate(baseline_raw)
    candidate_raw["datasets"] = [
        entry for entry in candidate_raw["datasets"] if entry["name"] != _TARGET
    ]
    report = compare(
        parse_results(baseline_raw, "baseline"), parse_results(candidate_raw, "candidate")
    )
    assert not report.ok
    assert any("missing dataset" in message for message in report.failures)


def test_unknown_dataset_fails(baseline_raw: dict[str, Any]) -> None:
    candidate_raw = _candidate(baseline_raw)
    extra = copy.deepcopy(_dataset(candidate_raw, _TARGET))
    extra["name"] = "brand_new@1"
    candidate_raw["datasets"].append(extra)
    report = compare(
        parse_results(baseline_raw, "baseline"), parse_results(candidate_raw, "candidate")
    )
    assert not report.ok
    assert any("unknown dataset" in message for message in report.failures)


def test_missing_regime_fails(baseline_raw: dict[str, Any]) -> None:
    candidate_raw = _candidate(baseline_raw)
    del candidate_raw["needle_recall"]["control"]
    report = compare(
        parse_results(baseline_raw, "baseline"), parse_results(candidate_raw, "candidate")
    )
    assert not report.ok
    assert any("missing regime" in message for message in report.failures)


def test_malformed_schema_missing_field_raises(baseline_raw: dict[str, Any]) -> None:
    candidate_raw = _candidate(baseline_raw)
    del _dataset(candidate_raw, _TARGET)["information_retention"]
    with pytest.raises(CompareInputError) as excinfo:
        parse_results(candidate_raw, "candidate")
    assert "information_retention" in str(excinfo.value)


def test_malformed_schema_bad_type_raises(baseline_raw: dict[str, Any]) -> None:
    candidate_raw = _candidate(baseline_raw)
    _dataset(candidate_raw, _TARGET)["tokens_after"] = "many"
    with pytest.raises(CompareInputError) as excinfo:
        parse_results(candidate_raw, "candidate")
    assert "tokens_after" in str(excinfo.value)


def test_cli_clean_exit_zero_writes_summary(tmp_path: Path, baseline_raw: dict[str, Any]) -> None:
    baseline_file = tmp_path / "base.json"
    baseline_file.write_text(json.dumps(baseline_raw), encoding="utf-8")
    candidate_file = tmp_path / "cand.json"
    candidate_file.write_text(json.dumps(baseline_raw), encoding="utf-8")
    summary = tmp_path / "summary.md"
    code = main(
        [
            "--baseline",
            str(baseline_file),
            "--candidate",
            str(candidate_file),
            "--markdown-summary",
            str(summary),
        ]
    )
    assert code == 0
    text = summary.read_text(encoding="utf-8")
    assert "Benchmark regression gate: PASS" in text
    assert _TARGET in text


def test_cli_regression_exit_one(tmp_path: Path, baseline_raw: dict[str, Any]) -> None:
    candidate_raw = _candidate(baseline_raw)
    _dataset(candidate_raw, _TARGET)["information_retention"] = 0.1
    baseline_file = tmp_path / "base.json"
    baseline_file.write_text(json.dumps(baseline_raw), encoding="utf-8")
    candidate_file = tmp_path / "cand.json"
    candidate_file.write_text(json.dumps(candidate_raw), encoding="utf-8")
    code = main(["--baseline", str(baseline_file), "--candidate", str(candidate_file)])
    assert code == 1


def test_cli_invalid_json_exit_two(tmp_path: Path, baseline_raw: dict[str, Any]) -> None:
    baseline_file = tmp_path / "base.json"
    baseline_file.write_text(json.dumps(baseline_raw), encoding="utf-8")
    candidate_file = tmp_path / "cand.json"
    candidate_file.write_text("{ not valid json", encoding="utf-8")
    code = main(["--baseline", str(baseline_file), "--candidate", str(candidate_file)])
    assert code == 2


def test_cli_missing_file_exit_two(tmp_path: Path, baseline_raw: dict[str, Any]) -> None:
    baseline_file = tmp_path / "base.json"
    baseline_file.write_text(json.dumps(baseline_raw), encoding="utf-8")
    code = main(["--baseline", str(baseline_file), "--candidate", str(tmp_path / "absent.json")])
    assert code == 2


def test_cli_baseline_updated_note(tmp_path: Path, baseline_raw: dict[str, Any]) -> None:
    baseline_file = tmp_path / "base.json"
    baseline_file.write_text(json.dumps(baseline_raw), encoding="utf-8")
    candidate_file = tmp_path / "cand.json"
    candidate_file.write_text(json.dumps(baseline_raw), encoding="utf-8")
    summary = tmp_path / "summary.md"
    code = main(
        [
            "--baseline",
            str(baseline_file),
            "--candidate",
            str(candidate_file),
            "--markdown-summary",
            str(summary),
            "--baseline-updated",
        ]
    )
    assert code == 0
    assert "Baseline was updated in this PR" in summary.read_text(encoding="utf-8")


def test_cli_nan_retention_exit_two(
    tmp_path: Path, baseline_raw: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    candidate_raw = _candidate(baseline_raw)
    _dataset(candidate_raw, _TARGET)["information_retention"] = float("nan")
    baseline_file = tmp_path / "base.json"
    baseline_file.write_text(json.dumps(baseline_raw), encoding="utf-8")
    candidate_file = tmp_path / "cand.json"
    candidate_file.write_text(json.dumps(candidate_raw), encoding="utf-8")
    code = main(["--baseline", str(baseline_file), "--candidate", str(candidate_file)])
    assert code == 2
    assert "finite" in capsys.readouterr().err


def test_cli_infinity_recall_exit_two(
    tmp_path: Path, baseline_raw: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    candidate_raw = _candidate(baseline_raw)
    candidate_raw["needle_recall"]["overall_output_or_ccr"] = float("inf")
    baseline_file = tmp_path / "base.json"
    baseline_file.write_text(json.dumps(baseline_raw), encoding="utf-8")
    candidate_file = tmp_path / "cand.json"
    candidate_file.write_text(json.dumps(candidate_raw), encoding="utf-8")
    code = main(["--baseline", str(baseline_file), "--candidate", str(candidate_file)])
    assert code == 2
    assert "finite" in capsys.readouterr().err
