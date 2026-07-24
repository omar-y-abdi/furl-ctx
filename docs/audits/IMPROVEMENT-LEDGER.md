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
| 2026-07-21 | correctness, opaque code-offload economics honesty, T9 pre-mortem audit | furl_ctx/compress.py, furl_ctx/cache/compression_store.py, furl_ctx/ccr/mcp_server.py, furl_ctx/__init__.py, benchmarks/code_roundtrip.py (harness since removed; see git history), BENCHMARKS.md, README.md, tests/test_opaque_offload.py | the front-page `code` row marketed a raw marker reduction near 99 percent, but that content is an opaque whole-blob CCR offload with strategy `ccr_offload` and no granular row index, so an agent that needs the code must retrieve the entire payload back and the round trip is a net token LOSS; measured fresh on this machine with `python -m benchmarks.code_roundtrip` the committed `benchmarks/data/code.raw.json` fixture is raw 95.9 percent but effective -4.1 percent after one retrieval, reproducing the pre-mortem's -4.1 percent exactly. `compress()` now surfaces every opaque whole-blob offload as a typed `result.opaque_offloads` list carrying offloaded and preview token counts and a `net_negative_on_retrieval` flag, discriminated from cheap granular per-row drops by the store entry's `compression_strategy`; the MCP `furl_compress` response carries the same field plus one honest line of copy. Following the #137 lesson the signal is a structured field the caller reads at its own cadence, never a per-call log, because the hook spawns a fresh subprocess per tool call so per-call stderr would spam; `store.get_metadata` was extended additively with `compression_strategy` and token counts and the router was left untouched, so the change is purely additive on the compression path. BENCHMARKS.md gains a labeled `code` round-trip row distinguishing opaque whole-blob offload from granular offload, and the README code claim is reframed to say the headline percent is a marker reduction not a token saving. verify.run stayed degradations=6 hash_failures=0 silent_loss=0 cache_prefix_violations=0 and the full pytest suite passed; RED proofs documented in the PR | #141 |
| 2026-07-21 | test rigor, router_dispatch SMART_CRUSHER no-savings fallback | tests/test_router_dispatch_no_savings_fallback.py | `StrategyDispatcher.apply`'s post-dispatch fallback chain (the safety net that reverts an expanded SMART_CRUSHER result and decides whether a last-ditch LOG fallback is actually smaller before adopting it) had zero direct or indirect coverage; proved by independently disabling the expansion-revert arm, the log-adoption comparison, forcing unconditional log adoption, widening `<` to `<=` at the exact-equal boundary, and removing the `try/except` fail-open around the log compressor call — each of the 5 mutations left the full 2597-test suite 100% green; 5 new tests pin all 5 behaviors with a RED proof per mutation, each reproduced against `router_dispatch.py` then reverted; four of the five fail exactly one dedicated test, but the forced-unconditional-adoption mutation reddens two at once, both `test_log_fallback_is_not_adopted_when_not_smaller` and `test_log_fallback_is_not_adopted_when_exactly_equal`, since an unconditional adopt trips both boundary checks together, so the suite is more sensitive than this row first claimed, not less; no production code changed | #142 |
| 2026-07-21 | release CI hardening, safety-gate docs truth, release-please tag drift guard | .github/workflows/release.yml, .github/workflows/ci.yml, tests/test_release_manifest_tag_guard.py, plugins/furl/hooks/pretool_pipe.py | deadsnakes PPA add in the Ubuntu smoke-import step no longer swallows its own failure with `|| true`, now retries up to 3 times with a short pause and fails loud with a named error, the exact bug that broke the v1.3.0 release run on the x86 runner while the arm twin passed the same attempt, confirmed against the actual failed job log from run 29815802918 attempt 1, which read `E: Unable to locate package python3.12`; the PYPI_SKIP safety-gate comment no longer states a live value or a PyPI-ownership claim, both proven false against `gh variable get PYPI_SKIP`, which returns `false` and has since 2026-07-07, and live PyPI metadata, whose author and maintainer email match this repo's owner rather than an upstream author, comment now states only the variable's semantics; new tests/test_release_manifest_tag_guard.py asserts the release-please manifest version has a matching git tag, the exact drift that let 1.3.0 sit unshipped for weeks, skips rather than false-fails when a shallow checkout cannot see any tags at all, verified empirically that a depth-1 clone sees 0 of 167 tags and that `git fetch --tags` resolves all of them, so `fetch-tags: true` was added to ci.yml's `test` job checkout to arm the guard in the required check instead of it skipping every PR forever; pretool_pipe.py's docstring corrected since Claude Code >=2.1.163 is the floor that applies a shape-mirrored updatedToolOutput rather than dropping it, contradicting the file's own prior claim plus compress_tool_output.py and host_version.py in the same repo | #143 |
| 2026-07-21 | test rigor, dead-branch removal, drift pin; relevance/bm25 and compress | tests/test_bm25_scorer.py, tests/test_bm25_dead_branch_parity.py, tests/test_retrieve_overhead_drift_pin.py, furl_ctx/relevance/bm25.py | two verified adversarial reviews on the BM25 search path, every premise re-verified first. ITEM 1 de-fragilized `tests/test_bm25_scorer.py`: the private `_tokenize` UUID and digit-in-word assertions were re-pinned through the public `score_batch` and `matched_terms`, the UUID case now rejecting a single-hex-character variant; the circular `single == 0.4700036292457356` pin became the symbolic `math.log(1.6) * 2.5 / 2.5` the worked-corpus test uses, one ULP off from bare `log(1.6)`, with `double == single * 2` kept; two derived float-equality asserts became `math.isclose(abs_tol=1e-12)`; the private empty-string, delimiter, and both `_compute_idf` tests were kept as distinct-guard and pure-formula coverage. ITEM 2 removed two dead branches in `_bm25_score`, the `avgdl` trailing `or 1` and both `math.log(2.0)` idf fallbacks, tightening `avg_doc_len` and `idf_map` to required params, proven behavior-preserving by a committed 468-pair parity test plus a byte-identical before/after `score_batch` dump at the same sha256; the `\b\d{4,}\b` numeric-ID branch was re-examined and found NOT redundant, since `\d` matches non-ASCII Unicode digits the ASCII class drops, so it was kept and locked by Unicode-digit tests; the `_compute_idf` doc_freq<=0 guard was kept with its intent made explicit, resolving the 2026-07-20 open candidate below. ITEM 3 pinned `_CCR_RETRIEVE_OVERHEAD_TOKENS` equal to verify.measure's `RETRIEVE_CALL_OVERHEAD_TOKENS` via a test-side cross-import, guarding that the library imports no `verify` module. Every new pin red-proofed on a relevant mutation; full pytest suite green and verify.run stayed degradations=6 hash_failures=0 silent_loss=0 cache_prefix_violations=0 | #144 |
| 2026-07-21 | test rigor, release CI hardening, wave2 review sweep closure | .github/workflows/ci.yml, .github/workflows/release.yml, docs/audits/IMPROVEMENT-LEDGER.md, tests/test_bm25_dead_branch_parity.py, tests/test_bm25_scorer.py, tests/test_pretool_pipe.py, tests/test_release_manifest_tag_guard.py, tests/test_retrieve_overhead_drift_pin.py, tests/test_router_dispatch_no_savings_fallback.py | ten adversarial-review follow-ups closed in one pass, every premise re-verified against current code first: ci.yml's changes filter now covers both release-please manifest files so the tag guard test actually runs on manifest-only PRs; the manifest guard itself now rejects a silently added second package with a red-proofed assert; release.yml's PPA retry no longer sleeps after its last failed attempt; test_pretool_pipe.py's docstring now matches pretool_pipe.py's already-fixed premise; the PR #142 ledger row's mutation-sensitivity claim corrected, the forced-adoption mutation reddens two tests not one; test_router_dispatch_no_savings_fallback.py's caplog assert softened to a stable token plus logger and DEBUG level; test_bm25_dead_branch_parity.py gained a committed 468-pair golden snapshot pinning exact score bits, red-proofed against a temporary constant-IDF mutation that the pre-existing range check alone missed; test_bm25_scorer.py's UUID test now pins the uppercase-folds-to-lowercase path with an uppercase document literal; test_retrieve_overhead_drift_pin.py's docstring tightened to the AST scan's actual direct-absolute-import-only scope; and a new open candidate flags the faA2 tests' `uv run` pin against furl-ctx[mcp]==1.3.0, confirmed today against PyPI's JSON API as not yet published. One named item, a router-module undercount in the open-candidates text, was found already fixed by #142 itself and left untouched. pytest went from 2621 passed 16 skipped to 2622 passed 16 skipped; ruff check, ruff format --check, and mypy furl_ctx all clean | #146 |
| 2026-07-21 | release CI unblock, tag-drift guard release-PR escape hatch, cyclonedx SBOM repair | tests/test_release_manifest_tag_guard.py, .github/workflows/{ci,publish}.yml, CONTRIBUTING.md | release-please release PRs failed the manifest tag-drift guard forever, since the manifest bumps ahead of its tag until merge, so a FURL_RELEASE_PR_CONTEXT escape hatch armed by ci.yml only for release-please-- head branches via a case-insensitive startsWith now skips just the tag assertion there while the guard stays armed on every other branch and on main; a committed test forces an untagged manifest and asserts the real git tag path still bites; separately the manual PyPI fallback's cyclonedx SBOM step was repaired from the dropped --outfile flag to --output-file with cyclonedx-bom pinned to the 7 major range, verified empirically against 7.3.0; CONTRIBUTING gained the pretool_pipe.py pin in its hand-sync list plus the escape-hatch note | #147 |
| 2026-07-21 | release 1.3.2, embedded engine pin hand-sync | plugins/furl/hooks/{hooks.json,pretool_pipe.py,session_start_banner.py}, plugins/furl/.mcp.json, plugins/furl/skills/furl/SKILL.md, plugins/furl/README.md, tests/test_hook_audit_fixes.py | release-please opened the 1.3.2 release PR bumping pyproject.toml and .release-please-manifest.json, but it has no updater that can rewrite a version embedded inside a shell-command string or a prose example, so the embedded furl-ctx engine pins were hand-synced from 1.3.0 to 1.3.2 on the same branch: both command pins in hooks.json, the .mcp.json pin, the _FURL_CTX_PIN constant in pretool_pipe.py, the _ENGINE_VERSION constant in session_start_banner.py, the worked-example prose in SKILL.md and README.md, and the hardcoded engine half of the status-line assertion in test_hook_audit_fixes.py; test_plugin_version_pins.py goes green so the release can merge | #148 |
| 2026-07-22 | correctness, compression quality, field_role.rs entropy over-exclusion | crates/furl-core/src/transforms/smart_crusher/field_role.rs | `string_is_identity`'s shape-independent entropy fallback (`avg_entropy > 0.7`, no other evidence) misclassified almost any short single-word token as identity noise, since `calculate_string_entropy` normalizes by the token's OWN alphabet size, not a language model — a 96-token corpus of real filenames/identifiers from this repo scored 0.94-1.00 average normalized entropy, all but 1 above the threshold; a build-audit-log-shaped array with a near-unique `file` column and a constant `status` column collapsed all rows into one `_dup_count` representative, hiding which file each row actually touched, with only CCR (a full round-trip) able to recover the real value. Root cause proven with a Python entropy-formula reimplementation before touching Rust. Fix: `looks_non_linguistic` requires 80% of the sampled tokens to also contain a digit or have vowel-ratio < 0.10 before the entropy signal is trusted, corroboration thresholds chosen by an empirical sweep (0.10/0.12/0.15/0.18/0.20) against the repo corpus vs synthetic random hex/base62 tokens: 0.10 cut natural-token false positives from ~100% to 1/96 while still catching 285/300 random tokens (a genuine but disclosed recall cost, not silent). The narrowing is strictly one-directional — every field still classified `VaryingIdentity` after the fix would also have been before it — so the fix cannot newly collapse a distinct-content row; it can only stop over-collapsing. New `IDENTITY_NONLINGUISTIC_FRACTION` constant kept separate from the pre-existing `IDENTITY_SHAPE_FRACTION` (both 0.8 today) so tuning one never silently retunes the other, per an independent adversarial review pass. 4 new Rust tests (filename corpus stays Content, digit-bearing random tokens stay VaryingIdentity as a regression guard, `looks_non_linguistic` unit boundaries including the exact 0.10 threshold, and a `compute_exclude_set`-level integration test), all red-proofed by temporarily reverting the corroboration check and confirming exactly the 2 targeted tests fail. Known, disclosed, pre-existing (not worsened) scope limit: the vowel check is ASCII-only, so a single CJK/Cyrillic/diacritic-only token still misclassifies exactly as it did before this fix. Non-Latin-script coverage is left as an open candidate below. Full gate green: `cargo test --workspace` 836+17+5+3+5 passed 0 failed, `cargo clippy -D warnings` and `cargo fmt --check` clean, pytest 2582 passed/19 skipped (skips are pre-existing `[code]`-extra and shallow-clone-tags gaps, unrelated), `verify.run` unchanged at `degradations=6 hash_failures=0 silent_loss=0 cache_prefix_violations=0`, `compare_baseline` 0 regressions/0 improvements (the fix's benefit isn't exercised by the existing benchmark corpus, which is why a dedicated Rust-level repro was needed) | #150 |
| 2026-07-23 | test rigor, router_blocks ContentBlockWalker direct coverage | tests/test_router_blocks_walker.py | resolves the 2026-07-19 open-candidate gap for `furl_ctx/transforms/router_blocks.py` (558 lines, zero direct test references, the per-block cache-safety walk): planted 14 single-gate mutations one at a time and ran the FULL 2583-test suite under each as the Phase 2 artifact — 6 survived 100% green: forcing adoption of a store-gate-rejected compression, `<=` to `<` at the read-protection window edge, pinning per-tool bias to 1.0 across the whole block path, `>` to `>=` at the min_chars floor, ignoring the nested-path feedback keep-budget multiplier, and returning a fresh dict for an untouched message instead of the by-reference identity the engine's delta accounting keys on; the other 8 (block and nested cache_control guards, retrieval-tool age-out exemption, assistant role gate, string and nested error protection, nested CCR pinning, string-path multiplier) were caught by only 1-4 incidental integration tests each, so the ledger's "zero references" claim was refined, not fully confirmed. New tests/test_router_blocks_walker.py adds 38 direct unit tests driving `process_content_blocks` with explicit fake injected callables (no router instance, no monkeypatching); red-proofed against all 14 mutations — every mutation reddens the new file, 13 of 14 redden exactly the targeted test(s), and the identity mutation reddens 23 at once since every by-reference assertion trips. No production code changed; verify.run counters and benchmark baseline untouched by construction, confirmed by the full gate | #156 |
| 2026-07-23 | correctness, COR-14 flatten ambiguity, csv-schema lossless tier | crates/furl-core compaction/compactor.rs, furl_ctx/transforms/csv_schema_decoder.py (docs), tests/test_csv_schema_decoder_roundtrip_fuzz.py | resolves the open "Residual COR-14 dotted-key ambiguity" candidate below via the decline option, and proves the family is six shapes wide, not one: `flatten_uniform_nested` synthesized dotted column names the wire grammar never records, and for a dotted INNER key (`{"a": {"b.c": 900}}` beside a sibling `a.b`, object or scalar), prefix-overlapping inners (`{"b", "b.c"}`), empty dot-segments (`""`, `"b."`), or an empty parent name (`{"": {"k": 1}}` shipping a `.k` column), the decoded rows bound values to the WRONG paths under the documented dotted-key equivalence — silently, lossless-tier claim intact, no CCR sentinel, and provably non-injective (two distinct originals produce identical decoded rows). Live-pipeline repro before any fix: original `a→b→c=900` with literal `"a.b"={"c":0}` decoded as `a→b→c=0` with literal `a.b.c=900`, values swapping owners. New `flatten_breaks_dotted_equivalence` guard declines exactly the proven-unsafe boundary (empty parent, empty inner segment, prefix-overlapping inners, sibling on a strict prefix of a synthesized path); a dot-free parent with dot-free inners is proven safe regardless of siblings and untouched; extension (`a.b.c.d`) and disjoint (`a.j`) siblings plus metrics-style dotted inners (`cpu.usage`) keep flattening, pinned in both Rust (7 new tests, 836→843) and Python (4 new tests, 2583→2587, both crusher zones n=14/n=9). RED-proofed against the final test text with only the guard disabled: exactly the 3 guard tests fail via `_assert_decoded_exact_or_declined`'s own COR-13 byte-exact assertion, benign pin stays green. Dotted PARENTS were probed and found already safe (pre-existing `uniform_object_keys` dot guard). Full gate green; verify.run unchanged at `degradations=6 hash_failures=0 silent_loss=0 cache_prefix_violations=0`; compare_baseline 0 regressions 0 improvements (no benchmark corpus contains ambiguity shapes, which is why the defect never tripped verify.run and needed dedicated repros). Deliberately not done: the grammar-level flatten record that would keep dotted compression on ambiguity shapes — noted in the candidate's resolution annotation | #157 |
| 2026-07-23 | compression correctness, HTML extraction boundary fusion | furl_ctx/transforms/html_ingest.py, tests/test_html_ingest.py | `_MainContentExtractor` dropped every whitespace-only `handle_data` event and joined the surviving parts with `""`, and neither table cells nor block-tag CLOSE boundaries emitted any separator, so the extracted view `compress_html` ships to the model fused adjacent text across boundaries the source genuinely had: `<td>5</td><td>3</td>` extracted as `"53"` (the model reads the number 53, not two cells), `<th>Name</th><th>Age</th>` as `"NameAge"`, `<span>Hello</span>\n <span>World</span>` as `"HelloWorld"`, and `<div>alpha</div>beta` as `"alphabeta"` — all reproduced live pre-fix. Lossy-but-recoverable (the raw HTML is CCR-stored byte-exact), so no hard loss, but the PRIMARY view the model consumes was semantically corrupted for every HTML table, worst for numeric cells. Fix in the extractor only: whitespace-only data events collapse to one `" "` instead of vanishing (browser whitespace semantics; also covers `&nbsp;` via `convert_charrefs`), new `_CELL_TAGS` (`td`/`th`) emit `" "` at open, and `_BLOCK_TAGS` now emit `"\n"` at CLOSE as well as open; `text()`'s per-line collapse plus empty-line filter absorbs any redundant separators, so block-structured extractions are unchanged (pinned). Deliberately-adjacent inline runs with NO source boundary (`<b>10</b><b>USD</b>` renders "10USD" in a browser) still fuse, pinned as correct. 6 new tests: 4 red pre-fix on exactly the defect shapes, 2 guard tests green pre- and post-fix proving separators are only reintroduced where the source had a boundary. Full gate green: pytest 2625→2631 passed/19 skipped, cargo 843+17+5+3+5, clippy/fmt/ruff/mypy clean, verify.run unchanged at `degradations=6 hash_failures=0 silent_loss=0 cache_prefix_violations=0`, compare_baseline 0 regressions 0 improvements (no benchmark corpus routes HTML). Phase 0 note: the session env's `_core.abi3.so` was again stale (Jul 19 build vs 5 later Rust-touching merges) and was rebuilt via `maturin develop --release` BEFORE baselining, per the 2026-07-21 maintainer note | #158 |
| 2026-07-23 | CI hardening, docs truth, plugin version-pin guard gap | tests/test_plugin_version_pins.py, CONTRIBUTING.md | resolves the 2026-07-21 open candidate below: SECURITY.md's supply-chain section carries two `furl-ctx[mcp]==X.Y.Z` prose pins that neither `tests/test_plugin_version_pins.py` nor CONTRIBUTING.md's "Releasing / version bumps" hand-sync list covered, the exact blind spot that nearly let the 1.3.2 release ship stale 1.3.0 pins in SECURITY.md until a manual grep caught it. Proof before the fix: temporarily drifted both SECURITY.md pins to `9.9.9` and ran the full pin-guard suite (and separately the full pytest suite) — 100% green, confirming the drift was invisible end to end. Fix: a new `test_security_md_prose_pins_match_pyproject_version` test (mirrors the existing SKILL.md/README.md prose-pin tests, reusing the same `_pins_in`/regex helpers) plus a `SECURITY.md` line item added to CONTRIBUTING's hand-sync list and the module docstring's file inventory. Red-proofed on the identical `9.9.9` mutation post-fix: the new test alone failed with an exact-diff assertion (`['9.9.9', '9.9.9'] != '1.3.2'`), then reverted and reconfirmed 11/11 green. Full gate: `cargo test --workspace` (30 tests) and `cargo clippy -D warnings`/`cargo fmt --check` clean (no Rust touched); `ruff check`/`ruff format --check` clean; `mypy furl_ctx` 0 errors; `pytest tests/ -q` 2583 passed, 19 skipped (pre-existing, unrelated — the Rust extension was rebuilt via `maturin develop --release` first, since it was stale relative to HEAD exactly as the 2026-07-21 session note warned a future session to check); `verify.run` unchanged at `default_params_confirmed=True degradations=6 hash_failures=0 silent_loss=0 cache_prefix_violations=0`; `compare_baseline` 0 regressions/0 improvements (test-and-docs-only change, no compression path touched). Runner-up candidates from this session's Phase 1 recorded below | #155 |
| 2026-07-24 | correctness, MIXED-path fictitious fence language | furl_ctx/transforms/router_split.py, furl_ctx/transforms/router_engine.py, tests/test_router_split_bare_fence_language.py | resolves the 2026-07-23 open candidate below: a bare ` ``` ` fence recorded `language = match.group(1) or "unknown"`, and `_compress_mixed`'s reassembly guard `if section.is_code_fence and section.language:` was then unconditionally true (`"unknown"` is truthy), so once any section in a MIXED-routed message changed, the reassembled output shipped a fabricated ```` ```unknown ```` tag the source never contained. Reproduced live pre-fix via `ContentRouter().compress()` on a bare fence beside an 80-row compressible JSON array. Fix: `split_into_sections` now records `""` for a bare fence (never `"unknown"`), and the reassembly guard fires unconditionally on `section.is_code_fence`, reassembling `f"```{section.language or ''}\n...\n```"` — an empty language reproduces the original bare fence exactly, a real tag (e.g. `python`) is unaffected. Traced the one downstream consumer of the hint, `code_aware_compressor._resolve_language`: it already documents "code-fence tags like `bash` or `unknown`" falling back to auto-detection, and its own `CodeLanguage.UNKNOWN` enum member meant `"unknown"` was excluded from the early-return anyway, so this opt-in path's behavior is unchanged before/after, confirmed by reading the code rather than assumed. 5 new tests in `tests/test_router_split_bare_fence_language.py` (split-level empty-vs-tagged language, `_compress_mixed`-level reassembly for both cases, and the live end-to-end repro); red-proofed by reverting both production lines and confirming exactly the 3 bug-covering tests fail (the 2 tagged-fence tests, unaffected by the bug, stay green), then restored. Full gate green: pytest 2631→2636 passed/19 skipped (no Rust touched, so cargo test/clippy/fmt unaffected by the diff but re-run clean regardless); `verify.run` unchanged at `degradations=6 hash_failures=0 silent_loss=0 cache_prefix_violations=0`; `compare_baseline` 0 regressions/0 improvements (no benchmark corpus routes a bare fence through MIXED). Deliberately not done, per the candidate's own scoping note: the `"\n\n".join` blank-line normalization and the synthesized closing fence for an unterminated code block remain separately lossy-but-recoverable (COR-30 already returns verbatim passthrough when no section changes); whether that reassembly path deserves its own whole-content CCR marker is left as a follow-up, not folded into this fix. Phase 0 note: the session's `_core.abi3.so` was again stale (Jul 18 build vs 4 later Rust-touching merges through #157/#158) and was rebuilt via `maturin develop --release --extras dev,mcp` before baselining | #159 |

## Open candidates, fair game for future sessions

- 2026-07-23 (HTML session runner-up, verified live): the MIXED path
  injects a fictitious fence language with no recovery marker.
  `router_split.py:90` stores `language = match.group(1) or "unknown"`, so a
  bare ``` fence records `"unknown"`; `router_engine.py`'s `_compress_mixed`
  reassembly guard `if section.is_code_fence and section.language:` is then
  ALWAYS truthy and rewrites the fence as ```` ```unknown ````, shipping an
  identifier the input never contained. Unlike the pure HTML/CSV/envelope
  paths, MIXED writes no whole-content CCR marker (its COR-30 guard only
  covers the no-section-changed case), so once any section compresses the
  mutation is unrecoverable; the `"\n\n".join` blank-line normalization and
  the synthesized closing fence for an unterminated block are the same
  unrecoverable-mutation class. Reproduced end to end through
  `ContentRouter().compress` (bare fence + 80-row JSON array: output carried
  ```` ```unknown ````). Candidate fix: store `language=""` for a bare fence
  and reassemble `f"```{section.language}\n..."` unconditionally for fences
  (empty string yields the original bare fence), then decide whether the
  join normalization deserves a whole-content marker.
  Resolved 2026-07-24 exactly via the candidate fix (see the Completed row):
  the fabricated `"unknown"` tag is gone, both production sites fixed, 5
  new tests red-proofed. The join-normalization / unterminated-fence-marker
  question was deliberately left open, not folded into this fix — still a
  candidate for a future session.
- 2026-07-23 (log_template deep audit, found clean, do not re-audit
  blindly): a dedicated adversarial pass over `log_template.py`,
  `log_template_miner.py`, `log_template_decoder.py`, and
  `log_template_format.py` (escape closure, wildcard sentinel collisions,
  ~52k encode round-trips over adversarial corpora seeded with `<*>`, `\w`,
  `\p`, backslashes, astral chars; 120k mutated wires through `decode`)
  found NO losslessness, crash, or round-trip defect: production uses only
  `encode_verified`, whose independent-path decode-and-compare makes any
  encoder bug degrade to declined compression, never silent corruption. One
  low-severity note survives: `content_and_terminators` recognizes only
  `\n`/`\r\n`/`\r`, while `test_stats_partition_all_lines` asserts equality
  against `str.splitlines()`, which also breaks on `\v \f \x1c \x1d \x1e
  \x85 \u2028 \u2029` — content still round-trips byte-exact (verified),
  but a real log carrying e.g. `\x85` makes that TEST spuriously fail and
  the informational `templated_lines`/`verbatim_lines` stats diverge from
  `splitlines()` semantics. Tiny, test-fragility-only fix if picked up.
- Audit `classify_field` / `compute_exclude_set` in
  `crates/furl-core/src/transforms/smart_crusher/field_role.rs` for
  remaining over-exclusion in the SHAPE-based paths (`is_hex_run`,
  `is_uuid_format`, `is_iso_date`/`is_iso_datetime`): a genuinely
  informative 8+ hex-digit CONTENT column (e.g. a CRC32/checksum column
  that is itself the point of the row, not noise) still shape-matches and
  gets excluded, the pre-existing cause of the audit's `_dup_count:390` on
  a unique HTTP row. PR #150 (2026-07-22) fixed the SEPARATE
  shape-independent entropy-only fallback (natural short words/filenames
  wrongly caught by raw entropy with no shape match at all) but
  deliberately left the shape-matched paths untouched — shape matches are
  higher-confidence signal than raw entropy, and disambiguating "hash as
  noise" from "hash as the value under inspection" needs semantic context
  (e.g. a paired expected/actual column) this module doesn't have. The T5
  display fix in PR #136 now paints such columns `<varies>`, which can make
  an inflated count read a little more plausible, though the data stays
  CCR-recoverable. The audit tiered this non-critical; worth a dedicated
  look at the shape-path thresholds in a future session.
- `field_role.rs`'s `looks_non_linguistic` (added in PR #150) only
  recognizes ASCII vowels/digits, so a single-token CJK/Cyrillic/etc. field,
  or a Latin word whose only vowels are diacritic (French `déjà`), still
  reads as non-linguistic and can still be wrongly excluded via the entropy
  fallback exactly as it could before #150 — not a regression, but a
  disclosed gap in the fix's proven scope. Extending vowel/script detection
  to non-ASCII natural language needs its own corpus and sweep (this
  session's sweep corpus was ASCII/English-only) rather than guessing at
  Unicode vowel sets.
- Dependency health follow-up from this session's exploration (not chosen,
  see below): `cargo tree -d` shows `hashbrown` at 2 resolved versions
  (0.14.5 via `dashmap` 6.2.1 production dep, 0.17.0 via `indexmap`/
  `serde_json` production+dev) and `getrandom` at 2 (0.3.4 via the
  `proptest`/`rand` dev-only chain, 0.4.2 via `tempfile` dev-only).
  `dashmap`'s latest is `7.0.0-rc2` (pre-release) and its current 6.2.1 is
  the newest stable; bumping to the rc to converge hashbrown is a bigger,
  riskier call than a same-day drive-by. `Cargo.lock` also carries a
  `wasmparser` 0.244.0 entry that `cargo tree -i wasmparser` shows as
  unreachable from furl-core's resolved graph on this platform/target —
  worth confirming whether that's an expected multi-target lockfile
  artifact or a genuinely prunable stale entry, needs `cargo update`
  reasoning a same-day session couldn't responsibly rush.
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
  of gap and is now covered as of #135; `router_dispatch.py`'s SMART_CRUSHER
  no-savings fallback chain specifically — the expansion-revert and the
  log-fallback adoption comparison, both boundary cases — is now covered by
  the mutation-proofed tests added in this session, see the row below.
  `router_blocks.py` is now covered as of the 2026-07-23 ContentBlockWalker
  session, see that row: 38 direct tests, 14 mutations red-proofed.
  `router_dispatch.py`'s other branches, `compressor_registry.py`, and
  `router_message_policy.py` remain open — `router_message_policy.py` is
  the strongest remaining target, since the walker session's sweep showed
  its helpers (`_is_retrieval_tool`, `_is_unstructured_error_output`,
  `_looks_like_ccr_output`) are load-bearing for several thinly-pinned
  gates.)
  Also surfaced while mutation-testing this file this session but not
  chased down: forcing `router_dispatch.py`'s envelope-ingestion
  `suppress_no_savings_fallback` flag to `False` (the sibling of the
  tabular-CSV flag two lines below it, which the existing
  `test_csv_schema_decoder_roundtrip_fuzz.py`/`test_lossless_fidelity.py`
  suite already catches) made one `pytest` run stall past a 2-minute
  timeout on a rerun that otherwise takes ~70 seconds; a second, careful
  rerun of an unrelated mutation nearby completed normally, so this reads
  as environment flakiness rather than a reproduced hang, but it was never
  confirmed innocent either. Needs a dedicated, isolated repro (single test
  file, `-p no:randomly` if applicable, resource monitoring) before either
  writing a regression test or dismissing it.
- 2026-07-20 finding while writing `BM25Scorer` tests (#135), not fixed
  there because both are dead code, not a testable behavior: the
  `avgdl = avg_doc_len or doc_len or 1` fallback in `_bm25_score` can never
  reach its trailing `or 1` — by the time that line executes, `doc_tokens`
  has already passed the `if not doc_tokens: return` guard above it, so
  `doc_len` is always >= 1. Likewise `_compute_idf`'s `doc_freq <= 0: return
  0.0` guard is unreachable from `score_batch`, the only caller, because its
  `idf_map` comprehension only includes terms already confirmed present in
  `doc_freq_across`. Both are honest defensive guards for direct callers of
  these "private" methods. Resolved in #144: a repo-wide grep confirmed
  `score_batch` and the tests are the only callers. The `or 1` avgdl tail was
  deleted under a 468-pair scoring-parity proof, and the `_compute_idf`
  doc_freq<=0 guard was kept with its defends-direct-callers intent stated in a
  comment, the second option this note offered, because deleting it would make
  a standalone unit-tested helper silently wrong for a zero document frequency
  rather than merely unreachable. The two `math.log(2.0)` idf fallbacks a later
  review flagged were dead too and were removed in the same pass; the
  `\b\d{4,}\b` numeric-ID branch was re-examined and kept, since `\d` matches
  non-ASCII Unicode digits the ASCII class drops.
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
  2026-07-23 re-examination: the premise is weaker than this entry claims.
  `_DedupState` is constructed fresh inside every `apply()` call (its own
  docstring: "Per-apply scan state (never carried across calls)"), so
  `seen`'s lifetime is one compression pass, not the conversation; and
  `_FirstOccurrence.content` holds a REFERENCE to a string that already
  lives in the message list, not a copy — the only genuinely new
  allocations are the `"\n".join(texts)` units for nested parts lists.
  Peak-memory during one pass over a huge conversation is the residual
  concern, and it is small. Effectively demoted; a future session should
  re-verify before investing.
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
  Resolved 2026-07-23 via the decline option (see the Completed row): the
  live round trip proved the ambiguity swaps which value owns `a.b.c` (a
  silent corruption, not just a verifier artifact), plus five sibling
  families beyond the recorded pair; `flatten_breaks_dotted_equivalence`
  now fails all of them closed while benign dotted inners keep flattening.
  The grammar-level record of the flatten remains unimplemented and would
  only matter if a future corpus wants ambiguity-shaped inputs to keep the
  dotted-column compression instead of nested cells.
- 2026-07-21: `tests/test_hook_audit_fixes.py`'s two faA2 tests,
  `test_faA2_killed_pipe_delivers_partial_stdout` and
  `test_faA2_killed_pipe_delivers_partial_stdout_on_sigint`, shell out through
  `pretool_pipe.py`'s rewritten command, which invokes `uv run --no-project
  --with "furl-ctx[mcp]==1.3.0" python3 ...`. Verified directly against
  PyPI's JSON API today: 1.3.0 is not published, the latest release is
  1.2.0. In a clean environment with no local furl-ctx 1.3.0 wheel cached,
  uv cannot resolve that pin, and the shelled-out compressor invocation
  fails or hangs until it times out. The SIGINT variant is the one most
  likely to actually reach that failure, since a killed non-interactive
  shell survives at its own process group and falls through to the real
  `uv run` line regardless of the planted signal. The pending T13 bootstrap
  rework also touches this same `uv run` command surface. Watch both, once
  1.3.0 ships to PyPI and T13 lands, re-check whether this is still live.
  2026-07-23 re-check against PyPI's JSON API: latest published version is
  now 1.3.2 (1.3.0 was never published and never will be), and the #148
  hand-sync moved every embedded pin to `furl-ctx[mcp]==1.3.2`, so the pin
  resolves in a clean environment. Resolved unless the pins drift ahead of
  PyPI again between a release-PR merge and the publish job.
- 2026-07-23: correctness audit of bare `except Exception:` blocks (6 files:
  router_engine, pipeline, code_aware_compressor, tokenizers/base, cli,
  ccr/mcp_server), carried over from 2026-07-20 below. This session's Phase 1
  pass re-surveyed the repo-wide grep as a runner-up candidate: most sites now
  carry an explicit `# noqa: BLE001` plus a rationale comment naming the
  fail-open boundary (pipeline extension hooks, CLI diagnostics, opaque-offload
  detection, CCR persistence). A handful still have no comment at all (e.g.
  `furl_ctx/transforms/pipeline.py`'s transform-apply catch, which re-raises
  after recording a breaker failure, and several `router_engine.py` sites) and
  were not individually triaged for correctness vs. merely undocumented
  intent. Still needs the per-site triage the 2026-07-20 entry called for
  before any diff; not chosen today because a quick pass found no failing
  artifact to prove a concrete site is wrong, only documentation-completeness
  gaps, and per-site triage across 6 files is not completable to the flawless
  bar in one remaining session slot alongside a chosen candidate.
- 2026-07-23: `furl_ctx/transforms/compressor_registry.py` (152 lines) has
  zero references anywhere under `tests/`, carried over from the 2026-07-19
  test-quality survey below. Read in full this session: it is a thin
  lazy-init-and-cache extraction (six `get_*` methods memoizing a compressor
  instance), byte-preserved from the pre-extraction `ContentRouter` per its
  own docstring, and its behavior is already exercised indirectly through
  `ContentRouter`'s existing test suite (the getters are simple enough that a
  planted bug — e.g. skipping the memoization check — would very likely show
  up as a router-level regression). Lower expected value than the modules
  still called out below with zero *indirect* coverage either
  (`router_blocks.py`, `router_message_policy.py`); left as a shallow
  candidate rather than promoted.

Removed (already satisfied): "Property-based tests for the tabling grammar
round-trip, encode then decode equals identity" — verified 2026-07-19 that
`tests/test_csv_schema_decoder_roundtrip_fuzz.py` (1067 lines, present since
2026-07-12) already covers exactly this: 200+ seeded adversarial fuzz cases,
COR-13/COR-15 shape-coverage gates, and encode-then-decode identity
assertions throughout. No further action needed; keeping the stale entry
would only cost a future session the time to rediscover this.

## Notes for the maintainer

- 2026-07-22 session start: PR #145 (2026-07-21, "lazy-dev deletion sweep")
  touched a large fraction of the repo's Python and Rust source files in one
  commit (137 files). A literal per-file 14-day exclusion check on this
  session's Phase 0 found EVERY current `furl_ctx/**/*.py` and
  `crates/**/*.rs` file had been touched within 14 days, entirely because of
  that one sweep plus the following week's dense PR cadence — the naive
  "any file touched" rule would have blocked all fresh work. Read the rule's
  own escape hatch literally: "or go strictly deeper than what was done
  there, never sideways repetition" — a file merely swept for unrelated
  dead-code deletion or doc-pin sync is fair game for a genuinely different,
  deeper problem in the same file. This session's pick (field_role.rs) was
  touched by #145 only to delete 7 already-dead lines, unrelated to the
  entropy-classification bug fixed here. Future sessions building the
  exclusion set should check WHAT changed at each touched path, not just
  THAT it changed, once a repo-wide mechanical sweep is in recent history.

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
- 2026-07-21 session start: `python -c "import furl_ctx, re2"` passed (Phase
  0's own smoke check), but the first full `pytest tests/ -q` run on a
  completely untouched checkout showed 16 failures, all CCR recovery-hash
  and lossless-fidelity tests expecting the 24-hex key width from #133
  while the actual crush output still carried the pre-#133 12-hex key. Root
  cause: `target/release/lib_core.so` was compiled before #133/#137/#139
  landed (its mtime was `Jul 18 23:14`, HEAD was already at #135's merge),
  so the environment's Rust extension was stale relative to `HEAD` even
  though it imported fine. `maturin develop --release --extras dev,mcp`
  rebuilt it and the exact same checkout went to 2597 passed, 0 failed.
  Not a code regression — a container/session artifact — but worth this
  note so a future session facing a red `pytest` on an "untouched" checkout
  checks the compiled extension's freshness before concluding main is
  broken and starting a root-cause-fix session per Phase 0.5.
