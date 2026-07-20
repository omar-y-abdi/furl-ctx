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
| 2026-07-20 | correctness, tokenizer proxy honesty (T10 pre-mortem audit) | furl_ctx/tokenizers/{registry,estimator,tiktoken_counter}.py, tests/test_tokenizers.py, README.md | claude-* silently resolved through tiktoken o200k_base (byte-identical to gpt-4o, since Anthropic's own tokenizer is not public) with no proxy label anywhere a caller would see it, so compress()'s default model showed OpenAI token counts as if they were Anthropic billing tokens; Gemini/Cohere's fixed 4.0 chars-per-token estimator had no accuracy warning either. `TiktokenCounter`/`EstimatingTokenCounter` now carry a typed `proxy_for` attribute plus a documented, non-fabricated error band in module-level NOTE constants (Anthropic's own tiktoken-undercounts-Claude guidance for the o200k proxy: ~15-20% on typical text, more on code/non-English; ~2x reproduced directly against this project's own tokenizer for the fixed-ratio estimator, worse for CJK/JSON, closer for English/code, both now assertion-pinned). No per-construction log: an adversarial review found the first cut's `logger.warning` fired on every `get_tokenizer` call because Furl's own hooks spawn a fresh subprocess per tool call, so the module-level cache that would have deduped it starts empty every time (~100 stderr writes over a 50-call session); removed in favor of the caller reading `proxy_for` at whatever cadence its own layer can dedupe. Token counts are unchanged, compare_baseline shows 0 regressions and verify.run's degradations=6 matches the pre-existing baseline (see #132's row); only labeling changed. `anthropic` package confirmed not installed in this env via `pip show`; the calibration test pins the current proxy contract and documents that limit instead of fabricating a real Anthropic count | #137 |
| 2026-07-20 | correctness, dup-count display honesty, audit T5 | crates/furl-core smart_crusher/route.rs, plugins/furl skills/furl/SKILL.md, tests/test_dup_count_varies_sentinel.py | annotate_dup_counts stamped _dup_count:N on a kept representative that still showed row-0's concrete identity values, so a collapsed audit trail, heartbeat, or retry log read as one id or timestamp recurring N times when N distinct ids each occurred once; the representative now renders each excluded identity column that varies across the collapsed family as a `<varies>` sentinel while a column constant within the family keeps its real value, and _dup_count plus what gets dropped or recovered stay unchanged because persist_dropped hashes the original items not the annotated survivors; documented _dup_count and the `<varies>` sentinel in the model-facing SKILL.md where they appeared in no legend; verify.run silent_loss and hash_failures stayed 0 and the benchmark baseline was unchanged | #136 |
| 2026-07-20 | retention, per-namespace CCR spill, audit T6 | furl_ctx/cache/compression_store.py, plugins/furl/.mcp.json, plugins/furl/hooks/{compress_tool_output,pipe_compress,pretool_pipe}.py, plugins/furl/{README.md,skills/furl/SKILL.md}, tests/test_ccr_spill_plugin_namespace_gap.py | `_build_namespace_store` now wires a per-namespace durable spill gated by `FURL_CCR_SPILL`, so a capacity-evicted entry is demoted to the namespace's own `ccr-ns-<digest>-spill.sqlite3` instead of dropped at the 1000-entry cap and stays retrievable past eviction; per-namespace and `-spill` suffixed so no tenant reads another's rows, deliberately skipping the global sqlite-primary guard that would otherwise leave the sqlite-backed plugin with no spill at all; the plugin now sets `FURL_CCR_SPILL` in `.mcp.json` and the hooks so retention is real, and the #134 gap pin flips to a spill HIT alongside an isolation test proving two namespaces spill to different 0600 files under a 0700 dir | #138 |
| 2026-07-20 | security, MCP regex-filter ReDoS (T11 pre-mortem audit) | furl_ctx/ccr/mcp_server.py, furl_ctx/ccr/regex_budget.py, tests/test_mcp_server_handlers.py, tests/test_regex_budget.py | furl_retrieve's `pattern` and furl_compress's `include_patterns`/`exclude_patterns` are matched off the event loop (run_in_executor/asyncio.to_thread), where no SIGALRM watchdog can ever arm; without RE2 they fell back to unbounded stdlib `re`, so a crafted pattern could freeze every session on the process, with only a stderr startup warning (F-alpha2) as defense, a line a stdio host's operator may never see; both handlers now refuse the call with a caller-visible structured error before ever dispatching to a worker thread when RE2 is unimportable, closing the residual regex_budget.py already documented for this exact path; the mcp extra's existing `furl-ctx[re2]` hard dependency is now pinned by a regression test so it cannot silently regress back to the vulnerable default; the real RED/GREEN discriminator is the refusal envelope, not the small fast payload used in the repro and RED proofs, `(a|b|ab)+Z` measured 0.036s/0.126s/0.498s/2.006s/8.257s at 32/36/40/44/48 chars (roughly 3.5x-4.1x per 4 extra chars), so within the module's 10,000-char cap the unbounded cost is not minutes, it is longer than any operator would wait, a de facto permanent GIL-holding freeze | #139 |
| 2026-07-20 | test rigor, relevance/bm25 | tests/test_bm25_scorer.py | added 15 direct unit tests (tokenization boundaries, IDF known-values, hand-derived exact-score pins, the long-match bonus threshold/order-of-operations, normalization clamp, matched_terms cap) for `BM25Scorer`, previously covered only by one loose integration assertion despite sitting on `CompressionStore`'s search/search_all hot path; red-proofed by mutating the bonus threshold 8->3 chars, which left the full 2481-test suite green and 3 of the new tests red, restored with no production diff | #135 |
| 2026-07-21 | correctness, ccr marker tail guard, PR #131 review finding 3 | crates/furl-core/src/ccr/markers.rs, tests/test_resolve_markers_roundtrip.py | `marker_for_opaque` now replaces any `>` in the opaque KIND label with `_` before it enters the double-angle wire format; the single Rust construction point for shape C, so the guard covers the walker's live substitution and the CSV/KV formatter alike, for every kind present or future, not only the currently-unreachable `OpaqueKind::Other` path; empirically verified both failure modes first: a lone `>` in the tail leaves resolve_markers's `[^>]{0,64}>>` scan unmatched and the marker unresolved, but a `>>` pair aligns with its own terminator and truncates the substitution mid-marker, corrupting the recovered content; `kind` is a display-only hint resolve_markers never captures back out of the marker text, so replacing costs no round-tripped data and the function stays total; red proofed by temporarily reverting the guard and confirming the pinning test failed on the identical assertion before restoring it; two new consumer-side tests pin the exact boundary a regression on either side of the guard would fall back to | #140 |
| 2026-07-21 | CI hardening, mcp extra gap | .github/workflows/ci.yml | test job's wheel install now adds the `mcp` extra and `google-re2` alongside `dev`, so every test gated by `pytest.importorskip("mcp")` or `skipif(not re2_available())` actually runs in CI instead of silently skipping; confirmed the gap against a real run first, CI run 29731339248 summed 2188 passed/136 skipped across its four shards before this fix, this PR's own CI run summed 2598 passed/17 skipped after, 0 failed either time, the 17 residual skips are the pre-existing `[code]` extra gate plus one machine-specific empirical pin unrelated to mcp/re2; `google-re2` is technically redundant for this specific wheel-based install, a `pip install --dry-run` against a real built wheel confirmed the mcp extra's self-referential `furl-ctx[re2]` resolves it transitively already, unlike `maturin develop --extras` which does not, but it is kept explicit to match the local gate's own install command exactly | #140 |

## Open candidates, fair game for future sessions

- Audit `classify_field` / `compute_exclude_set` in
  `crates/furl-core/src/transforms/smart_crusher/field_role.rs` for
  over-exclusion: some high-cardinality or hex CONTENT columns are ruled
  `VaryingIdentity` and dropped from the stable-projection hash, the
  pre-existing cause of the audit's `_dup_count:390` on a unique HTTP row.
  The T5 display fix in PR #136 now paints such columns `<varies>`, which can
  make an inflated count read a little more plausible, though the data stays
  CCR-recoverable. The audit tiered this non-critical; worth a dedicated look
  at the classifier thresholds and shape tests in a future session.
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
  have zero references anywhere under `tests/`; `router_dispatch.py` and
  `router_message_policy.py` sit at 1-2 test-file references versus 6-7 for
  comparably-sized siblings. (`furl_ctx/relevance/bm25.py` was the same kind
  of gap and is now covered as of #135; the three remaining router modules
  above are still open.)
- 2026-07-20 finding while writing `BM25Scorer` tests (#135), not fixed
  there because both are dead code, not a testable behavior: the
  `avgdl = avg_doc_len or doc_len or 1` fallback in `_bm25_score` can never
  reach its trailing `or 1` — by the time that line executes, `doc_tokens`
  has already passed the `if not doc_tokens: return` guard above it, so
  `doc_len` is always >= 1. Likewise `_compute_idf`'s `doc_freq <= 0: return
  0.0` guard is unreachable from `score_batch`, the only caller, because its
  `idf_map` comprehension only includes terms already confirmed present in
  `doc_freq_across`. Both are honest defensive guards for direct callers of
  these "private" methods, not proven-dead in the non-negotiable-4 sense (no
  repo-wide reference grep run, no removal proposed) — flagging for a future
  simplification pass to either delete them with that proof or make the
  intent (defends direct API use, not reachable via `score_batch`) explicit
  in a comment.
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
