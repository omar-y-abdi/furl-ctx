#!/usr/bin/env python3
"""Compare a freshly-written benchmark capture against the committed floor (git HEAD).

PASS iff every dataset's lossless_reduction + information_retention is >= floor
(within epsilon) and needle-recall (output OR CCR) == 100%.

Run AFTER `python -m benchmarks.run_bench` (which writes to DEFAULT_OUT_DIR by
default, NOT to benchmarks/).  The committed baseline_results.json is the floor;
the fresh capture is read from DEFAULT_OUT_DIR (or --cur <path>).

Exit 0 = PASS (no regression), 1 = FAIL.

Usage:
    python .claude/runtime/floor_check.py
    python .claude/runtime/floor_check.py --cur /tmp/my_out/baseline_results.json
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

EPS = 1e-6

# The committed baseline in the repo — used as the floor via `git show`.
# This path is relative to the repo root (for `git show HEAD:<path>`).
FLOOR_COMMITTED = "benchmarks/baseline_results.json"

# Default location where run_bench writes its fresh capture (must match
# DEFAULT_OUT_DIR in benchmarks/run_bench.py).
_DEFAULT_CUR: Path = Path(tempfile.gettempdir()) / "headroom_bench" / "baseline_results.json"


def _load_committed() -> dict:
    raw = subprocess.run(
        ["git", "show", f"HEAD:{FLOOR_COMMITTED}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(raw)


def _by_name(doc: dict) -> dict[str, dict]:
    return {d["name"]: d for d in doc["datasets"]}


def main() -> int:
    # Parse optional --cur <path>
    cur_path = _DEFAULT_CUR
    args = sys.argv[1:]
    if "--cur" in args:
        idx = args.index("--cur")
        if idx + 1 >= len(args):
            print("ERROR: --cur requires a path argument", file=sys.stderr)
            return 1
        cur_path = Path(args[idx + 1])

    if not cur_path.exists():
        print("FLOOR CHECK: FAIL")
        print(f"  - current capture not found: {cur_path}")
        print("    Run `python -m benchmarks.run_bench` first.")
        return 1

    floor = _load_committed()
    cur = json.load(open(cur_path))

    # Reject stale captures: if the timestamp hasn't changed, run_bench didn't
    # actually write a fresh measurement (e.g. it crashed before writing output).
    floor_ts = floor.get("captured_at_utc")
    cur_ts = cur.get("captured_at_utc")
    if cur_ts is not None and floor_ts is not None and cur_ts == floor_ts:
        print("FLOOR CHECK: FAIL")
        print(
            "  - captured_at_utc unchanged: current file is identical to HEAD "
            "(run_bench did not produce a fresh measurement)"
        )
        return 1

    f_sets, c_sets = _by_name(floor), _by_name(cur)
    failures: list[str] = []

    for name, fd in f_sets.items():
        cd = c_sets.get(name)
        if cd is None:
            failures.append(f"{name}: MISSING in current run")
            continue
        if cd["lossless_reduction"] < fd["lossless_reduction"] - EPS:
            failures.append(
                f"{name}: lossless {cd['lossless_reduction']:.4f} < floor "
                f"{fd['lossless_reduction']:.4f}"
            )
        if cd["information_retention"] < fd["information_retention"] - EPS:
            failures.append(
                f"{name}: retention {cd['information_retention']:.4f} < floor "
                f"{fd['information_retention']:.4f}"
            )

    needle = cur["needle_recall"]["overall_output_or_ccr"]
    if needle < 1.0 - EPS:
        failures.append(f"needle-recall (output|CCR) {needle:.4f} < 1.0")

    if failures:
        print("FLOOR CHECK: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("FLOOR CHECK: PASS (no compression regression, needle 100%)")
    for name, cd in c_sets.items():
        print(
            f"  {name}: lossless={cd['lossless_reduction']:.3f} retain={cd['information_retention']:.3f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
