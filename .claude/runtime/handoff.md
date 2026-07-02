# 🔷 HANDOFF — Headroom autonomous COR burn-down + PR #5 (2026-07-02; main `5cc4c131`+, pushed)

> Rewritten fresh (context was critical). **Full cycle-1..6 history recoverable via `git show HEAD:.claude/runtime/handoff.md` on any commit before this one.** This doc = the live state + every standing instruction so nothing is re-explained.

## 0. SESSION-WIDE MODES (persist verbatim, every response)
- **Swedish** for all prose. **Caveman ultra** (terse; drop articles/filler/pleasantries/hedging; fragments OK; **code/commits/PRs/security written NORMALLY**). **Lazy-dev full** (deletion>addition, shortest diff). **Ultracode on**. **Fast mode**. Model: **Opus 4.8 (1M ctx)**.
- Response protocol: first line `WORKFLOW: [Q&A|INVESTIGATION|DEFAULT|CONSENSUS] | Reason | Following: .claude/workflow/X.md`. `AskUserQuestion` on genuine forks — EXCEPT while user asleep (see §2 autonomy).
- Advisor tool: consult at inflection points (before committing to an approach, when stuck, before declaring done). It caught a confirmation-bias trap once — give it weight.

## 1. THE MANDATE — execute the ENTIRE `FABLE-RECON-PLAN.md` to zero-material
- Master plan = `FABLE-RECON-PLAN.md` (repo root, tracked): 189 findings (COR/SEC/ARCH/TYPE/PERF/TEST/DOC/API/SIMP/EFF) + §4.1/§4.2 refactor blueprints + §5 12 owner-decisions + §2 9-phase roadmap + §7 second-pass (COR-42..56). North star: codebase "beyond perfect for USAGE" before MCP tool creation.
- Process: RECON→consolidate→BATCH-FIX→DEEP re-recon VERIFY→loop until zero-material. "A flaw the workflow can still find = PM's fault." 200-agent `adversarial-critique.js` workflow = CONFIRMATION only, never discovery.
- **PM DISCIPLINE (non-negotiable):** PM does ZERO IC work. PM ONLY: **spec → review-diff → gate → commit → push**. Write to files SPARINGLY/surgically (PLAN.md / handoff.md / QUESTIONS + tiny cleanups of agent output only). Edit-agent times out → re-dispatch TIGHTER, do NOT drop into IC mode.

## 2. EXECUTION MODEL — fable = sole worker for COR/PERF/ARCH (user 2026-07-02)
- **fable owns COR + PERF + ARCH clusters** (incl. §4.1 ContentRouter decomp + §4.2 CCR-typed-FFI-refs). opus/sonnet for security-adjacent + peripheral; haiku for trivial.
- **FRESH fable per connected-file group** — NOT one reused session (its transcript bloats to ~1M tok → stream-idle-timeouts). **Light specs** (point fable to §3.1/§7.1, let it read the exact Fix). **Combine connected files in one agent.** Fable is flagship-rigorous: catches my spec errors, verifies consumers, honest bench attribution — trust-but-verify.
- **🔒 SECURITY EXCLUSION (user, CRITICAL):** fable's safeguards flag security/vulnerability/telemetry work. **PRE-FILTER every fable batch so fable never even READS a security finding.** Route to **opus/sonnet** instead: entire SEC cluster; COR-7-type (panic/FFI/DoS); COR-21 (thread-safety/leak); anything touching `mcp_server.py` / `compression_store.py` / `html_extractor.py`; PERF-6 (MD5/weak-hash); all telemetry/TOIN/collector/beacon/tool_injection/redaction/jail/secrets. Fable also told to STOP+flag if a finding turns security-adjacent (backstop).
- **AUTONOMY (user asleep 2026-07-02):** run autonomously to completion. **NO AskUserQuestion until user returns + messages.** Owner-decisions → append to `QUESTIONS-FOR-USER.md` (defer that item, proceed with everything else). On return → fire AskUserQuestion for all accumulated.

## 3. GATE + COMMIT DISCIPLINE (every wave)
- **Gate:** `.claude/runtime/gate.sh bench` = G1 `cargo test -p headroom-core` · G2 full pytest · G3 surface-walk · G4 recovery (MUST be 23) · G5 `run_bench`+`floor_check` (needle 100%). All exit-code keyed.
- **PM loop:** review fable's diff (spot-check the semantic core) → `gate.sh bench` → commit-in-groups → `git push origin HEAD:main`.
- **Commit hygiene:** `git checkout HEAD -- uv.lock` FIRST (uv ops truncate it). `git add <specific files>` (NEVER `-A`; `-u` only when tree is provably all-one-change). Guard: `git diff --cached --name-only | grep -iE 'uv\.lock|recon-findings|codebase-CRITIQUE|QUESTIONS-FOR-USER|raw_results|BASELINE\.md|baseline_results'` → abort if matched. **EXCEPTION:** a DELIBERATE re-baseline DOES commit `benchmarks/BASELINE.md`+`baseline_results.json` (don't guard them then).
- **gate.sh line-63 side-effect:** it runs `git checkout HEAD -- benchmarks/baseline_results.json BASELINE.md` after G5 → reverts them to HEAD. On a re-baseline commit/merge, RESTORE the intended baseline (`git checkout <src> -- benchmarks/...`) AFTER gating, before committing.
- **Rust:** `maturin develop --uv` (or `.venv/bin/maturin develop`) before pytest — **`scripts/build_rust_extension.sh` is BROKEN in the uv venv (no pip)**; agents use `--uv`. `cargo fmt --check` before every Rust commit (PM re-checks + `cargo fmt` if drift). Wire-contract/parity: Rust+Python+tests in one commit for dual-impl/marker-byte changes.
- **Subagent dispatch:** `Agent`, `general-purpose`, `run_in_background:true`, `model:` per tier. Do NOT tail the output JSONL (context overflow). VERIFY claims (review diff) before committing.
- **Hard invariants (never break silently):** CCR recovery 100% byte-exact · Python↔Rust hash parity · prompt-cache prefix ordering.
- **Git safety:** path-scoped only; NO `reset --hard`/`--force`/`-f`/`clean -f`/`stash`-without-path/`checkout .`/branch-switch-without-path/force-push. Allowed: `git checkout HEAD -- <path>`, `reset --soft`, `merge --abort`.
- **Repo:** private `github omar-y-abdi/headroom-mcp`; local branch `verify/phase2-audit-report` → origin/main.

## 4. DONE (all gate G1-G5 green + pushed)
- **Phase 0 COMPLETE:** COR-1/2 decoder `f1c44778` · gate-honesty `c5b58ca8` · COR-3+TEST-26 `7988bb5c` · TEST-4 bench-out `4ffd2541` · TEST-5/6 anti-vacuity `88f0e578` · TEST-27 lint pin `566f3449` · 0.4 re-baseline `590391a4` · honest README `d773285b`.
- **Phase-1 clear-correctness COMPLETE:** COR-4/20 CCR chunk-flood `a6344a2f` · COR-8/9 tag_protector `3457b905` · COR-19 walker `7e22da05` · COR-6/11/12 kompress `65a9aeeb`(+dedup `42e3476c`) · COR-15 `ba3e954b`(+fmt `a11167de`) · COR-13 decoder-verifiable `4f622cd6` · COR-5 fail-open `625fd108`.
- **COR-7** FFI panic containment `b4c70d0a` (opus, security-excluded).
- **Batch-2:** G1 COR-25/26/27 `93467935` · G2 COR-23/24 `e14a8d4e` · G3 COR-28/33/45 `fc5d03c3` · G4 COR-22/29 `e3bd0f6b` · G7 COR-42/52 `820fd6de`(+multiturn re-baseline). PLAN checkpoints between.
- **⚡ PR #5 MERGED `fb9c27e0`** (user-commissioned parallel fable web session; I verified EMPIRICALLY — local `merge --no-commit` + gate G1-G5 GREEN on merged tree, my COR assertions survived). CCR_OFFLOAD strategy: large uncompressible content (≥4000 chars, ratio ≥0.9) → byte-exact CCR store + preview+marker. **Corpus 36%→95%, code@7 0%→98.9%, multiturn 71%→87%(→85.1% after COR-52), all 100% retention + needle-100%.** Honest lossy-CCR framing (agent file-reads excluded, is_error verbatim, needs ccr_enabled). New `baseline_results.json` is the floor (code 471 / multiturn 2211). Also fixes: strict marker pinning (0 vs 21 FP), error-protection JSON exclusion (≈COR-16), CI perms.

## 5. RESUME PLAN (on main `5cc4c131`+)
- **⏳ FIRST: commit G8/COR-53** — fable finished it; edits sit UNCOMMITTED in the tree (`headroom/transforms/cache_aligner.py` + `content_router.py` `_detect_analysis_intent` + `compress.py`/`pipeline.py` docstrings + new `headroom/utils.py::concat_text_parts` + `tests/test_cache_aligner_block_format.py`). CacheAligner block-format awareness; **bench 0-delta** (behavior-invisible on corpus), 836 pytest passed, ruff/mypy clean. Nothing security-adjacent. → review diff + `gate.sh bench` + commit + push.
- **REMAINING fable groups (fresh fable, light spec):** G6 compress.py 43/46/49/50 (⚠ 46/49 touch content_router — re-assess post-PR#5) · G9 tokenizers 40 · G10 misc 41 · G11 serde 44 · **G5-reassess** 16/17/18/30/31/39/47/48 (content_router changed by PR#5 AND by G8's `_detect_analysis_intent` edit — **G5a was STOPPED** at the PR#5 merge; COR-16 ≈ covered; COR-17 word→token + COR-18 cache-key likely still needed — re-verify each against the NEW content_router.py).
- **Security-adjacent → opus/sonnet (NEVER fable):** COR-21 (router_cache DoS/leak) · 32/36/51 (mcp_server) · 37/54/56 (compression_store) · 38 (html_extractor). Then SEC cluster (§3.2) + telemetry/TOIN/injection excision (Phase-3 SIMP-1/3/4 — needs §5 decisions).
- **Owner-decisions → AskUserQuestion when user returns** = `QUESTIONS-FOR-USER.md` (14 items): §5's 12 (telemetry delete-vs-shrink, Bash COR-10, code-strategy EFF-2, dotted-flatten COR-14, content_detector-mirror, exception-contract, etc.) + Q13 README-multiturn sync (87→85, trivial) + Q14 code-99%-framing (informational).
- **Mid-run follow-up defects (logged, non-blocking):** (i) mixed-array `_dup_count` index-matching drops stamped rows (recoverable via outer sentinel; COR-33 reduces); (ii) COR-13 full Buckets/Nested wire coverage; (iii) walker.rs:126 4th lossless-accept site; (iv) `build_rust_extension.sh` uv-venv fix for ci-precheck.
- **After COR:** PERF (§3.5→fable, PERF-6→opus) · ARCH (§3.3 incl §4.1+§4.2→fable) · TEST/DOC/API/TYPE/SIMP (opus/sonnet/haiku) · §5 decisions gate Phase-3 Great Excision → then MCP tool creation.
- **Key files:** `PLAN.md` (live tracker) · `QUESTIONS-FOR-USER.md` (owner-decisions) · `FABLE-RECON-PLAN.md` (master, tracked) · `recon-findings.md` (round-5 history, UNTRACKED — never commit) · `codebase-CRITIQUE.md` (UNTRACKED — never commit).

## 6. Settings note (2026-07-02)
Global `~/.claude/settings.json`: auto-compact key is **`autoCompactWindow`** (int 100k-1M) + `autoCompactEnabled` — NOT `autoCompact`/`autoCompactThreshold` (invalid, silently rejected). Set to `autoCompactWindow: 600000`.

## 7. LIFTED EXCLUSION + GREAT EXCISION IN PROGRESS (2026-07-02 latest — SUPERSEDES §2 exclusion + §5 resume)
- **🔓 SECURITY-EXCLUSION LIFTED (user):** fable survived MCP/store (security-adjacent) where sonnet×2+opus stream-idle-DIED. **fable = DEFAULT for ALL tough jobs** (SEC cluster, security-adjacent COR, excision, PERF/ARCH). **opus = FALLBACK ONLY if fable safeguard-FLAGS** (NOT for timeouts → re-dispatch/foreground). Reuse running fable via SendMessage before respawn. No more pre-filtering security from fable.
- **Liveness:** output-file mtime is NOT a liveness signal (fable ran 137k tokens w/ "stale" mtime). Trust ONLY the completion/timeout notification.
- **DONE this session (gate G1-G5 green + pushed):** Wave-1 COR-40/44/46/49/50 (`18da4e93`/`ced9aa6a`/`9b907ba6`) · Wave-2 content_router COR-17/18/30/31/39/47/48 (`2ae3f6a2`) · MCP/store COR-32/51/54/56 (`f9e8d5be`).
- **🔥 GREAT EXCISION (MAXIMAL-LEAN/SEMI-NUCLEAR).** RULE = delete anything whose removal keeps {6-dataset floor + needle-100% + recovery-23 + MCP-hook smoke} GREEN; used⇒stays; uncertain⇒keep+flag. Per-chunk fable→gate→commit:
  - ✅ C1 Kompress ML `096eee54` (-1.7k; +RouterRuntime/kompress_model/target_ratio removed)
  - ✅ C2 HTML `5d408305` (COR-38 moot)
  - ✅ C3 Telemetry/TOIN/feedback `5922be28` (-5285; SEC-2/3+PERF-9/10+COR-37/41-TOIN moot; KEPT local mcp stats = COR-36)
  - ✅ C4 Tokenizers→tiktoken-only `01826d63` (-967; huggingface.py+mistral.py)
  - ⏭ **C5 Rust dead-weight (RUNNING):** content_detector.rs parity mirror (SIMP-6) + Rust HF tokenizer (tokenizer/hf_impl.rs/registry.rs + tokenizers/hf-hub Cargo deps) + inert Rust TOIN fields (config.rs/hashing.rs/error_keywords.rs, FFI: pyo3 lib.rs:514 kwargs). cargo test+maturin+parity.
  - ⏭ C6 ast-grep (code-path + `ast-grep-cli` dep, accept 0% code). C7 semi-nuclear sweep (signals/relevance if dead, tag_protector.py orphan [wraps live Rust — verify], archive/). C8 doc/CI honesty (README Kompress/HTML, llms.txt HEADROOM_TELEMETRY, conftest HF).
- **ciFix agent** (sonnet, running): fixing failing "Release Please" GH workflow (spamming user) + dead ci.yml HF prefetch, .github-scoped.
- **Remaining COR after excision (→fable):** COR-16 (analysis-intent keyword trim), COR-21 (router_cache DoS), COR-36 (mcp local-stats result.error honesty). Then SEC §3.2 + PERF/ARCH/TEST/DOC/API/SIMP.
- **Owner-Q resolved by excision:** Q1/Q3/Q4/Q6. QUESTIONS-FOR-USER.md remaining: Q2/5/7/8/9/10/11/12/13/14.
