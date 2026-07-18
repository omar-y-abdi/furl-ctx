"""Benchmark baseline regression gate: compare a fresh run against the floor.

The committed ``benchmarks/baseline_results.json`` is the regression FLOOR. This
tool measures a fresh ``benchmarks.run_bench`` output (the candidate) against it
and fails loudly on any regression. It is deliberately dependency-light: pure
stdlib, no ``furl_ctx`` / ``benchmarks.metrics`` import, so the comparison core
stays a total, side-effect-free function and the tests need no built extension
for the logic itself and no mocks.

Regression rules, per dataset matched by ``name``:

* ``information_retention`` drops below the baseline value  -> FAIL
* ``tokens_after`` exceeds ``baseline * (1 + TOKEN_SLACK)``  -> FAIL

  ``tokens_after`` is a deterministic tiktoken count, so the slack is a small
  tolerance for tokenizer or environment drift, not a licence to regress.

Regression rule, per needle-recall regime (each arm x scope x metric):

* any recall value drops below the baseline value           -> FAIL

Structural rules, so a mismatch is never a silent pass:

* a dataset or regime present on ONE side but not the other  -> FAIL
* a required schema field missing or mistyped                -> input error

Improvements (fewer tokens, higher retention, higher recall) never fail the
gate; they are recorded and surfaced in the summary.

Exit codes, so a caller can tell the three outcomes apart:

* ``0`` clean: no regressions. Improvements, if any, are noted.
* ``1`` regression: at least one FAIL finding.
* ``2`` invalid input: unreadable file, malformed JSON, or schema mismatch.

Run as::

    python -m benchmarks.compare_baseline \\
        --baseline benchmarks/baseline_results.json \\
        --candidate /tmp/bench-now/baseline_results.json \\
        --markdown-summary "$GITHUB_STEP_SUMMARY"
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

# Deterministic tiktoken counts drift only marginally across tokenizer or OS
# builds, so a fresh run is allowed to land up to this fraction ABOVE the
# baseline token count before it counts as inflation. Retention and recall get
# no such slack: they are structural ratios and must never drop.
TOKEN_SLACK: Final = 0.02

# Float-noise guard for ratio metrics (retention, recall). A drop smaller than
# this is treated as unchanged, so identical inputs never flag a phantom
# regression; a genuine drop is always far larger.
_EPS: Final = 1e-9


class CompareInputError(Exception):
    """Input could not be turned into a comparable result.

    Covers an unreadable file, malformed JSON, and any schema mismatch. Kept
    distinct from a detected regression: the CLI maps this to exit code 2, while
    a regression exits 1. The message names the source and the offending field
    so the two input-error causes stay tellable apart.
    """


class Verdict(Enum):
    """Outcome of one compared metric or one structural check."""

    FAIL = "FAIL"
    IMPROVEMENT = "IMPROVED"
    OK = "PASS"


@dataclass(frozen=True)
class DatasetMetrics:
    """The regression-relevant slice of one dataset row."""

    name: str
    tokens_after: int
    information_retention: float


@dataclass(frozen=True)
class BenchmarkResults:
    """A parsed, validated results file reduced to what the gate compares."""

    datasets: Mapping[str, DatasetMetrics]
    regimes: Mapping[str, float]


@dataclass(frozen=True)
class DatasetRow:
    """Comparison outcome for one dataset across both sides."""

    name: str
    baseline: DatasetMetrics | None
    candidate: DatasetMetrics | None
    verdict: Verdict
    notes: tuple[str, ...]


@dataclass(frozen=True)
class RegimeRow:
    """Comparison outcome for one flattened needle-recall regime."""

    key: str
    baseline: float | None
    candidate: float | None
    verdict: Verdict
    note: str


@dataclass(frozen=True)
class ComparisonReport:
    """Full comparison: per-dataset rows plus per-regime rows."""

    dataset_rows: tuple[DatasetRow, ...]
    regime_rows: tuple[RegimeRow, ...]

    @property
    def ok(self) -> bool:
        """True when no check failed (improvements do not fail the gate)."""
        return not self.failures

    @property
    def failures(self) -> tuple[str, ...]:
        """Human-readable messages for every FAIL check, datasets then regimes."""
        messages: list[str] = []
        for row in self.dataset_rows:
            if row.verdict is Verdict.FAIL:
                messages.append(f"dataset {row.name}: {'; '.join(row.notes)}")
        for regime in self.regime_rows:
            if regime.verdict is Verdict.FAIL:
                messages.append(f"regime {regime.key}: {regime.note}")
        return tuple(messages)

    @property
    def improvements(self) -> tuple[str, ...]:
        """Human-readable messages for every improvement, datasets then regimes."""
        messages: list[str] = []
        for row in self.dataset_rows:
            if row.verdict is Verdict.IMPROVEMENT:
                messages.append(f"dataset {row.name}: {'; '.join(row.notes)}")
        for regime in self.regime_rows:
            if regime.verdict is Verdict.IMPROVEMENT:
                messages.append(f"regime {regime.key}: {regime.note}")
        return tuple(messages)


# --------------------------------------------------------------------------- #
# Parsing (boundary): raw JSON -> validated domain objects, or a typed error.
# --------------------------------------------------------------------------- #


def _as_number(value: object, source: str, field: str) -> float:
    """Coerce a JSON number to float, rejecting bools, non-numbers, and non-finite values.

    NaN and the infinities must be rejected at the boundary: every metric
    comparison uses ``<`` / ``>``, and every ordering against NaN is False, so a
    NaN retention or recall would slip through as OK and hide a real regression.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CompareInputError(
            f"{source}: field {field!r} must be a number, got {type(value).__name__}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise CompareInputError(f"{source}: field {field!r} must be a finite number, got {value!r}")
    return number


def _as_int(value: object, source: str, field: str) -> int:
    """Coerce a JSON integer, rejecting bools and non-integers.

    ``tokens_after`` is always an integer token count in the schema; a float
    there is schema drift worth failing on, not silently truncating.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise CompareInputError(
            f"{source}: field {field!r} must be an integer, got {type(value).__name__}"
        )
    return value


def _parse_datasets(raw: object, source: str) -> dict[str, DatasetMetrics]:
    """Validate and index the ``datasets`` list by dataset name."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise CompareInputError(f"{source}: 'datasets' must be a list, got {type(raw).__name__}")
    parsed: dict[str, DatasetMetrics] = {}
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise CompareInputError(f"{source}: datasets[{index}] must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise CompareInputError(f"{source}: datasets[{index}].name must be a non-empty string")
        location = f"dataset {name!r}"
        for required in ("tokens_after", "information_retention"):
            if required not in entry:
                raise CompareInputError(f"{source}: {location} missing required field {required!r}")
        if name in parsed:
            raise CompareInputError(f"{source}: duplicate dataset name {name!r}")
        parsed[name] = DatasetMetrics(
            name=name,
            tokens_after=_as_int(entry["tokens_after"], source, f"{location}.tokens_after"),
            information_retention=_as_number(
                entry["information_retention"], source, f"{location}.information_retention"
            ),
        )
    if not parsed:
        raise CompareInputError(f"{source}: 'datasets' is empty; nothing to compare")
    return parsed


def _flatten_arm(
    arm: Mapping[str, object], arm_name: str, source: str, out: dict[str, float]
) -> None:
    """Flatten one needle-recall arm into ``arm/scope/metric`` -> recall entries."""
    location = "needle_recall" if arm_name == "naming" else "needle_recall.control"
    for field, metric in (
        ("overall_output_or_ccr", "output_or_ccr"),
        ("overall_visible_only", "visible_only"),
    ):
        if field not in arm:
            raise CompareInputError(f"{source}: {location} missing required field {field!r}")
        out[f"{arm_name}/overall/{metric}"] = _as_number(arm[field], source, f"{location}.{field}")
    by_family = arm.get("by_family")
    if not isinstance(by_family, Mapping):
        raise CompareInputError(
            f"{source}: {location}.by_family must be an object, got {type(by_family).__name__}"
        )
    for family, stats in by_family.items():
        if not isinstance(stats, Mapping):
            raise CompareInputError(f"{source}: {location}.by_family.{family} must be an object")
        for field, metric in (
            ("recall_output_or_ccr", "output_or_ccr"),
            ("recall_visible_only", "visible_only"),
        ):
            if field not in stats:
                raise CompareInputError(
                    f"{source}: {location}.by_family.{family} missing field {field!r}"
                )
            out[f"{arm_name}/{family}/{metric}"] = _as_number(
                stats[field], source, f"{location}.by_family.{family}.{field}"
            )


def _parse_regimes(raw: object, source: str) -> dict[str, float]:
    """Flatten the ``needle_recall`` block into named recall regimes.

    The naming arm lives at the top level; the control arm, when present, is
    nested under ``control``. Control is optional here on purpose: if a
    candidate omits it while the baseline has it, that surfaces as a missing
    regime FAIL in :func:`compare`, never a silent pass.
    """
    if not isinstance(raw, Mapping):
        raise CompareInputError(
            f"{source}: 'needle_recall' must be an object, got {type(raw).__name__}"
        )
    regimes: dict[str, float] = {}
    _flatten_arm(raw, "naming", source, regimes)
    control = raw.get("control")
    if control is not None:
        if not isinstance(control, Mapping):
            raise CompareInputError(
                f"{source}: 'needle_recall.control' must be an object, got {type(control).__name__}"
            )
        _flatten_arm(control, "control", source, regimes)
    if not regimes:
        raise CompareInputError(f"{source}: 'needle_recall' produced no recall regimes")
    return regimes


def parse_results(raw: object, source: str) -> BenchmarkResults:
    """Validate raw JSON into a :class:`BenchmarkResults`, or raise on mismatch."""
    if not isinstance(raw, Mapping):
        raise CompareInputError(
            f"{source}: top-level JSON must be an object, got {type(raw).__name__}"
        )
    if "datasets" not in raw:
        raise CompareInputError(f"{source}: missing required key 'datasets'")
    if "needle_recall" not in raw:
        raise CompareInputError(f"{source}: missing required key 'needle_recall'")
    return BenchmarkResults(
        datasets=_parse_datasets(raw["datasets"], source),
        regimes=_parse_regimes(raw["needle_recall"], source),
    )


# --------------------------------------------------------------------------- #
# Comparison core (pure): two validated results -> a report. Never raises.
# --------------------------------------------------------------------------- #


def _compare_one_dataset(
    name: str,
    baseline: DatasetMetrics | None,
    candidate: DatasetMetrics | None,
    token_slack: float,
) -> DatasetRow:
    """Compare one dataset. Missing on either side is a structural FAIL."""
    if baseline is None:
        return DatasetRow(
            name,
            None,
            candidate,
            Verdict.FAIL,
            ("present in candidate but absent from baseline (unknown dataset)",),
        )
    if candidate is None:
        return DatasetRow(
            name,
            baseline,
            None,
            Verdict.FAIL,
            ("present in baseline but absent from candidate (missing dataset)",),
        )

    failures: list[str] = []
    if candidate.information_retention < baseline.information_retention - _EPS:
        failures.append(
            f"information_retention regressed {baseline.information_retention:.4f} "
            f"-> {candidate.information_retention:.4f}"
        )
    ceiling = baseline.tokens_after * (1.0 + token_slack)
    if candidate.tokens_after > ceiling:
        failures.append(
            f"tokens_after inflated {baseline.tokens_after} -> {candidate.tokens_after}, "
            f"above the {token_slack:.0%} ceiling of {ceiling:.1f}"
        )
    if failures:
        return DatasetRow(name, baseline, candidate, Verdict.FAIL, tuple(failures))

    improvements: list[str] = []
    if candidate.tokens_after < baseline.tokens_after:
        improvements.append(
            f"tokens_after improved {baseline.tokens_after} -> {candidate.tokens_after}"
        )
    if candidate.information_retention > baseline.information_retention + _EPS:
        improvements.append(
            f"information_retention improved {baseline.information_retention:.4f} "
            f"-> {candidate.information_retention:.4f}"
        )
    if improvements:
        return DatasetRow(name, baseline, candidate, Verdict.IMPROVEMENT, tuple(improvements))
    return DatasetRow(name, baseline, candidate, Verdict.OK, ())


def _compare_one_regime(key: str, baseline: float | None, candidate: float | None) -> RegimeRow:
    """Compare one recall regime. Missing on either side is a structural FAIL."""
    if baseline is None:
        return RegimeRow(
            key,
            None,
            candidate,
            Verdict.FAIL,
            "present in candidate but absent from baseline (unknown regime)",
        )
    if candidate is None:
        return RegimeRow(
            key,
            baseline,
            None,
            Verdict.FAIL,
            "present in baseline but absent from candidate (missing regime)",
        )
    if candidate < baseline - _EPS:
        return RegimeRow(
            key,
            baseline,
            candidate,
            Verdict.FAIL,
            f"recall regressed {baseline:.4f} -> {candidate:.4f}",
        )
    if candidate > baseline + _EPS:
        return RegimeRow(
            key,
            baseline,
            candidate,
            Verdict.IMPROVEMENT,
            f"recall improved {baseline:.4f} -> {candidate:.4f}",
        )
    return RegimeRow(key, baseline, candidate, Verdict.OK, "unchanged")


def compare(
    baseline: BenchmarkResults,
    candidate: BenchmarkResults,
    *,
    token_slack: float = TOKEN_SLACK,
) -> ComparisonReport:
    """Compare candidate against baseline over the union of datasets and regimes."""
    dataset_names = sorted(set(baseline.datasets) | set(candidate.datasets))
    dataset_rows = tuple(
        _compare_one_dataset(
            name, baseline.datasets.get(name), candidate.datasets.get(name), token_slack
        )
        for name in dataset_names
    )
    regime_keys = sorted(set(baseline.regimes) | set(candidate.regimes))
    regime_rows = tuple(
        _compare_one_regime(key, baseline.regimes.get(key), candidate.regimes.get(key))
        for key in regime_keys
    )
    return ComparisonReport(dataset_rows=dataset_rows, regime_rows=regime_rows)


# --------------------------------------------------------------------------- #
# Rendering.
# --------------------------------------------------------------------------- #


def _num(value: float | int | None) -> str:
    """Render an optional number for a table cell, or ``n/a`` when absent."""
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    return f"{value:.4f}"


def _verdict_cell(verdict: Verdict, detail: str) -> str:
    """Render a verdict plus its detail for a table cell."""
    return f"{verdict.value}: {detail}" if detail else verdict.value


def render_markdown(report: ComparisonReport, *, baseline_updated: bool) -> str:
    """Render a before/after markdown report for a step summary."""
    status = "PASS" if report.ok else "FAIL"
    lines: list[str] = [f"## Benchmark regression gate: {status}", ""]
    if baseline_updated:
        lines += [
            "> Baseline was updated in this PR. The comparison runs against the "
            "PR's own committed `benchmarks/baseline_results.json`.",
            "",
        ]
    lines += [
        "### Datasets",
        "",
        "| dataset | base tokens_after | cand tokens_after | base retention | cand retention | verdict |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for row in report.dataset_rows:
        base_tok = row.baseline.tokens_after if row.baseline else None
        cand_tok = row.candidate.tokens_after if row.candidate else None
        base_ret = row.baseline.information_retention if row.baseline else None
        cand_ret = row.candidate.information_retention if row.candidate else None
        lines.append(
            f"| {row.name} | {_num(base_tok)} | {_num(cand_tok)} | {_num(base_ret)} | "
            f"{_num(cand_ret)} | {_verdict_cell(row.verdict, '; '.join(row.notes))} |"
        )
    lines += [
        "",
        "### Needle-recall regimes",
        "",
        "| regime | base recall | cand recall | verdict |",
        "|---|---:|---:|---|",
    ]
    for regime in report.regime_rows:
        lines.append(
            f"| {regime.key} | {_num(regime.baseline)} | {_num(regime.candidate)} | "
            f"{_verdict_cell(regime.verdict, regime.note if regime.verdict is not Verdict.OK else '')} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_text(report: ComparisonReport, *, baseline_updated: bool) -> str:
    """Render a compact console summary of the comparison."""
    status = "PASS" if report.ok else "FAIL"
    lines: list[str] = [f"benchmark regression gate: {status}"]
    if baseline_updated:
        lines.append("baseline updated in this PR; compared against the PR's own committed floor")
    lines.append(
        f"datasets={len(report.dataset_rows)} regimes={len(report.regime_rows)} "
        f"failures={len(report.failures)} improvements={len(report.improvements)}"
    )
    for message in report.failures:
        lines.append(f"  FAIL {message}")
    for message in report.improvements:
        lines.append(f"  IMPROVED {message}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# I/O edge + CLI.
# --------------------------------------------------------------------------- #


def load_results(path: Path) -> BenchmarkResults:
    """Read and parse a results file, raising :class:`CompareInputError` on failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CompareInputError(f"cannot read {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CompareInputError(f"{path}: invalid JSON: {exc}") from exc
    return parse_results(raw, str(path))


def _append_markdown(path: Path, content: str) -> None:
    """Append rendered markdown to the summary file (GITHUB_STEP_SUMMARY semantics)."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)
        if not content.endswith("\n"):
            handle.write("\n")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="benchmarks.compare_baseline",
        description="Fail on any benchmark regression of a candidate run vs the committed baseline.",
    )
    parser.add_argument(
        "--baseline", required=True, type=Path, help="committed baseline_results.json"
    )
    parser.add_argument("--candidate", required=True, type=Path, help="fresh run_bench output JSON")
    parser.add_argument(
        "--markdown-summary",
        type=Path,
        default=None,
        help="path to append a markdown before/after table to (e.g. $GITHUB_STEP_SUMMARY)",
    )
    parser.add_argument(
        "--baseline-updated",
        action="store_true",
        help="note in the summary that this PR intentionally updated the baseline file",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns 0 clean, 1 on regression, 2 on invalid input."""
    args = _parse_args(argv)
    try:
        baseline = load_results(args.baseline)
        candidate = load_results(args.candidate)
    except CompareInputError as exc:
        print(f"ERROR (invalid input): {exc}", file=sys.stderr)
        return 2

    report = compare(baseline, candidate)
    print(render_text(report, baseline_updated=args.baseline_updated))
    if args.markdown_summary is not None:
        _append_markdown(
            args.markdown_summary, render_markdown(report, baseline_updated=args.baseline_updated)
        )

    if not report.ok:
        print(
            f"RESULT: regression detected, {len(report.failures)} failing check(s).",
            file=sys.stderr,
        )
        return 1
    print("RESULT: no regressions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
