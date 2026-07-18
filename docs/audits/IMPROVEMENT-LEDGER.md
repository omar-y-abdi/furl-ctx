# Improvement Ledger

Anti-repeat memory for the daily improvement routine, see
`.github/claude/daily-improve.md`. Every automated improvement session appends
one row here in its own PR. The routine must not pick an area listed in the
last 30 days, nor a module a merged PR touched in the last 14 days.

## Completed

| date | area | files or module | result | PR |
|---|---|---|---|---|
| 2026-07-12 | cleanup, dead code tiers 1-4 | repo-wide, see docs/audits/lazy-dev-AUDIT-*.md | vestigial modules and dead Rust removed after 4 audit passes | multiple |
| 2026-07-16 | docs truth | NOTICE, README provenance | Headroom fork provenance corrected | #109 |
| 2026-07-17 | test isolation | tests, FURL_WORKSPACE_DIR | pytest sandboxed away from ~/.furl | #111 |
| 2026-07-17 | docs truth | README harness story, security claims, env vars | docs reconciled with code reality | #112 |
| 2026-07-17 | release engineering | release 1.3.0 line, hook audit fixes, MCP pinning | library 1.3.0 and plugin 1.3.2 shipped | #87 |
| 2026-07-18 | compression correctness | crates/furl-core log_compressor, furl_ctx wrapper | unique log lines preserved, CCR marker on every drop, no silent loss | #118 |
| 2026-07-18 | CI hardening | .github/workflows, tests/test_ci_required_checks_guard.py | timeouts, permission floors, pins, required-check deadlock guard | #119 |
| 2026-07-18 | CI automation | autofix.yml, perf.yml, benchmarks/compare_baseline.py | autofix with PAT push, perf and rust regression gate, baseline refreshed | #120 |

## Open candidates, fair game for future sessions

- Capture the committed benchmark baseline on Linux CI instead of macOS so the
  perf gate compares same-OS numbers. Review finding F3 on PR 120: recall has
  a knife-edge regime at 0.2222 where a single cross-OS trial flip would false
  fail. Empirically green today; structural fix is a CI-captured baseline.
- faA2 SIGINT timing test in tests/test_hook_audit_fixes.py flakes under heavy
  parallel load, hardcoded 20s timeout. Harden per the test-quality contract.
- Test-quality iteration over live modules per the phase-4 hardening plan:
  boundary coverage, red-proof rigor, fewer mock-heavy tests.
- Benchmark corpus growth: add a new real-world dataset family to
  benchmarks/datasets.py with provenance notes.
- Property-based tests for the tabling grammar round-trip, encode then decode
  equals identity.
- mypy strictness: pyproject notes an unused mlx.* override; audit and tighten
  one module to strict.
- CI timing telemetry on main pushes, warn-only trend line, design sketched in
  the perf gate PR discussion.
