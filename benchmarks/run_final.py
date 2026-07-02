"""Run the honest benchmark suite against the FINAL engine (HEAD).

Phase-2 closure: measures the same datasets + needle-recall as the baseline,
ADDS the real ``repeated_logs`` dataset, and runs the Improvement-2
field-aware dedup A/B. Writes ``benchmarks/final_results.json`` for the
before→after table in ``BENCHMARKS.md``.

It deliberately does NOT overwrite ``baseline_results.json`` / ``BASELINE.md``
— those are the *before* snapshot (captured at the pre-1A/Imp2/1B commit) and
must stay frozen so the before→after comparison is honest.

Deterministic and re-runnable off the committed snapshots::

    .venv/bin/python -m benchmarks.run_final
"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from benchmarks import datasets as ds_mod
from benchmarks.imp2_ab import Imp2AB, measure_imp2_ab
from benchmarks.metrics import BENCH_MODEL, CaseMetrics, measure_case, measure_conversation_case
from benchmarks.needle_recall import NeedleResult, recall_rate, run_needle_recall
from benchmarks.run_bench import git_commit, print_table, snapshot_provenance

HERE = Path(__file__).resolve().parent
FINAL_JSON = HERE / "final_results.json"
DATA_DIR = HERE / "data"

# Datasets that must already have a committed snapshot for a deterministic run.
_REQUIRED_SNAPSHOTS = ("code", "logs", "search", "repeated_logs")


def run_datasets() -> list[CaseMetrics]:
    """Measure every real dataset (incl. repeated_logs) at default cardinality.

    Single-tool datasets use ``measure_case``; multi-turn conversation datasets
    use ``measure_conversation_case`` (mirrors run_bench.run_datasets).
    """
    results: list[CaseMetrics] = []
    for dataset in ds_mod.all_datasets():
        name = f"{dataset.name}@{len(dataset.items)}"
        measure = measure_conversation_case if dataset.conversation else measure_case
        results.append(measure(name, dataset.query, dataset.items, dataset.messages))
    return results


def run_imp2_ab() -> Imp2AB:
    """Field-aware dedup A/B on the real repeated-content dataset."""
    items = ds_mod.repeated_log_rows(limit=90)
    return measure_imp2_ab("repeated_logs", items)


def _print_ab(ab: Imp2AB) -> None:
    print("\n=== IMP2 FIELD-AWARE DEDUP A/B (repeated_logs, real ping replies) ===")
    print(f"rows                         : {ab.n_rows}")
    print(f"identity exclude-set         : {ab.exclude_set or '(empty)'}")
    print(f"Imp2 OFF (whole-item hash)   : {ab.families_off} distinct families")
    print(f"Imp2 ON  (field-aware hash)  : {ab.families_on} distinct families")
    print(f"redundancy recovered         : {ab.collapse_ratio * 100:.1f}%")
    print(f"largest field-aware family   : {ab.largest_family_on}")
    print(f"engine lossy-path strategy   : {ab.engine_strategy}")
    print(f"engine real max _dup_count   : {ab.engine_max_dup_count}")


def build_payload(
    cases: list[CaseMetrics], needles: list[NeedleResult], ab: Imp2AB
) -> dict[str, object]:
    overall = recall_rate(needles)
    visible = sum(1 for r in needles if r.in_output) / len(needles) if needles else 1.0
    needle_by_family: dict[str, dict[str, float]] = {}
    for fam in ("search", "logs"):
        sub = [r for r in needles if r.family == fam]
        if not sub:
            continue
        needle_by_family[fam] = {
            "recall_output_or_ccr": recall_rate(sub),
            "recall_visible_only": sum(1 for r in sub if r.in_output) / len(sub),
            "trials": len(sub),
        }
    return {
        "schema": "headroom.imp3.final.v1",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "engine_model": BENCH_MODEL,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "provenance": snapshot_provenance(),
        "datasets": [asdict(c) for c in cases],
        "imp2_ab": asdict(ab),
        "needle_recall": {
            "overall_output_or_ccr": overall,
            "overall_visible_only": visible,
            "by_family": needle_by_family,
            "trials": [asdict(r) for r in needles],
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    refresh = "--refresh" in args

    # Fail loudly if any required snapshot is missing — never silently re-capture.
    missing = [name for name in _REQUIRED_SNAPSHOTS if not (DATA_DIR / f"{name}.raw.json").exists()]
    if missing and not refresh:
        print(
            f"ERROR: committed snapshots missing: {missing}\n"
            f"  Re-run with --refresh to capture them, then commit benchmarks/data/.",
            file=sys.stderr,
        )
        return 1

    if refresh:
        ds_mod.all_datasets(refresh=True)

    cases = run_datasets()
    needles = run_needle_recall()
    ab = run_imp2_ab()

    print_table(cases, needles)
    _print_ab(ab)

    payload = build_payload(cases, needles, ab)
    FINAL_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {FINAL_JSON.relative_to(HERE.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
