#!/usr/bin/env bash
# No-regression gate for the mass-repair effort. Run after every agent change.
#   gate.sh         -> G1 cargo + G2 pytest + G3 surface + G4 recovery (code-safe set)
#   gate.sh bench   -> the above + G5 run_bench floor-check (for changes touching compression)
# Exit 0 = all gates pass. Nonzero = a gate failed (do NOT commit; revert/restart agent).
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"
PY=.venv/bin/python
fail=0

# Gate checks key on the EXIT CODE, never on grepping output. cargo/pytest exit
# nonzero on compile error, test failure, collection error, OR an unrecognized
# arg — a previous grep-on-`tail` version silently PASSED when `--timeout` was
# unrecognized (pytest-timeout uninstalled) because pytest bailed before running
# a single test and the error line scrolled past `tail -3`. Exit code can't lie.
echo "=== G1 cargo (headroom-core) ==="
if cargo test -p headroom-core >/tmp/gate_g1.log 2>&1; then
  echo "G1 PASS"
else
  echo "G1 FAIL: cargo exited nonzero (compile error or test failure)"; tail -5 /tmp/gate_g1.log; fail=1
fi

echo "=== G2 pytest (full) ==="
# --timeout needs pytest-timeout; add it only if importable so a stripped env
# degrades to no-timeout instead of silently skipping the whole suite.
G2_TIMEOUT=""; $PY -c "import pytest_timeout" 2>/dev/null && G2_TIMEOUT="--timeout=180"
if $PY -m pytest tests/ -q --no-header -p no:cacheprovider $G2_TIMEOUT >/tmp/gate_g2.log 2>&1; then
  echo "G2 PASS"
else
  echo "G2 FAIL: pytest exited nonzero (failure/error/collection)"; tail -5 /tmp/gate_g2.log; fail=1
fi

echo "=== G3 surface-walk ==="
if $PY -c "import headroom; [getattr(headroom,n) for n in headroom.__all__]" 2>&1; then
  echo "G3 PASS"
else
  echo "G3 FAIL: import/surface broke"; fail=1
fi

echo "=== G4 recovery invariant (must be 23) ==="
if $PY -m pytest tests/test_ccr_recovery_invariant.py -q --no-header -p no:cacheprovider >/tmp/gate_g4.log 2>&1; then
  if grep -q '23 passed' /tmp/gate_g4.log; then
    echo "G4 PASS"
  else
    echo "G4 FAIL: exit 0 but count != 23"; tail -5 /tmp/gate_g4.log; fail=1
  fi
else
  echo "G4 FAIL: recovery invariant exited nonzero"; tail -5 /tmp/gate_g4.log; fail=1
fi

if [ "${1:-}" = "bench" ]; then
  echo "=== G5 run_bench + floor-check ==="
  if $PY -m benchmarks.run_bench >/tmp/runbench.out 2>&1; then
    if $PY .claude/runtime/floor_check.py; then
      echo "G5 PASS"
    else
      echo "G5 FAIL: compression regression"; fail=1
    fi
  else
    echo "G5 FAIL: run_bench exited nonzero (crash/error)"; tail -5 /tmp/runbench.out; fail=1
  fi
  # run_bench overwrites these — always restore
  git checkout HEAD -- benchmarks/baseline_results.json benchmarks/BASELINE.md
fi

echo "==============================="
if [ "$fail" = "0" ]; then echo "GATE: PASS"; else echo "GATE: FAIL"; fi
exit $fail
