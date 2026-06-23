# PLAN — MASS-REPAIR + STANDALONE-EXCISE (ACTIVE, 2026-06-24)

**Two mandates (user):**
1. Fix every actionable `codebase-CRITIQUE.md` finding. Scope locked: both large refactors fully (#5 marker-grammar, #6 ContentRouter split); change the by-design items too (telemetry, retention, SmartCrusher).
2. **STANDALONE REPO — not a fork anymore.** Every loose thread pointing to the old repo / proxy / fork-era MUST be excised, not documented. "If something is pointing to the old repo, that has to go." This is a hard no-go for the user.

**HARD CONSTRAINT: no regression.** Gate every change: `.claude/runtime/gate.sh [bench]` = G1 cargo · G2 pytest · G3 surface · G4 recovery-21 · G5 run_bench==floor + needle 100%. Revert/restart on any FAIL.

**Floor (locked):** code@7 0.0% · logs@90 92.8% · search@90 92.2% · repeated_logs@90 96.5% · disk@9 50.0% · multiturn@135 70.6% · needle 100%.
**Baseline:** HEAD `4a412c43` — cargo 765 green, pytest 626 green, gate PASS (validated).

**PM loop per task:** spawn opus agent (one selective concern, NOT the whole list) → agent testimonial → I independently run gate → PASS+good ⇒ commit; FAIL/bad ⇒ restart agent with feedback. Nothing slips past.

## Backlog (risk ascending)
- [ ] **R0** — root .md cleanup (#2): move `lazy-dev-AUDIT*`, `EVAL-*`, `test-hardening-PLAN.md` → `docs/audits/`. *PM direct (zero-risk moves).* Gate (no bench).
- [ ] **R-EXCISE** ⭐ — repo-wide hunt + excise EVERY old-repo/proxy/fork-era thread. RECON DONE (surface in handoff.md): upstream = **`chopratejas/headroom`** → `pyproject.toml:101-103`, `Cargo.toml:20`, `CONTRIBUTING.md:90`, whole `CHANGELOG.md` (upstream issue links). ~110 proxy mentions in code (paths.py dead proxy artifacts, smart_crusher.py ×8, content_router, telemetry, ccr/mod.rs "the proxy will hold"). Fork-era tags (#21/#816/#847/PR-B1/F2.2/Stage-3c.2/PR4). `wiki/proxy.md` + proxy across wiki/. JUDGMENT per site (reframe to standalone reality, not s/proxy//g). Exclude archive/. Agent. Gate.
- [ ] **R1** — clippy `too_many_arguments` (#1): `prioritize_indices` args→struct, `-D warnings` clean. Agent: rust. Gate+bench.
- [ ] **R3** — dead Python twins (#4): delete `adaptive_sizer.py` body + `log/search _select_*`/`_parse/_score`; repoint parity+direct-call tests onto `compress()`/Rust. Agent: python. Gate+bench.
- [ ] **R4** — telemetry collapse (D1): collapse no-op OTel ceremony in `pipeline.py`; **keep `record_metrics`** (simulate dry-run). Agent: python. Gate.
- [ ] **R5** — CCR marker grammar ownership (#5): every Rust producer through a `marker_for_*` family; one exported spec; single Python parser. **Keep `explicit_hash`**, keep per-producer algos. Owns + revives (not proxy-era) `compute_key`/`marker_for`. Agent: rust+python. Gate+bench, recovery emphasis. HIGH risk.
- [ ] **R6** — ContentRouter god-object split (#6): extract routing-decision / StrategyDispatcher registry / MixedContentSplitter / CompressionCache / CCR-verify. Mind thread-local `_runtime_*`. Agent: architect+builder. Gate+bench+thread-safety. HIGH risk.
- [ ] **R7** — retention durable spill (D2): wire Sqlite (default; Redis behind config) as eviction spill. **No regression:** default behavior unchanged unless durable backend configured. Agent: database/builder. Gate+bench+new spill tests.
- SmartCrusher size (D3): MEASURE which strategy ships → report. Do NOT delete compaction (would regress lossless floor). Fold into R-EXCISE/analysis.

## Finish
- [ ] Full sanity check (gate+bench) on final tree.
- [ ] `rm codebase-CRITIQUE.md` (untracked — never committed; no history rewrite needed, verified).
- [ ] Re-run EXACT same `adversarial-critique.js` (unchanged) → delta report.

---

# PLAN — BLOAT REMOVAL + ARCHITECTURE REBUILD (current, 2026-06-20)

ARCHITECTURE LOCKED: cut ALL proxy → hook (data-plane) + 2-tool fastmcp (set_compression + retrieve→CCR direct).
Scope = Claude Code + Codex only. Full rationale in `.claude/runtime/handoff.md` (⭐ ARCHITECTURE DECISION).
Findings source: `lazy-dev-AUDIT-final.md` (+ -v2.md Python detail, -rust.md Rust detail). Repo = 63,164 code LOC,
~17k (~27%) cuttable: Tier1 ~7,070 safe-now, Tier2 ~9,920 after-untangle.

## PHASE 1 — TIER 1 ✅ COMPLETE (2026-06-20, EXCLUDED proxy)
Sonnet teammate `tier1-cutter` (id a609285). Method: archive→5-gate→keep/restore→report. ALL 6 items cut green, 0 kept-as-live.
- [x] Rust pipeline/ subtree (4,212, commit fdfd817f) + safety.rs (215, commit 1573cd92) — mod.rs re-exports dropped, maturin rebuilt, cargo green.
- [x] Python conftest dead fixtures (206, dd8bf221), commented-out line compression_store.py:1097 (5ea86f3b), stale JSON (~1.2MB, e24f7f44), empty phantom dirs memory/{adapters,backends} + leftover pipeline/{offloads,reformats} rmdir'd.
- [x] Total ~4,634 code LOC + ~1.2MB. HEAD=e24f7f44. ORCHESTRATOR INDEPENDENTLY RE-VERIFIED: pytest 519/31, cargo 0-failed all suites, surface 56, recovery 21, compress OK. Must-not-touch intact (proxy/telemetry/live_zone/recommendations/auth_mode.rs/compression_policy.rs/live compressors all present).
- CAVEAT for Tier 2: safety.rs's `tool_pair_indices` (tool-pair atomicity) was dead (never called outside own tests). If the live_zone dispatcher ever needs tool-pair atomicity, re-implement or restore from archive/.

## PHASE 2 — TIER 2 CUTS ✅ COMPLETE (2026-06-21, user: "allting kapas nu")
Teammate a609285 cut ~4,967 LOC (4,395 Rust+FFI + 572 Py). Commits f8493718 + 8a90716f. HEAD=8a90716f. Orchestrator re-verified: pytest 519/31, cargo 0-failed, surface 56, recovery 21, compress OK.
- [x] Rust live_zone.rs (2,899) + recommendations.rs (329) + lib.rs FFI (~109) + mod.rs re-exports + 5 cargo tests = ~4,395. NO AuthMode hoist needed (canonical src/auth_mode.rs decoupled). Removed FFI had 0 callers (safer than recon's "proxy-only").
- [x] Python ccr/batch_processor.py (562, BatchProcessor never instantiated) — cut clean.
- [x] RESTORED as live (gate-sorted): compression_feedback (CCR loop), ml_models+dynamic_detector (NER/semantic call-sites; ml_models caught by gate-blindness guard — G2 green, guard-grep red), relevance/embedding+hybrid + cache optimizer cluster (all TOP-56 public surface → G3 red).
- TIER-3 (NOT dead-code): surface-only-live items = coordinated API deprecation candidates (drop __all__/_LAZY_EXPORTS+docs+version bump), never pure-move. Flag to user if shrinking public surface.

## ★ ROADMAP RE-SEQUENCED 2026-06-21 (user) — north star: codebase BEYOND-PERFECT before any MCP tool creation
Order is now: (3) v4 audit + apply 4th-pass cuts → (4) HARDEN TESTS to best quality+coverage → (5) proxy→hook+MCP rebuild.
The MCP/hook build WAITS until the codebase is beyond-perfect for usage. Test-hardening is promoted ABOVE the rebuild.

## PHASE 3 — 4TH-PASS AUDIT ✅ DONE (apply optional/small, user-gated)
- [x] v4 feature-reachability audit (wf_c7c82ad2-c7c, 86 agents). Report `lazy-dev-AUDIT-v4.md` committed 0d111fc5.
- HEADLINE: tree is essentially LEAN. Tier-1-safe ~3.65k (~8%), but ~1.9k is proxy-coupled CCR plane (dies at the rebuild).
  Genuinely-free non-proxy residual ≈ 1.3-1.7k (config.py deadflags, utils.py 13 orphans, get_siglip, Rust test-only fns, dup verify/measure.py 879).
  Tier-3 "big" numbers (cache-optimizer 2.5k, anchor_selector 770, code_compressor 2k, embedding scorers 533) = PUBLIC SURFACE → API deprecation, NOT free.
- v4 earned its keep: 2 deptry false-positives confirmed LIVE (KompressCompressor, ComponentTracker); ★ csv_schema_decoder.py TRAP (591 LOC, 0 prod callers BUT guards the byte-exact recovery invariant via test_ccr_recovery_invariant.py:36 — MUST NOT CUT). Orchestrator spot-confirmed both.
- [ ] APPLY (optional, small, user-gated): the non-proxy deadflags + dup measure.py via archive+5-gate loop. Proxy-coupled CCR plane rides the Phase-5 rebuild. Surface-tail = separate deprecation decision. Report-only until user says apply.

## PHASE 4 — HARDEN TESTS (NEW, user-prioritized — do AFTER Phase 3, BEFORE the rebuild)
GOAL: iterate the test suite to best QUALITY + coverage. Quality ≠ coverage% — coverage is a FLOOR, not the goal.
REASONING (user): better tests → find + improve the codebase → plausibly BETTER COMPRESSION RESULTS. Tests that fail when
behavior breaks and survive refactors expose real engine weaknesses (the eval `break` pass already found silent-loss holes
this way; harder tests find more).
METHOD: use the installed `test-quality-tools:test-quality` skill + scorecard (mutation-resistance, anti-fragility rules,
boundary coverage for every </<=/>/>=/==/!= in source, real-I/O fixtures over mocks, contract-named tests). Coverage held as
a non-regression floor; iterate-to-plateau per module (scope one module/package per pass, not the whole repo at once).
Prioritize the hard-invariant surfaces first: CCR recovery, Py↔Rust hash parity, prompt-cache ordering, the compress() route.

★ SKILL LIMITATIONS (read the skill files — verified 2026-06-21, set expectations honestly):
- It iterates to PLATEAU, NOT "perfect". Stop = (a) 3 consecutive rounds with no gated improvement, OR (b) zero contract
  violations + every source comparison has a boundary test. Condition (a) usually fires first → it stops when the MODEL
  can't find a further gated win, not at an objective perfection bar. "Quality" is relative (better-than-start), not absolute.
- The best axes are MODEL JUDGEMENT, not measured: score.py auto-counts 9 regex axes; boundary coverage (B.2), real-I/O (B.3),
  contract-naming (E.3), tautological readbacks (A.3) are judgement axes assessed by reading.
- NO real mutation engine (no mutmut/cosmic-ray/Stryker in score.py). "Mutation-resistance" is a PROXY (boundary tests +
  pinned literals + the gate's "does the new test fail when you break the behavior"). It's monotonic (revert-on-no-gain → never degrades).
- ★ NO RUST PROFILE. score.py PROFILES = python/js/go/kotlin/swift only — zero rust/cargo/.rs. ~Half the codebase (Rust 23k LOC)
  gets ZERO auto-scoring. Python is the ONLY empirically-validated profile.
TWO-TRACK PLAN (because of the Rust gap):
  • Python: the skill + score.py loop, hard-invariant surfaces first.
  • Rust: apply the contract PRINCIPLES by hand (they're language-agnostic); for a REAL mutation score on the core, add
    `cargo-mutants` (optional but it's the only way to get an objective mutation number on the Rust engine).

## ✅ TIER-3 SURFACE CUT DONE (2026-06-23): 10/11 rows, surface 54→40, ~6.5k removed, engine green (pytest 443/0-failed, recovery 21). RelevanceScorerConfig retained (live). Tree now 39,171 code LOC.

## ★ DELIVERABLE-SHAPE DECISION (pending — governs Phase 5 below)
User FORKED headroom, building minimalistic hook+MCP alternative; end goal = collaboration w/ headroom devs, both methods side-by-side. Measured ~2,850 LOC proxy-route DORMANT for the hook/MCP method (proxy/ 1,847 pure transport + ~1,010 auth-mode policy layer woven in live engine + mcp_server proxy tendril). DECISION: (a) standalone fork → DELETE proxy-route; (b) side-by-side collab → KEEP proxy/ as headroom's method, don't wire it. Decides proxy-delete + version scheme. (Version bump 0.25→0.26 DEFERRED until this is decided.)

## ✅ PROXY-ROUTE REMOVED — CODEBASE CLEANUP COMPLETE (2026-06-23, HEAD 63ad09a2)
~5,200 LOC retired (proxy/ + compression_policy.py + Rust auth_mode/compression_policy + parity tests + mcp un-couple). Tree 36,605 code LOC. Verified: cargo 0-failed, pytest 417/14, recovery 21, compress unchanged (compress-path diff = docstring-only). Standalone hook/MCP-only fork. Remaining = packaging only (version bump + stale-docs sweep, user-gated).

## ✅ PHASE 4 TEST-HARDENING COMPLETE (2026-06-23, HEAD 0d108fca)
25/25 audit bugs fixed-or-determined + #26 (flaky test → determinism, not a recovery hole) + #4-upstream full fix (all 7 strategies propagate real bugs loud) + 93 new mutation-sensitive tests + full per-module hardening (8 modules, cache/base 0%→covered). Suite 626/14, recovery 21 byte-exact, run_bench == FLOOR on all 6 datasets + needle-recall 100% (NO compression degradation across the whole phase — user's hard constraint held), coverage 54→59%. Orchestrator independently verified all gates. score.py axes (B1 0→5, D2 0.017→0.043, A2 166→201) are heuristic/misleading here (B1 undercounts list/int/hash pins; A2 up = legit internal-invariant coverage kept per advisor). Reports: test-hardening-PLAN.md, test-baseline.md.

## PHASE 5 — HOOK+MCP BUILD (the user's actual new system — supersedes "proxy rebuild")
- proxy → DELETE; extract live SSE utils (proxy/helpers.py parse_sse_events/safe_decode) to ccr/sse_parser.py first.
- Build hook (data-plane, like the Biljakten one but productized) + 2-tool fastmcp (set_compression + retrieve→CCR direct,
  un-couple mcp_server.py:472 _retrieve_via_proxy).
- Engine gates are BLIND to transport → needs NEW functional verification (build+test the hook+MCP replacement BEFORE cutting proxy), NOT the archive loop.
- DO NOT START until Phase 4 (test-hardening) makes the codebase beyond-perfect. User's north star.

---

# PLAN — CUT & ARCHIVE (v1, 2026-06-19 — COMPLETE)

User: "Cut all code, everything that may seem important put it in an archive folder. Then test to see if the project still runs."
Driven by `lazy-dev-AUDIT.md`. Archive (git mv → `archive/`), NOT delete = reversible. Test-gated: pytest after each
batch; any cut that breaks tests → restore from archive (empirical reachability beats the static audit label).

GREEN BASELINE: pytest `519 passed, 31 skipped` (48s).
TWO CO-EQUAL GATES after every batch (advisor — pytest alone shares the lazy/dynamic blind spot the audit exposed):
  G1 pytest full; G2 surface-walk: `.venv/bin/python -c "import headroom;[getattr(headroom,n) for n in headroom.__all__];print('surface OK',len(headroom.__all__))"`
  G2 forces every __all__/_LAZY_EXPORTS entry to resolve — catches an archived-but-still-exported module pytest would miss.
PURE-MOVE CUTS ONLY: move file(s) → archive/ + remove their __all__/_LAZY_EXPORTS entries + archive co-located tests. Run G1+G2.
  RED → restore the whole batch (`git mv archive/X back` + revert export edits), mark item LIVE, continue. NEVER write
  try/except/lazy-init shims to force green (advisor: that fakes a green where behavior is silently a no-op no test covers).
ORCHESTRATOR does ALL git/moves directly (PLAN incident history: agents corrupt main). No subagents for the cutting.
DO NOT MOVE (audit PROVED live/protected): telemetry/, onnx_runtime.py, proxy/auth_mode.py, crates/.../compression_policy.rs, wiki/.
DEFER — need NEW untangle code, out of scope for pure-move loop: proxy/helpers.py (SSE utils live), relevance/ (compression_store:48
  unconditional BM25 import), ml_models (dynamic_detector imports). For these: ATTEMPT the move empirically; if red, restore + leave live.

STATUS: COMPLETE. ~7.4k LOC archived, engine green throughout (pytest 519/31 == baseline). HEAD=05ff00b2.
- [x] B0 archive/ + README.
- [x] B1 pure-dead docs/sql/docker/scripts + empty dirs (commit 2f234c04).
- [x] B4/B5 leaf-dead semantic + prefix_tracker + shared_context (commit 1755d4b1).
- [x] B6 proxy/interceptors + binaries (commit 55befe92).
- [x] B3 _OPTIONAL_EXPORTS + create_pipeline + manifest lines (commit 05ff00b2).
- [x] FINAL gates: surface-walk 56, CCR recovery 21/21 byte-exact, compress() saved 6316 tok. archive/RESTORE-LOG.md written.
- [DEFERRED — entangled/live, need real untangle code (full specs in archive/RESTORE-LOG.md)]: cache-optimizer cluster ~3.8k,
  relevance 1k, ml_models, proxy/helpers.py, ccr batch+mcp_server 1.9k (mcp_server KEPT for MCP-retrieve-plane), bench/verify dedup.

GATE OVERRODE STATIC AUDIT (empirical > analytical, as user wanted): count_tokens_*/Tokenizer.available are TESTED → kept (not vestigial).
RESTORE-ON-FAIL was never triggered — every attempted move stayed green on both gates.

---

# PLAN — Headroom parallel-eval phase

Full context: `.claude/runtime/handoff.md`. cwd /Users/k/dev/headroom, venv .venv (x86_64).

## GOAL (user)
3 reusable eval workflows × ~50 sonnet agents each, isolated worktrees, MEASURED before/after,
loop-until-dry, persistent ANTI-REPEAT ledger (no agent redoes a tried approach; must reason from
prior failures + justify novelty), → opus synth → 3 ranked action-docs for me. Cost irrelevant.

## ARCHITECTURE (decided)
ONE reusable parameterized workflow `headroom-parallel-eval` (`.claude/workflows/_drafts/headroom-parallel-eval.js`),
invoked 3×: mode=optimize | break | quality. Loop-until-dry (2 dry rounds), worktree+own-venv isolation
(break = read-only shared engine, no worktree), ledger threaded across rounds. Opus reads full ledger.
VALIDATED ✓ (0 warnings).

## STATUS  (GO given; baselineRef=608fc7ac3d4b97a717b59f675b1f5bb260ef3371)
- [x] wyg1sl7ew complete + INDEPENDENTLY VERIFIED (cargo 966/0, pytest 424/0, recovery 21, needle 100%, my off-fixture probe 137/137 byte-exact, granular never negative).
- [x] Eval workflow + escalation logic + codebase-map recon built, validated, saved (.claude/workflows/eval/).
- [x] Phase 0 satisfied via verification (engine built @HEAD, baseline measured).
- [x] Recon done (wmv5sxddo): CODEBASE-MAP.md written (11.3KB, 12 areas/137 files) + sent to user. Eval agents read it via codebaseMapPath (abs path, works from worktrees).
- [x] Perf fix: wired SHARED sccache into GUARD (RUSTC_WRAPPER+SCCACHE_DIR=/Users/k/dev/headroom/.sccache+CARGO_INCREMENTAL=0), removed `pip -U`, added import-precedence check. Cache pre-warmed (266 crates). --system-site-packages proven NOT to inherit .venv deps (don't use).
- [x] optimize DONE (w69iw516k): 8 rounds/48 attempts/32 wins. EVAL-optimize.md written. Honest synth (caught ifree=avail*10 fixture coincidence). Top real: #4 hex-transcoding (generalizes), #3 unit-strip; #1 error-gate-bypass +15.9pp but LOSSY on protected content (re-verify out-of-sample + needle before build).
- [x] CONTAMINATION fixed: agent #10 leaked edit to main (csv_schema_decoder.py + uv.lock) → restored, worktrees purged. HARDENED per advisor: two-copies guard, fail-loud precedence, break /tmp-scratch, map→/tmp, cause-agnostic post-run restore-ALL-changed-files.
- [x] break DONE (waftw5h1x): 5 rounds/50/33 defects, clustered A-H. Hardening HELD (main stayed clean; only a 0-byte stray `=` removed). EVAL-break.md written.
      INDEPENDENTLY CONFIRMED top 2 silent-loss defects myself: (A) multi-line field on lossless path = 0/90 byte-exact total loss, no CCR backstop; (G) inter-call FIFO eviction = unbacked sentinel. Earlier "recovery 100%" verification had coverage gaps (no multi-line, single-call only). break = credible.
- [~] **quality RUNNING (w9tllezie / wf_b8bf7eaa-5e6)** [worktrees+builds, hardened]. On notify: restore any changed main files cause-agnostically, purge worktrees, write EVAL-quality.md.
- [x] Presented 3 action-docs + EVAL-SUMMARY.md. User chose: implement P0+P1 via opus-plan→sonnet-build→opus-verify workflow, then same for P2.
- [x] Committed eval deliverables + workflows + gitignore (.venv-eval/.sccache/worktrees) at 540906e9, advisor-fixes at d9adcfb8.
- [x] Built headroom-implement workflow (Plan/Build/Integrate/Verify), advisor-reviewed (scoped git-add, bundle dependent/same-file units, target/ ignored confirmed).
- [x] INCIDENT: run wviuvkwx1's integrator agent ran `git checkout main` on shared checkout. Canary caught it; FULLY RECOVERED (branch verify/phase2-audit-report intact @ d9adcfb8, main untouched, 0 data lost). Worktrees purged.
- [x] FIX: reworked headroom-implement to Plan+Build only; integration moved to ME (deterministic Bash, never switches branch); builders restricted to a git-allowlist (no checkout/switch/reset/branch). Committed 47eaf125. Planner now makes units fully independent (disjoint files, dependents bundled).
- [~] **P0+P1 IMPLEMENT RUNNING (wfh22vjl6 / wf_ad2e78a5-43b)** off baseline 47eaf125. NO agent touches main git → main HEAD must stay 47eaf125.
- [ ] On completion: confirm HEAD==47eaf125; cherry-pick each self-verified builder sha onto branch (guardrail per commit, revert on regress); purge worktrees; restore baseline files.
- [ ] Independently verify: my multi-line + eviction probes must now PASS; full guardrails; opus verify.
- [ ] Then headroom-implement scope=P2. Present.

## INTEGRATION PROTOCOL (orchestrator, after wfh22vjl6 returns)
On branch verify/phase2-audit-report. For each ready unit (self_verified+contracts_ok+commit_sha), in plan.integration_order:
`git cherry-pick <sha>` → maturin develop (sccache) → cargo + recovery + full pytest + run_bench (needle 100%) → restore baseline files → keep if green else `git revert --no-edit HEAD`. NEVER switch branch / reset --hard. Then purge worktrees.

## IMPLEMENT BASELINE: 47eaf125 (eval docs+workflows+gitignore+safe-workflow committed; engine code still = 608fc7ac's)

## BIG FINDING (break): engine has REAL silent-loss holes the prior verification missed
Cluster A (multi-line CSV field -> total loss, lossless path, no sentinel) + Cluster G (inter-call eviction -> unbacked sentinel). Both confirmed by me. These likely jump the queue over optimize gains when user prioritizes.

### RESOLVED
- [x] Cluster A — FIXED + committed 9930aade. Probe A 96/96 byte-exact (sha match). Regression test_csv_schema_affix_multiline.py.
- [x] Cluster G — REFRAMED + committed f6fa3759 (+ a654963a). Verified: eviction real (concern#1) but loss ALREADY LOUD (concern#2) on BOTH model-facing surfaces (response_handler + mcp_server); granular bare-hash = whole-blob, all-or-nothing (no silent subset); context_tracker is proactive-only. G as silent-loss defect does NOT reproduce. Real residual = misleading miss-message → fixed cause-honest (no ledger). FREE LUNCH (true cross-call retention) documented in CCR-RETENTION.md as open next step (wire existing Sqlite/Redis backend, session-scoped). Probe G reframed→PASS (loud). Guardrails: pytest 519/0, recovery 21/21, needle 100%.
- NEXT (user-gated): claim the free lunch (durable backend spill); then resume P0+P1 implement track / P2.

## POST-RUN RESTORE (cause-agnostic, after EVERY mode)
`git status --porcelain` → for every changed TRACKED file: `git checkout HEAD -- <file>`. Confirm clean before next mode. Purge any leftover .claude/worktrees/. (advisor mandate)

## SEQUENCING
Sequential (one workflow at a time) — 3 parallel × 10 concurrency would thrash 12 cores + collide.
Order: optimize → break → quality. roundSize 6 (opt/qual, build-heavy) / 10 (break, light). 12 cores → cap 10.

## CONTRACTS (agents must not break; report contracts_ok=false if they do)
CCR recovery invariant · prompt-cache ordering (idx0/prefix/cache_control) · Py↔Rust parity · no overfit to verify/ fixtures.

## GOTCHAS
- Agents MUST use own `.venv-eval` per worktree — NEVER the shared .venv. (in prompt)
- run_bench overwrites benchmarks/baseline_results.json + BASELINE.md → restore after any real run.
- Branch deviation: work sits on `verify/phase2-audit-report`, main stale. Reconcile (non-destructive ff) AFTER eval — flag to user, don't do unasked.
- fable inaccessible → workers=sonnet, synth=opus. claude-mem disabled.

## NEXT (do NOT start unasked)
After eval docs: integration layer (PostToolUse hook + MCP control-plane) — separate, user-gated.

## HOOK REAL-WORLD TEST — Biljakten (2026-06-14) — DATA-PLANE FIRST TEST
Built + tested the PostToolUse data-plane hook against a real project. Full detail in `.claude/runtime/handoff.md`.
- Hook: `/Users/k/dev/Biljakten/.claude/hooks/headroom-compress.py` + settings.json entry (matcher `Bash|Read|Grep|Glob`). Fail-open, shape-preserving `updatedToolOutput`.
- BUG fixed: `updatedToolOutput` must match each tool's OUTPUT SCHEMA (Read=object `{type,file:{content}}`, Bash=`{stdout,...}`); a bare string is rejected → original used. Now rebuilds the received tool_response with only the text slot swapped. Also: compress clean `file.content`/`stdout`, not the json wrapper.
- HIT-ZONE (empirical): only FLAT homogeneous JSON arrays compress (SMART_CRUSHER 93–96%). Nested/pretty JSON, code, free-text, git-log → 0% passthrough.
- BIG FINDING (2 layers): (1) DECODE error — model misread dense columnar format, 2/10 hits wrong (BMW/Mercedes), clustered at `=` ditto rows; effort-fixable, model admitted it. (2) SAMPLING BLINDNESS (effort-PROOF) — asked "top-10 by models", the model answered from the 10 KEPT rows (a compressor sample, not the answer): real top-10 includes Opel/Peugeot/Toyota (offloaded, invisible), model listed Audi/BMW/Kia (not top-10). 3/10 wrong; NO effort fixes it (data is in the 139 offloaded rows). Confident, well-formatted, WRONG. → row-drop + ranking/aggregate queries = silently wrong answers. headroom's "lossy by deletion" weakness in the agent loop.
- NEEDS: (a) MCP retrieve plane (recover offloaded rows), (b) compressed view must signal "SAMPLE: N shown / M offloaded" so model retrieves before aggregating, (c) legible format.
- OPEN ENGINE FEATURE: "nested-aware path" — recursively find + crush flat sub-tables inside nested JSON (Rust core, not the hook). Widens hit-rate to real nested API/tool output.
