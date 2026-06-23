# HANDOFF — Headroom compression engine (rebuild + max-compression + verification)

cwd: /Users/k/dev/headroom · venv: .venv (x86_64, python3.13 restored) · branch: verify/phase2-audit-report
Build: `.venv/bin/maturin develop` (rebuilds pyo3 ext). Public API: `from headroom import compress`.

## ⭐ ARCHITECTURE DECISION — LOCKED 2026-06-20 (the north star)
DELIVERY = **hook (data-plane) + 2-tool fastmcp server (control-plane)**, NOT the proxy. CUT ALL PROXY.
- Scope: ONLY agent harnesses with same architecture as Claude Code (Claude Code + Codex). NOT general/any-app.
  This is the exact condition that makes the proxy's universality worthless here → proxy = ~3.2k LOC of pure cost.
- Engine is transport-agnostic: proxy, hook, MCP all call the SAME `ContentRouter`/`SmartCrusher`. Only transport differs.
- WHY hook wins (verified from source): (1) hook only sees tool OUTPUTS = headroom's actual hit-zone; proxy's "extra"
  reach is cached prose (untouchable) or non-JSON (engine passes through anyway). (2) hook compresses BEFORE content
  enters context → cache forms around compressed bytes → the whole "did we bust the cached prefix" risk DISAPPEARS
  (proxy does delicate live_zone surgery to avoid it). (3) ~3.2k LOC of proxy is streaming/SSE/auth the hook never needs.
  (4) MCP makes retrieve an explicit model-visible tool → better fit for sampling-blindness than proxy's silent inject.
- THE 2 MCP TOOLS: `set_compression(on/off[/mode])` + `retrieve(hash)`. retrieve MUST point at CCR store
  (`headroom/cache/compression_store.py`) DIRECTLY — current `mcp_server.py:472 _retrieve_via_proxy` is proxy-coupled,
  that coupling is the one non-trivial bit of the rebuild (rest is straightforward).
- SEQUENCING: Tier-1 cleanup NOW (teammate, excludes proxy) → Tier-2 (incl. proxy→hook+MCP rebuild, user gives instructions).

## WHAT THIS IS
Forked LLM-context compression engine. Rust core `crates/headroom-core` (+ pyo3 `crates/headroom-py`) + thin Python `headroom/{transforms,ccr,cache}`. Amputated 384k→~91k LOC, hardened, max-compressed, independently verified.

## JOURNEY (done, all committed)
- Phase 0: origin detached. Phase 1: amputate 384k→91k (−76%), build green, suite green (~352).
- Phase 2: DESIGN.md → Imp1 (1A unconditional CCR persist, 1B novelty fill+singleton pin) + Imp2 (field-aware stable hash) + Imp3 honest bench. Adversarial loop proved+locked the CCR recovery invariant; fixed non-dict silent loss + marker-off/blob holes.
- Max-compression: round-2 (delta/dict/cross-message), round-3 (affix-fold, head-dict + entropy-floor crushability override). route-by-min-tokens (RoutingPolicy=MinTokens default).
- Fixed TTL/result-cache silent-loss bug (recompute-on-unbackable, commits abfb19b3 + 8cae60a6).
- Independent held-out verification (verify/ slugify+is-plain-obj; verify/heldout/ express+chalk+npm-cli) — engine untouched. VERDICT: recovery REAL + byte-exact + generalizes (not overfit); 0 silent loss; cache-prefix safe.

## CURRENT NUMBERS (held-out, honest)
~93-97% on redundant/medium data; DEGRADES on near-unique: logs high 82%/genuine 80%, disk 40-44%, multiturn 28-39% (only 70.8% at 900-items/low-entropy). Headline = CEILINGS, not typical. Recovery 100% byte-exact everywhere.

## DONE — wyg1sl7ew (`headroom-fix-weaknesses`): granular-CCR `cbf16a85` + strict/honest-verify `d3fdc3f5`. Both landed, tree clean.

## NOW — parallel-eval phase, PREPPED + WAITING FOR USER "go"
User wants 3 eval workflows × ~50 sonnet agents (isolated worktrees, MEASURED before/after), loop-until-dry,
ANTI-REPEAT ledger (no agent redoes a tried approach; reason from prior failures + justify novelty), → opus
synth → 3 ranked action-docs. Cost irrelevant ("brim-optimize everything").
- Built ONE reusable parameterized workflow `headroom-parallel-eval` (modes optimize|break|quality).
  Saved: `.claude/workflows/eval/headroom-parallel-eval.js`. Validated 0 warnings.
- EXACT launch steps + args: `.claude/runtime/eval-launch.md` (Phase 0 rebuild+baseline, then 3 sequential runs).
- BLOCKED: user's own workflow (opus agent) still running → repo LOCKED, no build/launch until user says "go".
- On "go": follow eval-launch.md (re-read HEAD, rebuild shared .venv, fresh baseline, run optimize→break→quality,
  write EVAL-{optimize,break,quality}.md from synth.doc_markdown, present to user).

## HOOK REAL-WORLD TEST — Biljakten data-plane (2026-06-14)
First real-world test of the integration-layer DATA PLANE: a PostToolUse hook that pipes tool output through the engine.
- BUILT: `/Users/k/dev/Biljakten/.claude/hooks/headroom-compress.py` (+ 2nd PostToolUse entry in that project's settings.json, matcher `Bash|Read|Grep|Glob`, runs `/Users/k/dev/headroom/.venv/bin/python3`). Fail-open (any error/parse-fail/no-shrink → ORIGINAL kept, exit 0). Compresses via `ContentRouter`; replaces output with `hookSpecificOutput.updatedToolOutput`.
- BUG FOUND+FIXED (Biljakten transcript = ground truth): `updatedToolOutput` MUST match the tool's native OUTPUT SCHEMA. Read = object `{type,file:{content,...}}`; Bash = `{stdout,stderr,interrupted,isImage,noOutputExpected,persistedOutputPath,persistedOutputSize}`. v1 returned a bare STRING → Claude rejected (`invalid_type: expected object, received string`) → fell back to ORIGINAL (uncompressed); that was the "hook warning" the user saw. FIX: rebuild the RECEIVED tool_response with only its text slot (`file.content`/`stdout`/`text`) swapped → schema-valid for any tool. Also fixed: v1 compressed the `json.dumps` WRAPPER (degenerate — whole file behind 1 CCR sentinel); now compresses CLEAN content.
- COMPRESSION SHAPE RULES (empirical — the engine's real hit-zone):
  - FLAT homogeneous JSON array → SMART_CRUSHER 93–96% ✓ (149 brand records 6797→324 = 95%).
  - NESTED / pretty-printed JSON (`filter_items` in `filter_items`) → TEXT fallback, 0% → passthrough.
  - Source code · git-log · free text → passthrough.
  - i.e. headroom ONLY compresses arrays-of-flat-records.
- BIG FINDING — MODEL MISREADS THE DENSE FORMAT: on a real 95% compression (10 visible rows + 139 offloaded to CCR), the model decoded the columnar `[10]{hits,make,models}` rows BY EYE and got 2/10 hits WRONG (BMW 11450→read 11530; Mercedes 11530→read 5248). Errors clustered at `=` ditto-marker rows — model resolved ditto SEMANTICS right (BMW models=28, Renault=41) but scrambled positional column-alignment for neighbors. PLUS 139/149 rows offloaded to `<<ccr:...>>` sentinels = UNRETRIEVABLE (no MCP retrieve tool wired). → "fully compressed, decode-by-eye" is LOSSY in practice: model errors + unrecoverable offload.
- BIGGER FINDING — SAMPLING BLINDNESS (effort-PROOF): the model (even after correcting the decode error) answered the actual question ("top 10 brands by MODELS") from the 10 KEPT rows — but those 10 are what the compressor sampled, NOT the answer. Real top-10-by-models includes Opel(40)/Peugeot(40)/Toyota(39) which were OFFLOADED → invisible; model instead listed Audi(28)/BMW(28)/Kia(26) which aren't top-10. 3/10 wrong, and NO amount of effort/decoding fixes it because the needed data is in the 139 offloaded rows. The model gave a confident, well-formatted, WRONG table; nothing in the compressed view forced it to realize "I can't rank 149 from 10 rows." → row-drop + ranking/aggregate queries ("top-N", "most", "sum", "count") = confidently wrong answers, model unaware. This is headroom's classic "lossy by deletion" weakness surfacing in the agent loop.
- IMPLICATION: validates the integration-layer design — DATA PLANE (hook) works, but the engine needs (a) the MCP CONTROL/RETRIEVE plane so offloaded rows are recoverable, (b) the compressed view must make the model AWARE the table is a SAMPLE (N shown, M offloaded) so it retrieves before aggregating/ranking, and (c) a model-legible format OR not asking the model to decode columnar+ditto by eye. Next concrete step toward the user's integration layer.
- CANDIDATE ENGINE FEATURE (user asked for layman explanation): "nested-aware path" — recursively find flat sub-tables INSIDE nested JSON and crush each, keeping the tree. Engine work (Rust core `crates/headroom-core`), NOT the hook. Would widen hit-rate on real (nested) API/tool output. Moderate effort: flat-table crusher already exists; new part = recursive walk to FIND inner tables + re-assemble + keep round-trip/recovery invariant.

## HARD CONTRACTS (never break)
CCR recovery invariant (100% recoverable, 0 silent loss; tests/test_ccr_recovery_invariant.py). Prompt-cache ordering (never drop msg index 0 / reorder cached prefix / rewrite cache_control). Python↔Rust parity (compute_item_hash / canonical CCR hash byte-stable). Default lossless decoder behavior. No synthetic benchmarks. No overfit to verify/ fixtures.

## GUARDRAIL CMDS
`cargo test -p headroom-core 2>&1 | grep 'test result:'` (0 failed) · `.venv/bin/maturin develop` · `.venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --continue-on-collection-errors --timeout=120` (≥420/0) · `.venv/bin/python -m pytest tests/test_ccr_recovery_invariant.py -q` (21) · `.venv/bin/python -m benchmarks.run_bench` THEN `git checkout HEAD -- benchmarks/baseline_results.json benchmarks/BASELINE.md` (run_bench overwrites baseline — always restore).

## OPEN / NEXT (user steers each step)
- Integration layer (user's design, awaiting final confirm): PostToolUse hook (compress, 2 flags: `fully on` + `gömda`) + MCP server (`set_compression(mode)` / `retrieve(hash)` / `expand`). Control-plane (MCP, model-driven) + data-plane (hook). RoutingPolicy config = the knob MCP flips. Model-controlled adaptive compression. NOT built yet.
- After weaknesses fix: maybe honest README, hybrid-routing default.

## GOTCHAS
- fable5 (`model:'fable'`) INACCESSIBLE this session → use inherited Opus (workflows already switched).
- claude-mem plugin DISABLED (settings.json enabledPlugins→false) — was looping (orphaned 13.5.7 + wedged worker); ignore its hook errors.
- zsh does NOT word-split unquoted vars → list files explicitly in pytest cmds.
- Routing-gate hook needs `./.claude/workflow/DEFAULT_WORKFLOW.md` read (cwd-relative) before Write/Bash; headroom/.claude/workflow/ docs created to unblock subagents.
- Agent tool `builder` subagents need a filled instruction-doc at /tmp/subagent-instructions-<session>-builder.md (template at ~/.claude/hooks/lib/subagent-templates/_default.md).
- Workflow agents commit to main; run engine-touching agents SEQUENTIALLY (shared maturin build + git index race).

## REUSABLE WORKFLOWS (saved, .claude/workflows/{group}/)
amputate/headroom-amputation-map · compression/headroom-engine-map · compression/headroom-recoverability-refute · compression/headroom-max-compression · review/headroom-fullscale-review · (_drafts/headroom-adversarial-verify, headroom-fix-weaknesses)

## KEY ARTIFACTS
DESIGN.md, BENCHMARKS.md (repo root); verify/REPORT.md + verify/heldout/REPORT.md (the truth); benchmarks/ (run_bench, run_final, datasets, BASELINE.md).

## SIMPLIFY-AUDIT (2026-06-19) — lazy-dev over-engineering audit, REPORT-ONLY
User: "simplify my entire codebase" via /workflow-architect + Lazy Senior Dev (ultracode). Built a dynamic
workflow (`.claude/workflows/_drafts/simplify-audit.js`, validated 0 warn) — 9 read-only `Explore` auditors
(no Edit/Write) per area → selective adversarial verify of big `delete` claims → synth returns markdown;
ORCHESTRATOR writes the single additive file `lazy-dev-AUDIT.md`. Nothing mutates the repo. Run wf_713eb596-33f.
- TREE: auditing branch `verify/phase2-audit-report` @ a654963a (the latest-work tree; main stale @ 47eaf125).
  Stale `wf_ad2e78a5` worktrees from the dead P0+P1 run still in .claude/worktrees/ — audit excludes them.
- KEY SCOUT FINDING (the headline): the "amputated" bloat is STILL LOAD-BEARING via keep-set imports —
  proxy(3894) <- transforms/pipeline.py(lazy)+compression_policy.py+ccr/*; tokenizers(1816) <- pipeline.py(lazy);
  models(596) <- config.py+cache/*; providers <- tokenizer.py; storage <- cache/backends; hooks.py <- compress.py;
  relevance(1017)+shared_context lazily re-exported by __init__.py. observability/telemetry(4001)/integrations/
  onnx_runtime/component_tracker appear unreferenced (only proxy/models reach telemetry). So naive "rm dead dir"
  is WRONG; the audit traces LIVE vs VESTIGIAL reachability from compress() + names the untangle per cut.
- DELIVERABLE: ranked cut-list in 3 tiers (safe-now / cut-after-untangle / internal-shrink). Applying = SEPARATE
  user-gated step (compression engine w/ hard CCR/cache/parity invariants — never auto-delete).
- DONE (run wf_713eb596-33f, 15 agents/967k tok): wrote `lazy-dev-AUDIT.md` (repo root). 45 findings, 11 verified-safe, 1 refuted.
  Verify CAUGHT THE SCOUT BEING WRONG (its whole point): telemetry/(4001) is LIVE (SmartCrusher TOIN loop, every lossy
  compress) — NOT cuttable; wiki/(6200) REFUTED (mkdocs docs_dir:wiki + REALIGNMENT retirement plan + live CHANGELOG link).
  I CAUGHT a synth coherence bug: REALIGNMENT/(3900) ranked Tier-1 "dead" yet cited as the LIVE plan that keeps wiki/ →
  reclassified as roadmap docs (archive, not blind-delete). Honest cut budget of ~91k LOC: ~1.3k zero-risk now (sql/docker/
  stray scripts/manifest lines), ~9.3k after small export/import untangles (cache-optimizer family 4.4k = biggest LOC lever;
  CCR batch+mcp_server 1.9k — but mcp_server load-bearing IF the planned MCP retrieve plane is built next; relevance 1k;
  interceptors+binaries 1.2k; shared_context, ml_models, bench dedup), ~35 lines internal shrink. Amputation INCOMPLETE +
  keep-set more entangled than handoff claimed. Workflow saved: `.claude/workflows/cleanup/simplify-audit.js` (reusable).
  Committed report+workflow. APPLYING NOT STARTED (user-gated).
- CUT APPLIED (user: "cut all code, archive anything important, test it runs"): archive-not-delete + test-gated.
  ~7.4k LOC moved to `archive/` (reversible; restore map in archive/RESTORE-LOG.md), engine GREEN throughout.
  TWO GATES every batch (advisor — pytest shares the audit's lazy/dynamic blind spot): G1 pytest 519/31 (== baseline,
  zero regress); G2 surface-walk all __all__ resolve (59→56); final: CCR recovery 21/21 byte-exact + compress() saved 6316 tok.
  Commits: B1 2f234c04 (docs/sql/docker/scripts), B4/5 1755d4b1 (semantic+prefix_tracker+shared_context), B6 55befe92
  (proxy/interceptors+binaries), B3 05ff00b2 (_OPTIONAL_EXPORTS+create_pipeline+manifest). HEAD=05ff00b2.
  GATE OVERRODE STATIC AUDIT (good): count_tokens_*/Tokenizer.available are TESTED → NOT vestigial, kept; telemetry/onnx/wiki LIVE, kept.
  DEFERRED (entangled/live — need real untangle code, unsafe for pure-move loop; full specs in archive/RESTORE-LOG.md):
  cache-optimizer cluster ~3.8k (registry←tokenizers, compression_feedback←compression_store), relevance 1k (unconditional
  BM25 import), ml_models, proxy/helpers.py (SSE live), ccr batch+mcp_server 1.9k (mcp_server KEPT — load-bearing if MCP
  retrieve plane built next), benchmarks/verify dedup (needs verify/ run). These are the user's next-decision items.
- SIMPLIFY-AUDIT V2 (2026-06-20): user wants exhaustive 2nd pass, more/different agent types, find ALL hidden fat.
  Built `.claude/workflows/_drafts/simplify-audit-v2.js` (validated): 10 read-only SPECIALIZED lenses (ecc:python-reviewer,
  ecc:rust-reviewer, Explore, code-architect, ecc:type-design-analyzer, config-auditor, ecc:pr-test-analyzer, ecc:comment-analyzer)
  → loop-until-dry (cap 3) → perspective-diverse verify (drop only MAJORITY-refuted, keep uncertain as needs-review) →
  completeness critic → synth. GROUNDED in real tools (ran vulture/ruff/deptry inline → `.claude/runtime/fat-groundtruth.md`).
  Advisor fixes pre-launch: (1) keep uncertain not just confirmed, (2) LOUD dead-lens logging (null≠clean), (3) complexity-lint
  quarantined out of cut tiers. Run wf_c4d4246b-f17 IN FLIGHT.
- RECLAIMED 19GB: rm'd 8 dead `.claude/worktrees/wf_ad2e78a5-*` + `git worktree prune` (handoff's never-done TODO). Engine imports OK.
- Tool signals already in hand: ruff 36 dead-code/unused (7 ERA001 commented-out, ~22 RUF100 unused-noqa) + 431 lint (complexity-heavy,
  NOT for cutting); vulture 2 (conservative); 1 Rust #[allow(dead_code)] live_zone.rs:1299; deptry needs rescope (lens redoes).
- V2 DONE (wf_c4d4246b-f17): 281 agents/12.7M tok/3 rounds/230 survivors/0 net-refuted. Report `lazy-dev-AUDIT-v2.md` (committed 7b999384).
  Tier-1 ~2.6k LOC safe now + 1.45MB stale JSON (verify/*raw_results.json, benchmarks/*_results.json) + 25.6MB gifs(needs-review) +
  ~2.8k doc-cruft lines (wiki HeadroomClient/SharedContext/RollingWindow stale). Tier-2 ~6.6k incl live_zone. Synth caught ERA001
  6/7 false-positives, dual-lockfiles not-dup. My spot-checks confirmed (conftest providers.openai import, artifacts, gifs).
- VENV INCIDENT + FIX: `brew install cloc` removed python@3.13 → .venv interpreter symlink dangled (engine unusable). Root-caused +
  FIXED via `brew install python@3.13` (restored /usr/local/opt/python@3.13). Verified: .venv python 3.13.14, engine OK, recovery 21/21.
- RUST WORKFLOW DONE (wf_c7539e4d-6cf): 47 agents/2.56M tok. 22 findings -> 6 dead targets, ~7,750 net dead Rust LOC.
  Report `lazy-dev-AUDIT-rust.md`. TIER1 SAFE: pipeline/ subtree 4,212 (zero pyo3, zero callers, no headroom-proxy crate) +
  safety.rs 215. TIER2: live_zone.rs 2,899 (hoist private AuthMode enum first, ≠ canonical auth_mode.rs) + recommendations.rs 329 +
  lib.rs FFI 94. REFUTED my dup hypothesis: Rust log/diff/search compressors NOT dead — live via pyclass; only offloads/ that point
  at them are dead. Spot-checked + confirmed (2 crates only, CompressionPipeline 0 pyo3 bridge, only dead mod.rs:60 re-export).
- CONSOLIDATED: `lazy-dev-AUDIT-final.md` written. Repo=63,164 code LOC (cloc, Py 34k+Rust 29k). Cuttable: Tier1 ~7,070 (Rust 4,427 +
  Py 2,640), Tier2 ~9,920 (Rust 3,322 + Py 6,600) = ~17k code ≈ 27% (matched my 25-33% prediction) + ~2.8k doc + 1.45MB/25.6MB disk.
  3 docs: lazy-dev-AUDIT-v2.md (py/doc/disk detail), lazy-dev-AUDIT-rust.md (rust detail), lazy-dev-AUDIT-final.md (merged exec).
- NEXT (user-gated): apply via archive+2-gate loop. Suggested order: Rust Tier1 pipeline/+safety (4.4k cleanest big win) -> Py Tier1 -> untangle tiers.

## BLOAT REMOVAL — PHASE 1 ✅ COMPLETE (2026-06-20)
DONE: teammate tier1-cutter (id a609285, sonnet) cut ALL 6 Tier-1 items green, 0 kept-as-live. ~4,634 code LOC + ~1.2MB.
Commits: fdfd817f (pipeline/ 4,212) · 1573cd92 (safety.rs 215) · dd8bf221 (conftest 206) · 5ea86f3b (compression_store:1097) · e24f7f44 (stale JSON ~1.2MB). HEAD=e24f7f44.
ORCHESTRATOR RE-VERIFIED INDEPENDENTLY (not just trusting report): pytest 519/31, cargo 0-failed all suites, surface 56, recovery 21/21, compress OK. Tree clean. Must-not-touch confirmed intact (proxy/telemetry/onnx/relevance/live_zone.rs/recommendations.rs/src/auth_mode.rs/src/compression_policy.rs/log+diff+search_compressor.rs all present). Empty leftover dirs (pipeline/{offloads,reformats}, memory/{adapters,backends}) rmdir'd.
TIER-2 CAVEAT (from teammate): safety.rs `tool_pair_indices` (tool-pair atomicity) was dead — never called outside own tests. If live_zone dispatcher later needs tool-pair atomicity, re-implement or restore from archive/.
## BLOAT REMOVAL — PHASE 2 (TIER-2 CUTS) ✅ COMPLETE (2026-06-21)
DONE: teammate a609285 cut ~4,967 LOC (4,395 Rust+FFI + 572 Py). Commits f8493718 (live_zone+recommendations bundle) + 8a90716f (batch_processor). HEAD=8a90716f. Everything else RESTORED as live (gate loop sorted empirically, exactly as designed).
ORCHESTRATOR RE-VERIFIED INDEPENDENTLY: pytest 519/31, cargo 0-failed all suites, surface 56, recovery 21/21, compress OK. Tree clean. Removed FFI symbol compress_openai_responses_live_zone = 0 refs anywhere (my stale handoff note was WRONG — it had ZERO callers, not proxy-only; proxy NOT broken). live_zone_tail (proxy memory-inject mode) + live_zone_only (transforms/compression_policy.py flag) are UNRELATED live strings, untouched. must-keep 11/11 intact.
★ GATE-BLINDNESS GUARD EARNED ITS KEEP: ml_models.py G2 stayed GREEN (spacy path env-gated = one of the 31 skips) but guard-grep FIRED on dynamic_detector.py:603 `MLModelRegistry.get_spacy()` live call-site → teammate restored it. Without the whole-tree grep guard, pytest-green would have wrongly cut live code.
TIER-2 RESTORED-AS-LIVE (recon was right "mostly live"): compression_feedback.py (G4 red, CCR feedback loop compression_store:1089), ml_models.py+dynamic_detector.py (functional NER/semantic call-sites, NOT plumbing), relevance/embedding.py+hybrid.py + cache/anthropic+openai+google+registry (all G3 red — TOP-56 PUBLIC SURFACE, not dead).
★ TIER-3 CANDIDATE (NOT dead-code — coordinated API deprecation): the surface-only-live items (relevance EmbeddingScorer/HybridScorer/create_scorer, cache optimizer registry) are published in headroom.__all__+_LAZY_EXPORTS with little/no runtime instantiation. Removable ONLY as a deprecation (drop __all__/_LAZY_EXPORTS + docs + version bump = API break), never as a pure-move cut. Flag to user if they want to shrink the public surface.

CUMULATIVE (Tier-1 + Tier-2): ~9,601 code LOC removed (Rust 8,822 + Py 779) + ~1.2MB artifacts, engine GREEN throughout, fully reversible in archive/.

## ★ ROADMAP RE-SEQUENCED 2026-06-21 (user) — NORTH STAR: codebase BEYOND-PERFECT before any MCP tool creation
New order: (3) v4 audit + apply 4th-pass cuts → (4) HARDEN TESTS to best quality+coverage → (5) proxy→hook+MCP rebuild.
The hook+MCP build is DEFERRED; test-hardening is promoted above it. Doc-only update now (user: "enbart uppdatera docs just nu, vill bara berätta what our next steps are. MCP tool creation will have to wait until the codebase is beyond perfect for usage").
- STEP 3 ✅ AUDIT DONE: v4 feature-reachability audit (wf_c7c82ad2-c7c, 86 agents/3.8M tok/70 findings/0 panel-refuted). Report `lazy-dev-AUDIT-v4.md` committed 0d111fc5. HEADLINE: **the tree is now essentially LEAN.** Static tools clean (cargo 0-warn, vulture 2, ruff 34 nits). Tier-1-safe ~3.65k (~8%) but ~1.9k of it is the proxy-coupled CCR plane (response_handler 896/context_tracker 660/batch_store 313) that dies at the proxy rebuild anyway → genuinely-free non-proxy residual is only ~1.3-1.7k (config.py deadflags ~250, utils.py 13 orphans 110, get_siglip 60, Rust test-only fns ~70, dup verify/measure.py 879=Tier-2). Big numbers (cache-optimizer 2.5k, anchor_selector 770, code_compressor 2k, embedding scorers 533) = TIER-3 PUBLIC SURFACE → API-deprecation+version-bump, NOT free deletes (~6.4k). v4 EARNED ITS KEEP: (a) 2 deptry FALSE-POSITIVES confirmed LIVE — KompressCompressor (ML fallback, torch optional) + ComponentTracker (compression_store+batch_store import it); (b) ★ csv_schema_decoder.py TRAP — 591 LOC looks dead (0 prod callers) but tests/test_ccr_recovery_invariant.py:36 + verify/ import decode_csv_schema_rows to PROVE byte-exact recovery → MUST NOT CUT (I spot-confirmed both). Apply scope (when reached): small config/utils/Rust-test-fn deadflags + the dup measure.py; proxy-coupled CCR plane rides the rebuild; surface-tail is a separate deprecation decision.
- APPLY of v4 cuts is OPTIONAL/SMALL and sequenced per the roadmap (user prioritized test-hardening next). Report-only until user says apply.
- ✅ v4 TIER-1 APPLY DONE (2026-06-23): teammate a609285 cut ~3,900 LOC (18 commits, ef3776e5→5f0ce4b3). ORCHESTRATOR added component_tracker.py (388, TRANSITIVE ORPHAN created by the batch_store cut — 0 importers after) at 2c525cc0. TOTAL THIS ROUND ~4,288. Re-verified independently: pytest 509/31 (519→509 = 10 intentionally-archived compression_units dead tests, 0 live regress), surface 54 (56→54 intentional: −HeadroomMode −CacheOptimizerConfig; models 4→3 −get_siglip), recovery 21/21, cargo 0-failed, compress OK. Tree clean.
  KEY: response_handler.py loud-CCR-miss invariant test was MIGRATED to the live HeadroomMCPServer._retrieve_content path (not dropped) — verified test_ccr_eviction_loud_miss 5/5 green. KEPT (teammate judgment): Rust ccr::compute_key + marker_for (test-only but guard CCR-backend round-trip + marker-format-pinned invariants; no live keying fn to redirect to). Self-inflicted RED (over-deleted StubScorer) caught by cargo + fixed.
  CUMULATIVE all rounds (Tier-1+2+v4): ~13,900 code LOC removed, engine GREEN throughout, fully reversible in archive/.
- ✅ TIER-3 CUT DONE (2026-06-23): teammate a609285 cut 10/11 rows (HEAD 37d440e1) + orchestrator removed inert enable_code_aware flag (f0f12993). Surface 54→40. Re-verified: pytest 443 passed/0 failed/14 skipped (509→443 drop = archived cut-feature tests, ZERO live regression), recovery 21, compress OK, cargo 0-failed. LIVE split-halves all survived (CacheConfig, TokenCounter, relevance base+bm25, csv_schema TRAP, KompressCompressor — all import OK). Row 9 PARTIAL: RelevanceScorerConfig RETAINED (LIVE field SmartCrusherConfig.relevance:269 — removing broke item-2 RED; belongs to Tier-2 SmartCrusherConfig work, not surface cut). code_compressor untangle: content_router CODE_AWARE branch collapsed to KOMPRESS fallback (live invariant code→KOMPRESS 20/20). Current tree = 39,171 code LOC (Py 16,089 + Rust 23,082).
- ★ VERSION BUMP DEFERRED (not done): I was going to bump pyproject 0.25→0.26, but the architecture-clarification turn revealed this is the USER'S FORK (not maintainer) + collaboration-pending + deliverable-shape undecided → unilateral version bump is premature. Version scheme ties to the deliverable-shape decision. Flagged, not executed. Docs still reference retired [code] extra (README:102,231, CHANGELOG:525, wiki/*) — left per docs carve-out.
- ★★ NEW CONTEXT (2026-06-23, reframes everything): user is NOT a headroom maintainer — FORKED the OSS repo, found it over-engineered, building a MINIMALISTIC hook+MCP alternative to the proxy (hook=lazy-dev data-plane, MCP=on-demand full-output retrieve). END GOAL: present to headroom devs for COLLABORATION — both methods side-by-side (their proxy + user's hook/MCP), either as a mentioned repo in their README or two solutions in one repo. So proxy is NOT "deferred for rebuild" — it's DORMANT for the user's method.
- ★ DORMANT PROXY-ROUTE FOOTPRINT (measured 2026-06-23): ~2,850 code LOC is proxy-route, dormant for hook/MCP: (1) PURE proxy transport = headroom/proxy/ 1,847 (helpers.py SSE/streaming/interception 2931-raw + auth_mode.py 262) — deletable now; (2) PROXY-PURPOSE policy layer ~1,010 woven into live engine = compression_policy.py 276 + compression_policy.rs 484 + auth_mode.rs 250 — SmartCrusher reads a per-request compression_policy from kwargs that ONLY THE PROXY feeds; for hook/MCP it's None → default pass-through → auth-mode branches dormant, refactor-to-remove (NOT clean delete); (3) mcp_server _retrieve_via_proxy + httpx tendril (tens of LOC) — un-couple, mcp_server stays. KEY REFRAME: the "Py↔Rust auth-mode parity invariant" I'd protected as untouchable IS ITSELF proxy-route (parity only matters when the proxy applies policy in both langs); never running proxy → that invariant is moot.
- ★ DELIVERABLE-SHAPE DECISION (pending, governs proxy-delete + version): (a) standalone minimalistic fork → DELETE the ~2,850 proxy-route (lighter); (b) side-by-side in their repo (collab vision) → KEEP proxy/ as THEIR method, just don't wire it into the user's hook/MCP path. This decides whether proxy/ is deleted + the version scheme.

  (prev) ★ TIER-3 CUT RAN (2026-06-23, user decided "kör" — go internal-engine-only, retire the published-library API; breaking change ACCEPTED): teammate a609285 cutting ALL 11 Tier-3 rows (~6.4k). KEEP-THE-LIVE-PART splits flagged: cache/base.py keep CacheConfig(69)/cut BaseCacheOptimizer(254); providers/base.py keep TokenCounter(18)/cut Provider(34)+retype pipeline param; relevance keep base+bm25/cut embedding+hybrid. code_compressor cut TOGETHER w/ compression_summary + content_router CODE_AWARE branch untangle. User reasoned it through: Tier-3 = old proxy-arch + dead Rust-duplicated Python, ZERO connection to the MCP/hook future (retrieve=CCR store, set_compression=RoutingPolicy, hook=ContentRouter — all kept). ON COMPLETE: re-verify gates + ★ I do pyproject version bump (0.25.0 → breaking) + CHANGELOG note (published-API removal).
  (prev) ★ TIER-3 SURFACE DECISION (was pending): ~6.4k unused PUBLIC exports (anchor_selector 770, cache-optimizer cluster 2.5k, code_compressor 2k, embedding scorers 533, compression_cache 315, Provider ABC, observability Protocol). CRITICAL: headroom-ai is a PUBLISHED PyPI package (pyproject v0.25.0, name=headroom-ai, docs site headroom-docs.vercel.app) → these are PUBLIC API contract, cutting = breaking change for pip-install users (NOT internal dead-code). No external consumer found in Biljakten (uses only headroom.transforms via hook). DECISION = keep published-library identity (KEEP surface) vs go internal-engine-only via hook+MCP (free to slim, major version bump). Asked user.
- STEP 4 (NEW, user-prioritized): HARDEN TESTS. Iterate suite to best QUALITY + coverage (coverage = FLOOR not goal). Use installed `test-quality-tools:test-quality` skill + scorecard: mutation-resistance, anti-fragility rules, boundary tests for every </<=/>/>=/==/!=, real-I/O fixtures over mocks, contract-named tests; iterate-to-plateau per module. REASONING (user): better tests → find+improve codebase → plausibly BETTER COMPRESSION (the eval `break` pass already surfaced silent-loss holes this way). Hit the hard-invariant surfaces first (CCR recovery, Py↔Rust parity, cache ordering, compress() route).
  ★ SKILL LIMITATIONS (verified by reading the skill files 2026-06-21 — set expectations, do NOT expect "perfect"):
    (1) Stops at PLATEAU, not perfection — 3 dry rounds OR zero-violations+all-boundaries-tested; the dry-rounds arm usually
        fires first → stops when the MODEL can't improve further. "Quality" is relative (better-than-start), not absolute.
    (2) Best axes (boundary B.2 / real-I/O B.3 / contract-naming E.3 / tautology A.3) are MODEL JUDGEMENT, not auto-counted.
    (3) NO real mutation engine — "mutation-resistance" is a proxy (boundary + pinned literals + break-the-behavior gate). Monotonic (revert-on-no-gain, never degrades).
    (4) ★ NO RUST PROFILE in score.py (python/js/go/kotlin/swift only). ~Half the codebase (Rust 23k) gets ZERO auto-scoring; Python is the only validated profile.
  TWO-TRACK because of the Rust gap: Python → skill + score.py loop; Rust → apply the contract principles by hand (+ optional `cargo-mutants` for a real mutation score on the engine core).
- STEP 5 (DEFERRED until codebase beyond-perfect): PROXY→HOOK+MCP REBUILD. proxy→DELETE; extract live SSE utils (proxy/helpers.py parse_sse_events/safe_decode → ccr/sse_parser.py) FIRST; build hook (data-plane, productized Biljakten-style) + 2-tool fastmcp (set_compression + retrieve→CCR direct, un-couple mcp_server.py:472 _retrieve_via_proxy). Engine gates BLIND to transport → needs NEW functional verification (not the archive loop). DO NOT START until Step 4 done.

(historical) Phase-2 brief sent to teammate a609285 via SendMessage.
★ ORCHESTRATOR RECON REFUTED v2-audit's Tier-2 Python (~6.6k "cuttable" is mostly LIVE/import-woven — v1 was right to defer):
  - LIVE (will restore): relevance/bm25.py+base.py (compression_store:258 instantiates BM25Scorer), compression_feedback.py (CCR feedback-loop, heavy use in compression_store), cache-optimizer cluster anthropic/openai/google/registry (woven into cache/__init__ 56-surface + tokenizers/__init__→registry; likely test-pinned).
  - VESTIGIAL CANDIDATES (0 runtime caller found, gate-verifiable): relevance/embedding.py+hybrid.py (smart_crusher uses RUST HybridScorer crate not these), ccr/batch_processor.py (BatchProcessor never instantiated, only ccr/__init__:24 re-export), models/ml_models.py + cache/dynamic_detector.py (no compress-path caller; ml_models lazy-imported inside dynamic_detector funcs).
  - Realistic Python yield ~1-2k, NOT 6.6k. Real Tier-2 win = Rust live_zone bundle ~3.3k.
★ TEAMMATE TIER-2 SCOPE: (A) Rust live_zone.rs 2,899 + recommendations.rs 329 + lib.rs FFI ~94 + mod.rs re-exports + co-located cargo tests (archive live_zone+recommendations together; private AuthMode≠canonical src/auth_mode.rs which is KEPT; hoist only if cargo demands). live_zone is proxy-only → cutting it makes proxy/ Python reference a removed FFI = EXPECTED/OK (proxy deleted next step, archived). (B) Python cluster attempt-all in order embedding/hybrid→batch_processor→ml_models/dynamic_detector→cache-optimizer cluster.
★ NEW HARDENING vs Tier-1 (gate-blindness guard): for every GREEN cut, grep whole tree (excl archive/) for stray refs before commit — pytest doesn't cover every path; stray ref → restore.
★ MUST-KEEP: ccr/mcp_server.py (MCP retrieve plane), relevance/bm25+base, compression_feedback (if red), proxy/ Python files (defer — but DO remove their Rust FFI per bundle A).
ON TEAMMATE COMPLETE: re-verify gates myself, review KEPT(live) section, then user gives PROXY-REBUILD instructions (step 3): proxy→hook(data-plane)+2-tool fastmcp(set_compression+retrieve→CCR direct, un-couple mcp_server.py:472 _retrieve_via_proxy). Extract live SSE utils (proxy/helpers.py parse_sse_events/safe_decode → ccr/sse_parser.py) BEFORE deleting proxy.

(prev) AWAITED USER Tier-2 instructions (proxy→hook+MCP rebuild + untangle cuts). Reuse same teammate via SendMessage to:'a609285ce32cd6df9'.

--- (historical, Phase 1 while-running notes below) ---
## BLOAT REMOVAL — PHASE 1 RAN (2026-06-20)
Sonnet teammate (background Agent, name=tier1-cutter) ran Tier-1 cleanup. Method = proven archive+2-gate loop:
archive (git mv → archive/) → rebuild if Rust (maturin) → gates (cargo test + pytest 519/31 + surface-walk 56 +
recovery 21/21 + compress() round-trip) → green=keep-removed+commit / red=dig deeper (fixable small refactor? do it;
genuinely live? restore + report caveat). Sequential, one item at a time, commit per green batch.
- TEAMMATE TIER-1 SCOPE (EXCLUDES proxy — proxy handled by the hook+MCP rebuild in Tier 2): Rust pipeline/ subtree
  (4,212, drop mod.rs:32,59-63 re-exports) + safety.rs (215, drop mod.rs:34,65); Python conftest dead fixtures (193,
  incl nonexistent providers.openai import), commented-out code compression_store.py:1097, stale JSON results
  (verify/*raw_results.json + benchmarks/*_results.json ~1.45MB), empty phantom dirs memory/{adapters,backends}.
- MUST-NOT-TOUCH (verified live/invariant): ALL proxy/ (rebuild later), telemetry/, onnx_runtime.py, auth_mode.rs,
  compression_policy.rs, SmartCrusher (crusher/compaction/analyzer/planning/orchestration/anchor_selector), ccr/,
  tokenizer*, cache_control.rs, log/diff/search_compressor.rs (live via pyclass — dup hypothesis REFUTED), wiki/,
  feature-gated rust, live_zone.rs + recommendations.rs (those are Tier 2). NO complexity-lint churn.
- ON TEAMMATE COMPLETE: review his report (esp. anything he KEPT + caveats), then user gives Tier-2 instructions
  (which include the proxy→hook+MCP rebuild). Reuse the SAME teammate via SendMessage for Tier 2.
