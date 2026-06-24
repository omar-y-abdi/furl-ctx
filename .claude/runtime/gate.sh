#!/usr/bin/env bash
# No-regression gate for the mass-repair effort. Run after every agent change.
#   gate.sh         -> G1 cargo + G2 pytest + G3 surface + G4 recovery (code-safe set)
#   gate.sh bench   -> the above + G5 run_bench floor-check (for changes touching compression)
# Exit 0 = all gates pass. Nonzero = a gate failed (do NOT commit; revert/restart agent).
set -uo pipefail
cd /Users/k/dev/headroom
PY=.venv/bin/python
fail=0

echo "=== G1 cargo (headroom-core) ==="
if cargo test -p headroom-core 2>&1 | grep -E 'test result:' | grep -qv ' 0 failed'; then
  echo "G1 FAIL: a cargo suite reported failures"; fail=1
else
  echo "G1 PASS"
fi

echo "=== G2 pytest (full) ==="
if $PY -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=180 2>&1 | tail -3 | grep -qE '[0-9]+ failed|error'; then
  echo "G2 FAIL: pytest failures"; fail=1
else
  echo "G2 PASS"
fi

echo "=== G3 surface-walk ==="
if $PY -c "import headroom; [getattr(headroom,n) for n in headroom.__all__]" 2>&1; then
  echo "G3 PASS"
else
  echo "G3 FAIL: import/surface broke"; fail=1
fi

echo "=== G4 recovery invariant (must be 23) ==="
if $PY -m pytest tests/test_ccr_recovery_invariant.py -q --no-header -p no:cacheprovider 2>&1 | grep -q '23 passed'; then
  echo "G4 PASS"
else
  echo "G4 FAIL: recovery invariant not 21-green"; fail=1
fi

if [ "${1:-}" = "bench" ]; then
  echo "=== G5 run_bench + floor-check ==="
  $PY -m benchmarks.run_bench >/tmp/runbench.out 2>&1
  if $PY .claude/runtime/floor_check.py; then
    echo "G5 PASS"
  else
    echo "G5 FAIL: compression regression"; fail=1
  fi
  # run_bench overwrites these — always restore
  git checkout HEAD -- benchmarks/baseline_results.json benchmarks/BASELINE.md
fi

echo "==============================="
if [ "$fail" = "0" ]; then echo "GATE: PASS"; else echo "GATE: FAIL"; fi
exit $fail
