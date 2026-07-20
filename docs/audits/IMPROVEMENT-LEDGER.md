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
| 2026-07-19 | type strength, mypy override, ccr/mcp_server | pyproject.toml, furl_ctx/ccr/mcp_server.py | removed the last remaining disallow_untyped_defs override, proven dead (`mypy furl_ctx` passes with 0 errors without it); fixed the 7 real Any-leaks it was hiding (store/backend/entry/result signatures) with the concrete domain types (CompressionStore, CompressionStoreBackend, CompressionEntry, TextContent) already defined elsewhere in the codebase | #129 |
| 2026-07-19 | performance, cross-message dedup | furl_ctx/transforms/cross_message_dedup.py, tests/test_cross_message_dedup.py | bounded the near-dup reference window (`array_sources`) to the most recent 64 kept-verbatim arrays, eliminating O(n^2) scan cost and unbounded memory growth on long conversations; 3200-message repro went 5444ms -> 1002ms with per-message cost flat instead of climbing | #128 |
| 2026-07-19 | test rigor, faA2 hardening follow-up | tests/test_hook_audit_fixes.py | portable anchored pgrep poll restored macOS coverage and a returncode assert replaced the vacuous SIGINT pin | #130 |
| 2026-07-19 | correctness, CCR recovery-key width and store collision guard, audit T3 | crates/furl-core ccr persist/mod/in_memory and smart_crusher persist/walker/compactor, furl_ctx/transforms/smart_crusher.py, furl_ctx/ccr/marker_grammar.py | widened the 48-bit 12-hex crusher recovery key to 96-bit 24-hex so a birthday collision no longer lets one dropped row silently recover as another row's content; InMemoryCcrStore now drops the binding loudly on same-key different-payload instead of silently overwriting; the Rust to Python mirror hash-verifies fetched bytes before storing; 12-hex legacy markers still resolve | #133 |
| 2026-07-19 | correctness, ccr marker resolution (T4 pre-mortem audit) | furl_ctx/retrieve.py, furl_ctx/ccr/marker_grammar.py, tests/test_resolve_markers_roundtrip.py | resolve_markers substituted only the marker head for the double-angle `<<ccr:...>>` family, leaving a descriptive tail (e.g. `_rows_offloaded>>`) glued onto recovered content and breaking JSON round trips on all 6 double-angle sub-shapes; new DOUBLE_ANGLE_FULL_PATTERN + substitution_patterns fix the span, bracket family (G/H) confirmed unaffected and pinned; tail bounded to `[^>]{0,64}` after review found the unbounded pattern reintroduced O(n squared) ReDoS on the public resolve_markers API | #131 |
| 2026-07-19 | compression correctness, lossless type and byte fidelity | crates/furl-core compaction (compactor, formatter, ir), furl_ctx/transforms/csv_schema_decoder.py | fixed three silent value-corruption defects the reference decoder reconstructed to the wrong value with no CCR marker (audit T1/T2/T12): T1 mixed-type columns rendered a string bare so "200"/"true"/"null" decoded as int/bool/None, now CSV-quoted in json columns or declined to CCR when a container-string can't be disambiguated; T2 stringified-JSON fields were deserialized and object-strings flattened into dotted columns so the original vanished, now kept as verbatim string bytes; T12 a literal dotted key colliding with a synthesized flatten name silently overwrote a value, now the flatten is skipped and the decoder fails loud on duplicate columns. verify.run counters (degradations=6, silent_loss=0) and the benchmark baseline unchanged | #132 |
| 2026-07-19 | security, retention honesty, hook version gating | plugins/furl/hooks/{pipe_compress,compress_tool_output,_furl_ccr_counters,session_start_banner}.py, furl_ctx/host_version.py (new), furl_ctx/ccr/mcp_server.py | pipe path now redacts built-in credential patterns (was env-only, a true no-op with nothing configured); plugin retention docs corrected to name the 1000-entry cap instead of a plain 24h claim, the audit's FURL_CCR_SPILL=1 flip withheld after proving it is a no-op for the namespaced store the plugin actually uses; PostToolUse compression claims now check the running Claude Code version via a new detector and stop overclaiming below 2.1.163 | #134 |

## Open candidates, fair game for future sessions

- Guard the double-angle marker tail (`DOUBLE_ANGLE_FULL_PATTERN` in
  `furl_ctx/ccr/marker_grammar.py`) against a `">"` ever entering it. PR #131
  review finding 3: the tail is bounded to `[^>]{0,64}` on the assumption
  that no real double-angle producer ever emits a `">"` inside a marker's
  tail, verified true today for every shape (A-F), but shape C's `kind`
  field is `OpaqueKind::wire_str()`, and its `Other(String)` variant is
  currently unreachable in production (only a `#[cfg(test)]` fixture
  constructs it, `crates/furl-core/src/transforms/smart_crusher/compaction/
  ir.rs:593`) — a future producer that starts emitting `Other` for a
  classifier-detected format name is latent coupling debt: if that name
  ever contained a `">"`, the tail pattern would stop early and the marker
  would fail to resolve (fail-closed, not fail-open, so not a correctness
  risk today, but worth a proactive guard, e.g. asserting at construction
  time in the Rust producer that `Other`'s string never carries a `">"`).
  Not fixed in #131: no producer emits one today, so there was nothing to
  reproduce, and the PR's scope was the resolve_markers span/ReDoS bug.
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
- `furl_ctx/transforms/cross_message_dedup.py`'s `_DedupState.seen` (the
  exact-duplicate dict) is still unbounded: it retains a full copy of the
  original content string per distinct content-hash for the conversation's
  lifetime, with no cap analogous to the `array_sources` deque added
  2026-07-19. Unlike `array_sources`, `seen` lookups are already O(1)
  (a dict, not a linear scan), so this is a pure memory-retention concern,
  not a quadratic-cost one, and bounding it would change exact-dup
  matching semantics for very long conversations (an evicted hash's later
  exact duplicate would stop deduping) — worth a dedicated look rather than
  copying the array_sources fix verbatim.
- CI timing telemetry on main pushes, warn-only trend line, design sketched in
  the perf gate PR discussion.
- Dependency health: `Cargo.lock` carries 3 versions of `hashbrown` (0.14.5
  via dashmap, 0.15.5 via wasmparser, 0.17.0 via indexmap/serde_json) and 2
  of `getrandom` (0.3.4, 0.4.2). Cargo.toml already carries comments about
  active wheel-size pressure (hit PyPI's 10GB/project ceiling at v0.21.36),
  so converging these transitive versions (a `dashmap` bump may pull its
  hashbrown pin to 0.15.x) is worth a dedicated bump-and-recheck pass.
- T6 real fix, PR #134: `FURL_CCR_SPILL=1` only wires a spill tier into
  `get_compression_store`'s global singleton. The plugin never reaches that
  path since every hook and the MCP server set `FURL_CCR_PROJECT_DIR`, which
  routes through `resolve_ccr_namespace_store` -> `_build_namespace_store`
  instead, and that function never wires a spill backend regardless of the
  env var, on purpose, to avoid demoting every tenant into one shared file.
  Proven with `tests/test_ccr_spill_plugin_namespace_gap.py`. Real fix needs
  a per-namespace spill backend added to `_build_namespace_store` in
  `furl_ctx/cache/compression_store.py`. Out of PR #134's file scope and
  explicitly excluded from that batch's mandate, which forbade redesigning
  cap accounting; needs its own design pass on the cap and spill interaction.
- Residual COR-14 dotted-key ambiguity on a pathological uniform-nested shape
  such as `[{"a.b": {"c": i}, "a": {"b.c": i}}]`, surfaced by the PR #132
  adversarial review. Both branches flatten toward the same `a.b.c` dotted
  name; the T12 collision guard means no duplicate columns ship and both
  values are retained in dotted form, so the T12 goal holds and it is no worse
  than main (which drops one value). But the reconstruction is not value-exact
  under `verify/independent_recheck._unflatten_dotted` because the dotted names
  are ambiguous about the original nesting. A grammar-level record of the
  flatten, or a decline for the collision-shaped input, would make it exact.
  Deferred: its own session, not folded into the fidelity fix.
- CI test job installs only the `[dev]` extra, never `[mcp]`, so every test
  file gated by `pytest.importorskip("mcp")` (e.g.
  `tests/test_mcp_stats_version_gate.py`) is silently skipped in CI and gives
  zero signal there, positive or negative. Surfaced reading the PR #134 CI
  shard logs directly. The local armed gate installs `dev,mcp` so it covers
  them, but CI's required `test` check does not. Fix: add the `[mcp]` extra to
  the ci.yml test install, minding the required-check deadlock guard, so the
  mcp-gated suites actually run in CI. Its own small session since it touches
  ci.yml.

Removed (already satisfied): "Property-based tests for the tabling grammar
round-trip, encode then decode equals identity" — verified 2026-07-19 that
`tests/test_csv_schema_decoder_roundtrip_fuzz.py` (1067 lines, present since
2026-07-12) already covers exactly this: 200+ seeded adversarial fuzz cases,
COR-13/COR-15 shape-coverage gates, and encode-then-decode identity
assertions throughout. No further action needed; keeping the stale entry
would only cost a future session the time to rediscover this.

## Notes for the maintainer

- `github-advanced-security` fails on essentially every PR with the same
  platform-level cause, unrelated to any diff: GitHub's own Copilot-based
  PR review backend throws
  `SessionModelError: ... "model_not_supported" ... model: claude-opus-4.6`
  before it reads any code. Seen on PR #127 (73ddb4a, 7be8d4b, c3f6895) and
  again on PR #129 (9f3edf0, job 88168484648), both with the identical
  400 from `api.individual.githubcopilot.com`. This is not one of the
  three required checks (`lint`, `build-wheel`, `test`, per ruleset
  18484290 and `tests/test_ci_required_checks_guard.py`) — it's a GitHub
  Advanced Security / Copilot platform feature outside `ci.yml` entirely.
  Confirmed persistent across two separate PRs; left as red each time —
  nothing in this repo can fix a 400 from GitHub's own model routing.
