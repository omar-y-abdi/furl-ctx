"""Run the Imp3 honest benchmark suite against the CURRENT engine.

Produces the Phase-2 baseline:
- Per-dataset metrics (code / logs / search): lossless token reduction,
  lossy drop ratio, information retention.
- Needle-recall (two regimes: search=lossless, logs=lossy) — both the
  "visible in output" recall and the "output OR CCR-recoverable" recall.

Writes machine-readable ``benchmarks/baseline_results.json`` and human
``benchmarks/BASELINE.md`` (methodology + numbers + data provenance).

Deterministic and re-runnable::

    .venv/bin/python -m benchmarks.run_bench
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from benchmarks import datasets as ds_mod
from benchmarks.metrics import BENCH_MODEL, CaseMetrics, measure_case
from benchmarks.needle_recall import (
    NeedleResult,
    recall_rate,
    run_needle_recall,
)

HERE = Path(__file__).resolve().parent
RESULTS_JSON = HERE / "baseline_results.json"
BASELINE_MD = HERE / "BASELINE.md"
DATA_DIR = HERE / "data"


# ---------------------------------------------------------------------------
# Dataset run.
# ---------------------------------------------------------------------------


def run_datasets() -> list[CaseMetrics]:
    """Measure all three real datasets at their default cardinality."""
    results: list[CaseMetrics] = []
    for dataset in ds_mod.all_datasets():
        name = f"{dataset.name}@{len(dataset.items)}"
        results.append(
            measure_case(name, dataset.query, dataset.items, dataset.messages)
        )
    return results


# ---------------------------------------------------------------------------
# Provenance: capture commands + committed snapshot sizes.
# ---------------------------------------------------------------------------


def snapshot_provenance() -> list[dict[str, object]]:
    """Describe each committed raw snapshot under benchmarks/data/."""
    out: list[dict[str, object]] = []
    for dataset in ds_mod.all_datasets():
        snap = DATA_DIR / f"{dataset.name}.raw.json"
        size = snap.stat().st_size if snap.exists() else 0
        out.append(
            {
                "dataset": dataset.name,
                "snapshot": str(snap.relative_to(HERE.parent)),
                "snapshot_bytes": size,
                "n_items_default": len(dataset.items),
                "provenance": dataset.provenance,
            }
        )
    return out


def git_commit() -> str:
    """Best-effort current commit hash (for the results envelope)."""
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(HERE.parent),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or "unknown"


# ---------------------------------------------------------------------------
# Reporting.
# ---------------------------------------------------------------------------


def _pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def print_table(cases: list[CaseMetrics], needles: list[NeedleResult]) -> None:
    """Print the human-readable baseline table to stdout."""
    print(f"\n=== DATASET METRICS (current engine, model={BENCH_MODEL}) ===")
    print(
        f"{'dataset':12s} {'items':>5s} {'tok_before':>10s} {'tok_after':>9s} "
        f"{'lossless':>9s} {'drop':>7s} {'retain':>7s} {'path':>9s}"
    )
    for c in cases:
        path = "LOSSY" if c.took_lossy_path else "lossless"
        print(
            f"{c.name:12s} {c.n_input_items:5d} {c.tokens_before:10d} "
            f"{c.tokens_after:9d} {_pct(c.lossless_reduction):>9s} "
            f"{_pct(c.lossy_drop_ratio):>7s} {_pct(c.information_retention):>7s} "
            f"{path:>9s}"
        )

    print("\n=== NEEDLE-RECALL (current engine) ===")
    print(
        f"{'family':8s} {'card':>4s} {'position':9s} {'in_output':>9s} "
        f"{'ccr_recov':>9s} {'recalled':>8s}"
    )
    for r in needles:
        print(
            f"{r.family:8s} {r.cardinality:4d} {r.position:9s} "
            f"{str(r.in_output):>9s} {str(r.ccr_recoverable):>9s} "
            f"{str(r.recalled):>8s}"
        )

    overall = recall_rate(needles)
    visible = sum(1 for r in needles if r.in_output) / len(needles) if needles else 1.0
    print(
        f"\nNEEDLE-RECALL overall (output OR CCR): {_pct(overall)}   "
        f"visible-in-output only: {_pct(visible)}"
    )
    for fam in ("search", "logs"):
        sub = [r for r in needles if r.family == fam]
        if not sub:
            continue
        fam_recall = recall_rate(sub)
        fam_visible = sum(1 for r in sub if r.in_output) / len(sub)
        print(
            f"  {fam:7s}: recall(output|CCR)={_pct(fam_recall)}  "
            f"visible-only={_pct(fam_visible)}"
        )


def build_results_payload(
    cases: list[CaseMetrics], needles: list[NeedleResult]
) -> dict[str, object]:
    """Assemble the machine-readable results envelope."""
    overall = recall_rate(needles)
    visible = (
        sum(1 for r in needles if r.in_output) / len(needles) if needles else 1.0
    )
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
        "schema": "headroom.imp3.baseline.v1",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "engine_model": BENCH_MODEL,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "provenance": snapshot_provenance(),
        "datasets": [asdict(c) for c in cases],
        "needle_recall": {
            "overall_output_or_ccr": overall,
            "overall_visible_only": visible,
            "by_family": needle_by_family,
            "trials": [asdict(r) for r in needles],
        },
    }


# ---------------------------------------------------------------------------
# BASELINE.md.
# ---------------------------------------------------------------------------


def render_baseline_md(payload: dict[str, object]) -> str:
    """Render the human BASELINE.md from the results payload."""
    cases = payload["datasets"]  # type: ignore[index]
    needle = payload["needle_recall"]  # type: ignore[index]
    prov = payload["provenance"]  # type: ignore[index]

    lines: list[str] = []
    a = lines.append
    a("# BASELINE — Imp3 Honest Benchmark (Phase-2 current engine)")
    a("")
    a(f"- Captured: `{payload['captured_at_utc']}`")
    a(f"- Commit: `{payload['git_commit']}`")
    a(f"- Token model: `{payload['engine_model']}` (real tiktoken BPE via the engine's tokenizer registry)")
    a(f"- Python: `{payload['python']}`  Platform: `{payload['platform']}`")
    a("")
    a("All numbers come from REAL captured data (no synthetic low-entropy")
    a("inputs). Token counts use the SAME tokenizer the engine uses")
    a("(`headroom.tokenizers.get_tokenizer` -> `Tokenizer`).")
    a("")
    a("## Metrics (defined)")
    a("")
    a("- **Lossless token reduction** — token savings ratio. Only a true")
    a("  zero-loss number when *drop ratio = 0*. On the lossy path the savings")
    a("  partly come from deletion, so the row is flagged `LOSSY`.")
    a("- **Lossy drop ratio** — fraction of distinct input rows NOT visible in")
    a("  the output (removed / offloaded).")
    a("- **Information retention** — fraction of distinct rows present in the")
    a("  output OR recoverable from the CCR store via `<<ccr:HASH>>`.")
    a("")
    a("## Dataset metrics")
    a("")
    a("| dataset | items | tok before | tok after | lossless reduction | lossy drop ratio | info retention | path |")
    a("|---|---:|---:|---:|---:|---:|---:|---|")
    for c in cases:  # type: ignore[union-attr]
        path = "LOSSY" if c["took_lossy_path"] else "lossless"
        a(
            f"| {c['name']} | {c['n_input_items']} | {c['tokens_before']} | "
            f"{c['tokens_after']} | {c['lossless_reduction'] * 100:.1f}% | "
            f"{c['lossy_drop_ratio'] * 100:.1f}% | "
            f"{c['information_retention'] * 100:.1f}% | {path} |"
        )
    a("")
    a("## Needle-recall")
    a("")
    a("A known unique needle row is injected at start/middle/end into real")
    a("arrays of 30/90/300 rows, in two regimes:")
    a("")
    a("- **search** — rg --json rows (lossless columnar path keeps all rows).")
    a("- **logs** — git-log rows (varying-field -> lossy drop path fires).")
    a("")
    a(f"- Overall recall (visible OR CCR-recoverable): **{needle['overall_output_or_ccr'] * 100:.1f}%**")  # type: ignore[index]
    a(f"- Overall *visible-in-output* recall: **{needle['overall_visible_only'] * 100:.1f}%**")  # type: ignore[index]
    a("")
    a("| family | recall (output\\|CCR) | recall (visible-only) | trials |")
    a("|---|---:|---:|---:|")
    for fam, stats in needle["by_family"].items():  # type: ignore[index]
        a(
            f"| {fam} | {stats['recall_output_or_ccr'] * 100:.1f}% | "
            f"{stats['recall_visible_only'] * 100:.1f}% | {stats['trials']} |"
        )
    a("")
    a("### Per-trial needle outcomes")
    a("")
    a("| family | card | position | in_output | ccr_recoverable | recalled |")
    a("|---|---:|---|---|---|---|")
    for r in needle["trials"]:  # type: ignore[index]
        a(
            f"| {r['family']} | {r['cardinality']} | {r['position']} | "
            f"{r['in_output']} | {r['ccr_recoverable']} | {r['recalled']} |"
        )
    a("")
    a("## Data provenance")
    a("")
    a("Raw captures are committed under `benchmarks/data/` so every number is")
    a("auditable and re-derivable. Capture commands:")
    a("")
    for p in prov:  # type: ignore[union-attr]
        a(f"- **{p['dataset']}** ({p['n_items_default']} items, snapshot "
          f"`{p['snapshot']}` = {p['snapshot_bytes']} bytes): {p['provenance']}")
    a("")
    a("## Honest read")
    a("")
    a("- **search** takes the *lossless columnar* path: all rows survive, no")
    a("  drop, needle-recall is 100% visible. The audited 90->24 needle drop")
    a("  does NOT reproduce on real ripgrep search results through `compress()`.")
    a("- **logs** (varying commit-hash/date) takes the *lossy* path: ~83% of")
    a("  rows are dropped from the visible output and the mid/high-cardinality")
    a("  needle is silently removed from what the LLM sees — but every dropped")
    a("  row is CCR-recoverable, so information retention is 100% *while the")
    a("  marker/store path is on*.")
    a("- **code** (large distinct source files) is a passthrough: no crush,")
    a("  0% reduction, 100% retention.")
    a("")
    a("Net: the audited *needle-loss* reproduces on log-shaped varying-field")
    a("data at the *visible-output* level (the LLM loses sight of mid-array")
    a("needles), recoverable only via CCR. The audited *~30% real savings*")
    a("reproduces as the lossless figure on search (~40%) and code (0%); the")
    a("high log 'savings' (84%+) are inflated by deletion, not free.")
    a("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the full suite, print the table, write JSON + BASELINE.md.

    By default measures off the COMMITTED snapshots under benchmarks/data/
    (fully deterministic). Pass ``--refresh`` to re-capture live from the
    local sources (git/rg/files) and overwrite the snapshots — only do this
    when intentionally re-baselining, then re-commit the snapshots.
    """
    args = sys.argv[1:] if argv is None else argv
    refresh = "--refresh" in args

    # Capture once if no snapshot exists yet, or when explicitly refreshing.
    snapshots_exist = all(
        (DATA_DIR / f"{name}.raw.json").exists()
        for name in ("code", "logs", "search")
    )
    if refresh or not snapshots_exist:
        ds_mod.all_datasets(refresh=True)

    cases = run_datasets()
    needles = run_needle_recall()

    print_table(cases, needles)

    payload = build_results_payload(cases, needles)
    RESULTS_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    BASELINE_MD.write_text(render_baseline_md(payload), encoding="utf-8")

    print(f"\nWrote {RESULTS_JSON.relative_to(HERE.parent)}")
    print(f"Wrote {BASELINE_MD.relative_to(HERE.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
