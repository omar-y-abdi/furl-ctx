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
## BLOAT REMOVAL — PHASE 2 (TIER-2 CUTS) RUNNING (2026-06-20)
User: "allting kapas nu" — full Tier-2 cut scope via same archive+5-gate loop (empirical > my analytical recon; gate sorts live/dead, restore-on-red). Proxy→hook+MCP REBUILD deferred to its OWN later step (user: "proxy-ombygge sen, granska mellan stegen"). Teammate a609285 resumed via SendMessage with the brief.
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
