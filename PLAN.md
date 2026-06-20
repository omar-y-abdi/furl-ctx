# PLAN — BLOAT REMOVAL + ARCHITECTURE REBUILD (current, 2026-06-20)

ARCHITECTURE LOCKED: cut ALL proxy → hook (data-plane) + 2-tool fastmcp (set_compression + retrieve→CCR direct).
Scope = Claude Code + Codex only. Full rationale in `.claude/runtime/handoff.md` (⭐ ARCHITECTURE DECISION).
Findings source: `lazy-dev-AUDIT-final.md` (+ -v2.md Python detail, -rust.md Rust detail). Repo = 63,164 code LOC,
~17k (~27%) cuttable: Tier1 ~7,070 safe-now, Tier2 ~9,920 after-untangle.

## PHASE 1 — TIER 1 (teammate running, EXCLUDES proxy)
Sonnet teammate `tier1-cutter` (background). Method: archive→2-gate→keep/restore→report. Scope + must-keep in handoff.
- [ ] Rust pipeline/ subtree (4,212) + safety.rs (215) — drop mod.rs re-exports, maturin rebuild, cargo+pytest gates.
- [ ] Python conftest dead fixtures (193), commented-out code, stale JSON results (1.45MB), empty phantom dirs.
- [ ] Teammate report: LOC removed + anything KEPT with caveats. Gates all green (519/31, recovery 21, cargo green).

## PHASE 2 — TIER 2 (user gives instructions after Phase 1)
Includes the proxy→hook+MCP REBUILD + the untangle cuts:
- proxy → DELETE; extract live SSE utils (proxy/helpers.py parse_sse_events/safe_decode) to ccr/sse_parser.py first.
- Build hook (data-plane, like the Biljakten one but productized) + 2-tool fastmcp (set_compression + retrieve→CCR direct,
  un-couple mcp_server.py:472 _retrieve_via_proxy).
- Rust live_zone.rs (2,899, hoist private AuthMode enum first) + recommendations.rs (329) + dead lib.rs FFI.
- Python cache-optimizer cluster (~3.8k), relevance/ (lazy BM25), ml_models, ccr/batch_processor (keep mcp_server).

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
