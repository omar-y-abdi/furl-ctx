#!/usr/bin/env python3
"""Compare freshly-written benchmarks/baseline_results.json against the committed
floor (git HEAD). PASS iff every dataset's lossless_reduction + information_retention
is >= floor (within epsilon) and needle-recall (output OR CCR) == 100%.

Run AFTER `python -m benchmarks.run_bench` (which overwrites baseline_results.json),
then restore the baseline files. Exit 0 = PASS (no regression), 1 = FAIL.
"""
from __future__ import annotations

import json
import subprocess
import sys

EPS = 1e-6
CUR_PATH = "benchmarks/baseline_results.json"


def _load_committed() -> dict:
    raw = subprocess.run(
        ["git", "show", f"HEAD:{CUR_PATH}"],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(raw)


def _by_name(doc: dict) -> dict[str, dict]:
    return {d["name"]: d for d in doc["datasets"]}


def main() -> int:
    floor = _load_committed()
    cur = json.load(open(CUR_PATH))

    # Reject stale captures: if the timestamp hasn't changed, run_bench didn't
    # actually write a fresh measurement (e.g. it crashed before writing output).
    floor_ts = floor.get("captured_at_utc")
    cur_ts = cur.get("captured_at_utc")
    if cur_ts is not None and floor_ts is not None and cur_ts == floor_ts:
        print("FLOOR CHECK: FAIL")
        print("  - captured_at_utc unchanged: current file is identical to HEAD "
              "(run_bench did not produce a fresh measurement)")
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
        print(f"  {name}: lossless={cd['lossless_reduction']:.3f} retain={cd['information_retention']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
