"""Run the Imp3 honest benchmark suite against the CURRENT engine.

Produces the Phase-2 baseline:
- Per-dataset metrics (code / logs / search): lossless token reduction,
  lossy drop ratio, information retention.
- Needle-recall (two regimes: search=lossless, logs=lossy) — both the
  "visible in output" recall and the "output OR CCR-recoverable" recall.

By default writes to a temporary directory so it NEVER clobbers the committed
``benchmarks/baseline_results.json`` / ``benchmarks/BASELINE.md``.  Pass
``--out <dir>`` to redirect output (e.g. ``--out benchmarks`` to re-baseline
in-place, then commit).

Deterministic and re-runnable::

    .venv/bin/python -m benchmarks.run_bench               # writes to DEFAULT_OUT_DIR
    .venv/bin/python -m benchmarks.run_bench --out /tmp/x  # writes to /tmp/x/
    .venv/bin/python -m benchmarks.run_bench --out benchmarks  # in-place (re-baseline)
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from benchmarks import datasets as ds_mod
from benchmarks.metrics import (
    BENCH_MODEL,
    BenchmarkAbortedError,
    CaseMetrics,
    measure_case,
    measure_conversation_case,
)
from benchmarks.needle_recall import (
    NeedleResult,
    recall_rate,
    run_needle_recall,
)

HERE = Path(__file__).resolve().parent
# Committed baseline files — these are the floor.  Default run NEVER writes here.
RESULTS_JSON = HERE / "baseline_results.json"
BASELINE_MD = HERE / "BASELINE.md"
DATA_DIR = HERE / "data"

# Default out-dir for normal (non-re-baselining) runs.
# Must be outside benchmarks/ so default runs leave `git status benchmarks/` clean.
DEFAULT_OUT_DIR: Path = Path(tempfile.gettempdir()) / "furl_bench"


# ---------------------------------------------------------------------------
# Dataset run.
# ---------------------------------------------------------------------------


def run_datasets() -> list[CaseMetrics]:
    """Measure all real datasets at their default cardinality.

    Single-tool datasets keep the strict last-message scoring
    (``measure_case``); multi-turn conversation datasets are scored across
    all messages (``measure_conversation_case``).
    """
    results: list[CaseMetrics] = []
    for dataset in ds_mod.all_datasets():
        name = f"{dataset.name}@{len(dataset.items)}"
        measure = measure_conversation_case if dataset.conversation else measure_case
        results.append(measure(name, dataset.query, dataset.items, dataset.messages))
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

    print("\n=== NEEDLE-RECALL (current engine; naming vs control query arms) ===")
    print(
        f"{'arm':8s} {'family':8s} {'card':>4s} {'position':9s} {'in_output':>9s} "
        f"{'ccr_recov':>9s} {'recalled':>8s}"
    )
    for r in needles:
        print(
            f"{r.arm:8s} {r.family:8s} {r.cardinality:4d} {r.position:9s} "
            f"{str(r.in_output):>9s} {str(r.ccr_recoverable):>9s} "
            f"{str(r.recalled):>8s}"
        )

    # Per-arm summaries — NEVER blended (the control arm exists to expose
    # the gap the naming arm hides).
    for arm in ("naming", "control"):
        arm_trials = [r for r in needles if r.arm == arm]
        if not arm_trials:
            continue
        overall = recall_rate(arm_trials)
        visible = sum(1 for r in arm_trials if r.in_output) / len(arm_trials)
        print(
            f"\nNEEDLE-RECALL [{arm}] overall (output OR CCR): {_pct(overall)}   "
            f"visible-in-output only: {_pct(visible)}"
        )
        for fam in ("search", "logs"):
            sub = [r for r in arm_trials if r.family == fam]
            if not sub:
                continue
            fam_recall = recall_rate(sub)
            fam_visible = sum(1 for r in sub if r.in_output) / len(sub)
            print(
                f"  {fam:7s}: recall(output|CCR)={_pct(fam_recall)}  "
                f"visible-only={_pct(fam_visible)}"
            )


def _arm_summary(trials: list[NeedleResult]) -> dict[str, object]:
    """Recall summary (overall + by family) for ONE query arm's trials."""
    overall = recall_rate(trials)
    visible = sum(1 for r in trials if r.in_output) / len(trials) if trials else 1.0
    by_family: dict[str, dict[str, float]] = {}
    for fam in ("search", "logs"):
        sub = [r for r in trials if r.family == fam]
        if not sub:
            continue
        by_family[fam] = {
            "recall_output_or_ccr": recall_rate(sub),
            "recall_visible_only": sum(1 for r in sub if r.in_output) / len(sub),
            "trials": len(sub),
        }
    return {
        "overall_output_or_ccr": overall,
        "overall_visible_only": visible,
        "by_family": by_family,
    }


def build_results_payload(
    cases: list[CaseMetrics], needles: list[NeedleResult]
) -> dict[str, object]:
    """Assemble the machine-readable results envelope.

    The top-level ``needle_recall`` numbers are the NAMING arm — the same
    best-case-by-construction semantics the committed floor was captured
    with (== 100%). The honest
    non-quoting-user numbers live SEPARATELY under ``needle_recall.control``
    (EFF-7): the control arm is expected to be lower on the lossy path and
    is deliberately NOT part of the 100% gate.
    """
    naming = [r for r in needles if r.arm == "naming"]
    control = [r for r in needles if r.arm == "control"]
    summary = _arm_summary(naming)

    return {
        "schema": "furl_ctx.imp3.baseline.v1",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "engine_model": BENCH_MODEL,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "provenance": snapshot_provenance(),
        "datasets": [asdict(c) for c in cases],
        "needle_recall": {
            **summary,
            "control": _arm_summary(control),
            "trials": [asdict(r) for r in needles],
        },
    }


# ---------------------------------------------------------------------------
# BASELINE.md.
# ---------------------------------------------------------------------------


def render_baseline_md(payload: dict[str, object]) -> str:
    """Render the human BASELINE.md from the results payload."""
    # Runtime no-ops: the payload is a JSON-shaped dict[str, object]; these
    # casts state the concrete shapes build_results_payload assembles.
    cases = cast("list[dict[str, Any]]", payload["datasets"])
    needle = cast("dict[str, Any]", payload["needle_recall"])
    prov = cast("list[dict[str, Any]]", payload["provenance"])

    lines: list[str] = []
    a = lines.append
    a("# BASELINE — Imp3 Honest Benchmark (Phase-2 current engine)")
    a("")
    a(f"- Captured: `{payload['captured_at_utc']}`")
    a(f"- Commit: `{payload['git_commit']}`")
    a(
        f"- Token model: `{payload['engine_model']}` (real tiktoken BPE via the engine's tokenizer registry)"
    )
    a(f"- Python: `{payload['python']}`  Platform: `{payload['platform']}`")
    a("")
    a("All numbers come from REAL captured data (no synthetic low-entropy")
    a("inputs). Token counts use the SAME tokenizer the engine uses")
    a("(`furl_ctx.tokenizers.get_tokenizer` -> `Tokenizer`).")
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
    a(
        "| dataset | items | tok before | tok after | lossless reduction | lossy drop ratio | info retention | path |"
    )
    a("|---|---:|---:|---:|---:|---:|---:|---|")
    for c in cases:
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
    a("Two query arms per trial, reported separately (EFF-7): **naming**")
    a("(the query quotes the needle's sentinel token — best-case recall BY")
    a("CONSTRUCTION; this is the number the floor gate checks) and")
    a("**control** (the query describes the need without any of the needle's")
    a("literal tokens — the honest non-quoting-user number).")
    a("")
    a(
        f"- Naming-arm recall (visible OR CCR-recoverable): **{needle['overall_output_or_ccr'] * 100:.1f}%**"
    )
    a(f"- Naming-arm *visible-in-output* recall: **{needle['overall_visible_only'] * 100:.1f}%**")
    control = needle.get("control", {})
    if control:
        a(
            f"- Control-arm recall (visible OR CCR-recoverable): **{control['overall_output_or_ccr'] * 100:.1f}%**"
        )
        a(
            f"- Control-arm *visible-in-output* recall: **{control['overall_visible_only'] * 100:.1f}%**"
        )
    a("")
    a("| arm | family | recall (output\\|CCR) | recall (visible-only) | trials |")
    a("|---|---|---:|---:|---:|")
    for arm_name, arm_stats in (("naming", needle), ("control", control)):
        for fam, stats in arm_stats.get("by_family", {}).items():
            a(
                f"| {arm_name} | {fam} | {stats['recall_output_or_ccr'] * 100:.1f}% | "
                f"{stats['recall_visible_only'] * 100:.1f}% | {stats['trials']} |"
            )
    a("")
    a("### Per-trial needle outcomes")
    a("")
    a("| arm | family | card | position | in_output | ccr_recoverable | recalled |")
    a("|---|---|---:|---|---|---|---|")
    for r in needle["trials"]:
        a(
            f"| {r.get('arm', 'naming')} | {r['family']} | {r['cardinality']} | "
            f"{r['position']} | {r['in_output']} | {r['ccr_recoverable']} | {r['recalled']} |"
        )
    a("")
    a("## Data provenance")
    a("")
    a("Raw captures are committed under `benchmarks/data/` so every number is")
    a("auditable and re-derivable. Capture commands:")
    a("")
    for p in prov:
        a(
            f"- **{p['dataset']}** ({p['n_items_default']} items, snapshot "
            f"`{p['snapshot']}` = {p['snapshot_bytes']} bytes): {p['provenance']}"
        )
    a("")
    a("## Honest read")
    a("")
    # Derived from the CAPTURED numbers above (not a hand-written story that
    # can rot when the engine changes).
    lossy_names = [c["name"] for c in cases if c["took_lossy_path"]]
    lossless_names = [c["name"] for c in cases if not c["took_lossy_path"]]
    min_retention = min((c["information_retention"] for c in cases), default=1.0)
    if lossy_names:
        a(
            f"- **Deletion-backed savings**: {len(lossy_names)}/{len(cases)} datasets "
            f"({', '.join(lossy_names)}) ship with rows dropped from the visible"
        )
        a("  output — their savings are NOT free; every drop must be (and is)")
        a("  covered by a CCR recovery pointer.")
    if lossless_names:
        a(
            f"- **True zero-loss savings**: {', '.join(lossless_names)} "
            "(drop ratio 0.0% — the only rows where the reduction is free)."
        )
    a(f"- **Retention floor**: {min_retention * 100:.1f}% — every dataset's dropped rows")
    a("  resolve through the emitted recovery pointers (sentinel `<<ccr:HASH>>`")
    a("  or the raw-text `Retrieve …: hash=…` marker) against the live store.")
    a("- **Needle honesty (EFF-7)**: the naming arm is best-case BY")
    a("  CONSTRUCTION (the query quotes the sentinel, query-aware keep pins")
    a("  it). The control arm is what a user who cannot quote the row gets:")
    if control:
        a(
            f"  visible-in-output recall {control['overall_visible_only'] * 100:.1f}% vs "
            f"{needle['overall_visible_only'] * 100:.1f}% naming — the gap is the retrieval"
        )
        a("  cost a non-quoting user pays on the lossy path (CCR round-trip,")
        a("  not silent loss, while recall-output-or-CCR holds).")
    a("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the full suite, print the table, write JSON + BASELINE.md.

    Measures off the COMMITTED snapshots under benchmarks/data/ (deterministic).
    Pass ``--refresh`` to re-capture live from local sources and overwrite the
    snapshots — only do this when intentionally re-baselining, then commit them.

    ``--out <dir>`` redirects where baseline_results.json and BASELINE.md are
    written.  Defaults to DEFAULT_OUT_DIR (a temp dir) so a plain invocation
    NEVER dirties the committed baseline files.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    refresh = "--refresh" in args

    # A2: fail CLOSED on a broken engine. Running `python -m benchmarks.run_bench`
    # from the repo root puts the extension-less source tree ahead of the built
    # wheel on sys.path, so every compress() fail-opens ("No module named
    # 'furl_ctx._core'") and the harness would otherwise write a plausible-looking
    # all-zero baseline (0% reduction, "lossless", 100% recall). Verify the native
    # core imports up front and abort BEFORE measuring or writing anything.
    try:
        import furl_ctx._core  # noqa: F401  (native extension presence check)
    except Exception as exc:  # noqa: BLE001 - any import failure is fatal here
        print(
            "ERROR: furl_ctx._core native extension is not importable "
            f"({exc}).\n  compress() would fail open on every case and produce a "
            "fictional all-zero baseline. Build/install the wheel (maturin develop) "
            "and run from OUTSIDE the source tree, or `pip install -e .`. "
            "Refusing to write a misleading baseline.",
            file=sys.stderr,
        )
        return 1

    # Parse --out <dir>
    out_dir = DEFAULT_OUT_DIR
    if "--out" in args:
        idx = args.index("--out")
        if idx + 1 >= len(args):
            print("ERROR: --out requires a directory argument", file=sys.stderr)
            return 1
        out_dir = Path(args[idx + 1])

    # Fail loudly if any snapshot is missing — never silently re-capture.
    required_snapshots = (
        "code",
        "logs",
        "search",
        "ci_log",
        "grep_raw",
        "diff_raw",
        "markdown_doc",
    )
    missing = [name for name in required_snapshots if not (DATA_DIR / f"{name}.raw.json").exists()]
    if missing and not refresh:
        print(
            f"ERROR: committed snapshots missing: {missing}\n"
            f"  Re-run with --refresh to capture them, then commit benchmarks/data/.",
            file=sys.stderr,
        )
        return 1

    if refresh:
        ds_mod.all_datasets(refresh=True)

    # A2: any per-item compress() fail-open aborts the whole run BEFORE the write
    # below, so a broken engine never produces a baseline file at all.
    try:
        cases = run_datasets()
        needles = run_needle_recall()
    except BenchmarkAbortedError as exc:
        print(f"ERROR: benchmark aborted (fail-closed): {exc}", file=sys.stderr)
        return 1

    print_table(cases, needles)

    payload = build_results_payload(cases, needles)
    out_dir.mkdir(parents=True, exist_ok=True)
    results_json = out_dir / "baseline_results.json"
    baseline_md = out_dir / "BASELINE.md"
    results_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    baseline_md.write_text(render_baseline_md(payload), encoding="utf-8")

    print(f"\nWrote {results_json}")
    print(f"Wrote {baseline_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
