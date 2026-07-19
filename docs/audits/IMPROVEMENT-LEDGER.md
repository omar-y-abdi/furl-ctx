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
| 2026-07-18 | test rigor, faA2 timing race | tests/test_hook_audit_fixes.py | replaced the fixed pre-kill sleep with a /proc readiness poll, eliminating a proven load-dependent flake and cutting the two faA2 tests from 4.65s to 0.14s | #126 |
| 2026-07-19 | type strength, mypy overrides | pyproject.toml, furl_ctx/tokenizers/{base,tiktoken_counter}.py | removed the dead mlx.* override and the furl_ctx.tokenizers.* blanket override; fixed the 7 real Any-leaks (PIL dimension unpack, tiktoken encoding load) they were hiding with concrete types instead of suppression | #127 |
| 2026-07-19 | performance, cross-message dedup | furl_ctx/transforms/cross_message_dedup.py, tests/test_cross_message_dedup.py | bounded the near-dup reference window (`array_sources`) to the most recent 64 kept-verbatim arrays, eliminating O(n^2) scan cost and unbounded memory growth on long conversations; 3200-message repro went 5444ms -> 1002ms with per-message cost flat instead of climbing | TBD |

## Open candidates, fair game for future sessions

- Capture the committed benchmark baseline on Linux CI instead of macOS so the
  perf gate compares same-OS numbers. Review finding F3 on PR 120: recall has
  a knife-edge regime at 0.2222 where a single cross-OS trial flip would false
  fail. Empirically green today; structural fix is a CI-captured baseline.
- Correctness audit of bare `except Exception:` blocks (6 files: router_engine,
  pipeline, code_aware_compressor, tokenizers/base, cli, ccr/mcp_server) to
  confirm each is a deliberate fail-open boundary and not silent error
  swallowing. Needs per-site triage before any diff, so it is its own session.
- Test-quality iteration over live modules per the phase-4 hardening plan:
  boundary coverage, red-proof rigor, fewer mock-heavy tests. 2026-07-19
  survey turned up concrete targets: `furl_ctx/transforms/router_blocks.py`
  (589 lines, owns the content-block walk extracted from content_router's
  god-object) and `furl_ctx/transforms/compressor_registry.py` (151 lines)
  have zero references anywhere under `tests/`; `furl_ctx/relevance/bm25.py`
  (243 lines, on the retrieval hot path) has exactly one shallow integration
  test (`tests/test_ccr.py::test_search_with_bm25`) and no direct unit tests
  for empty query/corpus or the `avgdl == 0` division edge case;
  `router_dispatch.py` and `router_message_policy.py` sit at 1-2 test-file
  references versus 6-7 for comparably-sized siblings.
- Benchmark corpus growth: add a new real-world dataset family to
  benchmarks/datasets.py with provenance notes.
- CI timing telemetry on main pushes, warn-only trend line, design sketched in
  the perf gate PR discussion.
- mypy strictness: furl_ctx.ccr.mcp_server still carries a blanket
  disallow_untyped_defs override (2773 lines, the module the tokenizers.*
  override's sibling covered). Tightening it is real value but is its own
  session: much larger surface than tokenizers/ was, expect a real annotation
  effort, not a quick pass.
- Dependency health: `Cargo.lock` carries 3 versions of `hashbrown` (0.14.5
  via dashmap, 0.15.5 via wasmparser, 0.17.0 via indexmap/serde_json) and 2
  of `getrandom` (0.3.4, 0.4.2). Cargo.toml already carries comments about
  active wheel-size pressure (hit PyPI's 10GB/project ceiling at v0.21.36),
  so converging these transitive versions (a `dashmap` bump may pull its
  hashbrown pin to 0.15.x) is worth a dedicated bump-and-recheck pass.

Removed (already satisfied): "Property-based tests for the tabling grammar
round-trip, encode then decode equals identity" — verified 2026-07-19 that
`tests/test_csv_schema_decoder_roundtrip_fuzz.py` (1067 lines, present since
2026-07-12) already covers exactly this: 200+ seeded adversarial fuzz cases,
COR-13/COR-15 shape-coverage gates, and encode-then-decode identity
assertions throughout. No further action needed; keeping the stale entry
would only cost a future session the time to rediscover this.

## Notes for the maintainer

- PR #127's `github-advanced-security` check failed on every push
  (73ddb4a, 7be8d4b, c3f6895) with the same cause, unrelated to the diff:
  GitHub's own Copilot-based PR review backend threw
  `SessionModelError: ... "model_not_supported" ... model: claude-opus-4.6`
  before it read any code. This is not one of the three required checks
  (`lint`, `build-wheel`, `test`, per ruleset 18484290 and
  `tests/test_ci_required_checks_guard.py`) — it's a GitHub Advanced
  Security / Copilot platform feature outside `ci.yml` entirely. Left as
  red; nothing in this repo can fix a 400 from GitHub's model routing.
