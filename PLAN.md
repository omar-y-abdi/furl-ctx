# PLAN — Cycle-6: recon → batch-fix → DEEP re-recon → confirm (LIVE progress report)

> This is the **working progress report** for the effort in flight. Tag each step
> with its commit as it lands; NEVER delete this file while work is in progress.
> Broader context / decisions / lessons live in `handoff.md`; the master defect
> list + per-finding detail live in `recon-findings.md`. The three work in tandem.

**North star (user):** codebase "beyond perfect for USAGE" before MCP tool creation.
**Process (user mandate):** RECON FIRST (deep opus agents find everything) → CONSOLIDATE
to one master list → BATCH-FIX informed → VERIFY = DEEP RE-RECON AGAIN (fresh agents) →
loop until zero MATERIAL → 200-agent workflow = CONFIRMATION only. A flaw the workflow can
still find = PM's fault.

**PM DISCIPLINE (user mandate 2026-07-01 — non-negotiable):** You are the PM. DELEGATE +
VERIFY; do NOT execute. Round-4 recon = fresh subagents (not you reading files). Batch-fixes
= edit-only subagents (not you making 30 edits). You ONLY: spec the work → review findings →
gate → commit. Write to files SPARINGLY + SURGICALLY. If an edit-agent times out, re-dispatch
a TIGHTER-scoped agent — do NOT drop into IC mode and do it yourself (that failure burned a
whole context window in cycle-6). Your context = orchestration, never execution.

---

## Round 1 — recon + batch-fix — DONE
- [x] Recon round 1 (3 opus agents + 2 sub-sweeps) → master list in `recon-findings.md`.
- [x] P0 CCR silent-loss veto (diff/log/search bool-veto + passthrough) — `a341bf4f`
- [x] P3+P4 delete dead ML/embedding subsystem (~1400 LOC) — `6aba33b3`
- [x] P1py + P5 Python doc-lies + pyproject + dead retrieval_endpoint — `30c8742d`
- [x] Rust dead-deps + core hash doc-lies (blake3→sha256[:6], sha2 [:8]) — `df173f5a`
- [x] B SQLite/Redis CCR backend deletion (−590 LOC) — `4c0b1b6d`
- [x] god-object step 1 (delete dead eager_load_compressors + route_and_compress) — `68da6f03`
- [x] perf: compute content detection once per compress — `47dce416`
- [x] god-object step 2 (content-block de-dup → `_compress_content_block`) — `924ce8cc`
- [x] P7 csv phantom-row guard + compress() reject-unknown-kwargs — `4551c0a1`
- [x] P7 cache_aligner stateless (kill cross-request latch) — `34cf1bbc`
- [x] router contained (ContentType total-fn + Optional sigs + drop dead cache_hit) — `091b319f`
- [x] mcp jail + size caps (partial — redaction finished in round 2) — `163b131c`
- [x] P8 Python test-strengthening (weak-assert → content-equality) — `b745e9b0`

## Round 1.5 — BATCH-3 — DONE
- [x] god-object site-3 pins (BOTH string + content-block cache-lookup copies) + banking doc — `591a897f`
- [x] SQLite residue (in_memory.rs doc-lie) — `1a78e6b4`
- [x] TokenCounter Protocol unify + fold `headroom/providers/` — `9eb82351`
- [x] P8 crusher absolute-saved-floor boundary predicates + tests — `ee847a30`

## Round 2 — VERIFY re-recon (3 fresh opus agents) + batch-fix — DONE
- [x] Re-recon round 2 (correctness / cleanliness / architecture, diff-weighted `a341bf4f..HEAD`) → `recon-findings.md` ROUND-2.
- [x] **CRIT: gate G2 was hollow** — pytest-timeout dropped by an env recreation → `--timeout` unrecognized → pytest bailed before running any test → grep-on-`tail` matched nothing → false "PASS" all session. Fixed: G1+G2 key on EXIT CODE now; the `force_kompress` test it masked fixed; pytest-timeout reinstalled. — `53bbd96c`
- [x] Docs excision (README/llms.txt/RUST_DEV.md/SECURITY.md/CONTRIBUTING.md + redis docstring) — user chose EXCISE — `7d977a07`
- [x] paths.py dead-code (−213 LOC, 0-caller verified) + CODEBASE-MAP drift + Cargo comment — `0841826a`
- [x] mcp log redaction + jail fd-pin (TOCTOU/hardlink) + 3 test strengthenings — `f1119fce`

## Site-3 lookup-half extraction — DONE (`92088258`); cycle-6 tail below
Outcome table (verified vs code + advisor — byte-identical route_counts across both paths):
| outcome | route_counts effect | action |
|---|---|---|
| Tier-1 skip-hit | `ratio_too_high`+`cache_hit` | ServeOriginal |
| Tier-2 tightened→skip | `move_to_skip`; `ratio_too_high`+`cache_hit` | ServeOriginal |
| Tier-2 live CCR-backed | `cache_hit` | ServeCached(compressed,strategy,ratio) |
| Tier-2 unbacked sentinel | `invalidate`; `cache_stale_recompute`+`cache_miss` | Recompute |
| plain miss | `cache_miss` | Recompute |

Divergence blocking full merge = format (`router:{strat}:{ratio}` flat vs `router:{label}:{strat}` threaded) + recompute mechanism (deferred pending_tasks vs inline). Lookup-half extracts clean.

Exec steps (advisor-refined) — extraction DONE `92088258`:
- [x] S1 — characterization net FIRST: `_CapturingObserver` (BOTH methods; `record_compression` no-op) + `TestCacheLookupRouteCounts` (skip-hit/tightened/serve-cached × string+block) asserting EXACT route_counts deltas. Ran on CURRENT code → green (proved pins reflect real behavior). — `92088258`
- [x] S2 — ADT near RouterRuntime: `ServeOriginal | ServeCached(compressed,strategy,ratio) | Recompute`; singletons `_SERVE_ORIGINAL`/`_RECOMPUTE`. — `92088258`
- [x] S3 — extract `_lookup_cached_disposition(content_key, context, min_ratio, route_counts) -> CacheDisposition`; ALL effects inside (bumps, move_to_skip, invalidate, `_ensure_ccr_backed`, stale bumps BOTH stale_recompute+cache_miss). — `92088258`
- [x] S4 — rewire block site (`_compress_content_block`) via `match`; moved bumps DELETED; gate green. — `92088258`
- [x] S5 — rewire string site (`apply`) via `match`; moved bumps DELETED; non-merge comment rewritten (lookup IS shared now); gate green. — `92088258`
- [x] S6 — DIRECT unit test of `_lookup_cached_disposition` (all 5 outcomes + route_counts=None, no compression) = architectural guard. Full gate → committed. — `92088258`
- [x] S8 — final `gate.sh bench` → G1-G5 PASS, floor needle 100%, ratios untouched (dispatch-only change confirmed).
- [x] S7 — complete ML→Kompress rename (user greenlit "gör nu") — `1d8a69e9`. All 3 holdouts in lockstep: method `_try_ml_compressor`→`_try_kompress`, LIVE dispatcher param `try_ml_compressor`→`try_kompress` (+ delegator closure/kwarg + call sites), type alias `_TryMlCompressor`→`_TryKompress`. CODEBASE-MAP refs updated; historical audit docs left as dated records. Pure identifier rename, zero behavior change. Gate G1-G4 green, 750 passed.
- [ ] **Round-3 re-recon** — diff-weighted on the round-2 batch → confirm zero MATERIAL (by-design/nitpick don't count). Loop if material.
- [ ] **200-agent confirmation workflow** — ONLY when re-recon confirms beyond-perfect. `adversarial-critique.js`, `args.map=CODEBASE-MAP.md`. Confirmation, never discovery.

## Notes / residual (re-recon arbitrates — not reactive-fix)
- `exceptions.py` StorageError exported-but-never-raised.
- README accuracy-table (GSM8K/TruthfulQA/SQuAD/BFCL) numbers not backed by any in-repo file.
- RTK-binary-shipping claim (README attribution) unconfirmed in the live package.
- RUST_DEV.md pre-commit bullet references absent `scripts/sync-plugin-versions.py`.

## Current position — FABLE-RECON CAMPAIGN (supersedes round-5 framing)

**Master plan: `FABLE-RECON-PLAN.md`** (repo root, committed to main) — fable full-tree audit: 189 findings (3 critical · 31 high · 90 medium · 65 low) + 2 refactor blueprints (§4.1 ContentRouter decomposition, §4.2 typed-CCR-refs) + §5's 15 owner decisions + 9-phase roadmap (§2). All execution follows FABLE-RECON-PLAN's roadmap.

**Execution model (user 2026-07-02, refined — 4-tier):** PM does ZERO IC work; only spec → review → gate → commit → merge to main + push to remote. Difficulty → model routing, ALL local subagents:
- **tough → fable** — the 2 wire-contract refactors (§4.1/§4.2), the hardest CCR/FFI-typing items. (PROVEN: COR-1/COR-2 dry-run = rigorous RED→GREEN, byte-exact, self-verified.)
- **hard → opus** — complex correctness/security, multi-file changes, the Great Excision coordination.
- **medium → sonnet** — standard fixes, test hardening, moderate refactors.
- **easy only → haiku** — lint, doc sweeps, dead-code deletion, mechanical one-liners.

**Phase 0 — wave 1 DONE + pushed (HEAD `f1c44778` on main):**
- `c5b58ca8` — gate honesty: TEST-1 G4 keyed on exit code not grep; TEST-2 G5 captures run_bench exit code + floor_check rejects stale captured_at; portable cd. RED-proofed. (Also bundled FABLE-RECON-PLAN.md.)
- `f1c44778` — COR-1/COR-2 critical lossless-decoder data-loss (tables decoded to 0 rows; re.DOTALL header + _unq head-dict cell; fuzz generator hardened to stamp both shapes). RED 0/60 → GREEN 60/60, 762 passed, gate G1-G4 green.

**Phase 0 — wave 2a DONE + pushed (origin/main `88f0e578`), gate G1-G5 PASS:**
- `7988bb5c` verify — COR-3 (repoint search generator to committed `express_rg`; `slugify_rg` was gitignored+never committed → clean-checkout crash) + TEST-26 (DEV_CLAIMS `multiturn@90` auto-flag; degradations 5→7)
- `4ffd2541` bench — TEST-4 (run_bench `--out`→tempdir so default run never clobbers committed baseline; floor_check dual-path git-show-floor vs tempdir-capture, G5 preserved; run_final multiturn scorer + loud missing-snapshot)
- `88f0e578` tests — TEST-5 (20+ skip-sites→hard asserts + 3 loud `fixture_actually_fires` companions for genuinely build-dependent skips) + TEST-6 (real `missing = expected − CCR-recovered` check; killed the dead `if lossy in strategy` vacuous guard). 74 passed, 0 skips.

**Phase 0 — COMPLETE + pushed (origin/main `d773285b`):**
- `566f3449` TEST-27 lint — pin ruff 0.15.19 + mypy 1.14.1 (ci.yml+pre-commit+pyproject), format sweep (79 files), fix 4 mypy errs (mypy 1.14.1 flags 4 not the finding's 5), wire ci-precheck-python, exclude untracked .claude tooling.
- `590391a4` 0.4 re-baseline — search 40%→92% / logs 85%→93% etc. are LOSSY-drop via the near-dup dedup feature (357dbad8), 100% CCR-recoverable + 100% needle recall; benchmark input unchanged (pure engine behavior). Honest 6-dataset floor.
- `d773285b` honest README Proof table (user chose full-6-dataset framing; regime column: lossless vs lossy-CCR).
Substrate honest, gates trustworthy — "everything depends on it" foundation laid.

**EXECUTION MODEL (user 2026-07-02, refined): fable = SOLE worker for COR + PERF + ARCH clusters** — reused across all (compounds context; combine connecting files in ONE session). PM verifies → gates → commits-in-groups → pushes. **SECURITY EXCLUSION (user, non-negotiable): fable's safeguards flag security/vulnerability/telemetry work → PRE-FILTER every fable batch so he never even READS a security finding.** Route to opus/sonnet instead: entire SEC cluster (§3.2), COR-7 (panic/FFI DoS-adjacent), PERF-6 (MD5 weak-hash wording), all telemetry/TOIN/collector/beacon/tool-injection/redaction/jail/secrets (Phase-2 + Phase-3 SIMP-1/3/4). Owner decisions (COR-10/14, §5) → user.

**🔓 SECURITY-EXCLUSION LIFTED (user 2026-07-02) — supersedes the exclusion above.** After **fableSec survived MCP/store COR-32/51/54/56** (mcp_server + compression_store — the exact security-adjacent files) where sonnet×2 + opus stream-idle-DIED, user confirmed: **fable CAN handle the "tough" security-adjacent parts. GOING FORWARD fable = DEFAULT for ALL tough jobs** — full SEC cluster §3.2, security-adjacent COR (21/36/etc.), the Great Excision itself, PERF/ARCH. **opus = FALLBACK ONLY if fable safeguard-FLAGS/refuses a task** (not for timeouts — those get re-dispatched/foregrounded). No more pre-filtering security findings away from fable. **Reuse the running fable via SendMessage** for follow-up edits (compounds context) before respawning.

**Phase 1 — invariant gaps. Rust items share maturin `.so` → serialize Rust; `maturin develop` before pytest.**
- **1.1 DONE `a6344a2f` [fable]** — COR-4 CCR chunk-flood (persist only dropped rows + capacity/4 granular gate + `CcrStore::capacity()`; whole-blob backstop unconditional/last) + COR-20 honest marker count. Gate G1-G5 green, 2 breach-repro tests (two-large-arrays, single-oversized), design verified (whole-blob=recovery backstop, chunks=proportional-retrieval optimization; multiset-diff over-approximates, never misses).
- **COR BATCH 1 (Phase-1 correctness) — split into connected sub-groups, FRESH fable per group** (the reused session bloated to ~1M tok → escalating stream-idle-timeouts; fresh context runs clean). Each gate G1-G5 green, pushed:
  - ✓ COR-8/9 `3457b905` (tag_protector balanced nested close / unquoted-`/` lookahead)
  - ✓ COR-19 `7e22da05` (single ccr_store field — walk_array opaque cells persist)
  - ✓ COR-6/11/12 `65a9aeeb` (+dedup `42e3476c`) (kompress store-fail veto / onnx_coreml / mid-batch KeyError)
  - ✓ COR-15 `ba3e954b` (+fmt `a11167de`) (decline compaction on grammar-breaking column keys, fail-closed)
  - ✓ COR-13 `4f622cd6` (gate lossless-accept to `is_decoder_verifiable` — Table-no-Nested; json-cell decode; bench **+0.0000** all 6 datasets). **FOLLOW-UPS (noted, non-blocking):** full Buckets/Nested wire coverage (needs formatter+decoder lockstep — the "1-day" option); `walker.rs:126` (compact_document_json) 4th accept site still ships Buckets/Nested (not labeled lossless — PM decision whether to adopt the predicate).
  - ✓ COR-5 `625fd108` (typed-hash store-miss → CcrMirrorError fail-open + de-vacuum typed-parity test). COR-4 fixed single>capacity; COR-5 closes the residual aggregate-eviction window.
  - **→ PHASE-1 CLEAR-CORRECTNESS COMPLETE** (COR-4/5/6/8/9/11/12/13/15/19/20). All gate G1-G5 green + pushed.
- **COR-7 → opus ✓ `b4c70d0a`** — FFI panic containment (catch_unwind→PyRuntimeError on 7 hot bridge methods inside allow_threads + `except (KeyboardInterrupt,SystemExit): raise; except BaseException` fail-open in compress() + Cargo comment; panic=unwind confirmed). Success paths byte-identical.
- **BATCH-2 PROGRESS (fresh fable per connected group, light specs):** ✓ G1 COR-25/26/27 `93467935` · ✓ G2 COR-23/24 `e14a8d4e` (parity twins retired → Rust+bridge tests; repeated_logs +0.3pp) · ✓ G3 COR-28/33/45 `fc5d03c3` (mixed-array persist-skip+lossless-win, dup_count gating, walker no-op guard) · ✓ G4 COR-22/29 `e3bd0f6b`.
- **⚡ PR #5 MERGED `fb9c27e0`** (user-commissioned parallel fable web session; I verified EMPIRICALLY — local merge + gate on merged tree G1-G5 GREEN, not just trusting the PR). CCR_OFFLOAD fallback (content_router.py + router_policy.py): large uncompressible content (≥4000 chars, ratio ≥0.9) → byte-exact CCR store + preview+marker. Corpus **36%→95%**, code@7 **0%→98.9%**, multiturn **71%→87%**, all 100% retention + needle-100% + recovery-23. Honest lossy-CCR framing (agent file-reads excluded, is_error verbatim, needs ccr_enabled). **New baseline_results.json is the floor now** (code 471 / 0.989 etc.). Also fixes: strict marker pinning (0 vs 21 FP), error-protection JSON exclusion (≈ part of COR-16), CI perms. My COR-1..49 fixes all survived the merge (assertions intact).
- **G5a (COR-16/17/18) was STOPPED** for the merge (overlapped content_router.py). PR #5's error-protection-JSON-exclusion partially addresses COR-16. **RE-ASSESS COR-16/17/18 against the PR-#5-modified `content_router.py`** before redoing (COR-17 word→token + COR-18 cache-key likely still needed; COR-16 may be covered).
  **Remaining fable groups (on merged main):** G5(re-assess) 16/17/18/30/31/39/47/48, ✓ G7 dedup `820fd6de` (COR-42 surrogate-safe hashing + COR-52 protect_recent contract; multiturn re-baselined 86.5%→85.1% contract-correct, README sync flagged Q13). **NEXT:** G6 compress.py 43/46/49/50, G8 cache_aligner 53, G9 tokenizers 40, G10 misc 41, G11 serde 44. **Security-adjacent → opus/sonnet:** COR-21/32/36/37/38/51/54/56.

### POST-COMPACT WAVE-1 (2026-07-02, main `65f866fd`)
- ✓ **G8/COR-53** committed `65f866fd` (CacheAligner block-format text-part awareness; bench 0-delta).
- **WAVE 1 dispatched** (fresh agents, disjoint files, parallel):
  - ✓ **fableTok COR-40** DONE+reviewed (uncommitted): (a) 60× base64 explosion killed (1607 vs 110030), (b) GPT-4o→o200k `.lower()`, (c) 3 permanent negative-caches removed (HF 300s TTL negative cache, success perma-cached), (d) mistral SKIPPED+flagged (mistral-common not installed — didn't guess). Full suite 842 pass. **Flagged SEC-1**: `trust_remote_code=True` left untouched (→opus/sonnet). Files: tokenizers/{tiktoken_counter,huggingface,registry}.py + tests/test_tokenizers.py.
  - ⏳ **fableA COR-46/49/50** running (compress lifecycle). **COR-43 PULLED → sonnet** (advisor: its fix moves pipeline construction into COR-7's FFI-panic/`BaseException` fail-open region = excluded-from-fable). SendMessage sent to drop it.
  - ⏳ **sonSerde COR-44/45** running (Rust serde fail-closed + walker no-op guard). Re-dispatched as sonnet after **opusSerde stream-idle-timeout** (0 persistent edits).
- **ROUTING CORRECTIONS:** COR-43 → sonnet (COR-7 region). COR-16 → sonnet (keyword set has `"security"`/`"vulnerability"` literals — avoid fable safeguard-flag). COR-41 → opus/sonnet (touches TOIN telemetry). COR-32 → sonnet (mcp_server+compression_store).
- **RE-ASSESSED vs current content_router.py (PR#5+G8):** COR-18 still needed (`content_key=hash(text)` still content-only). COR-47 PARTIALLY done (`_process_content_blocks`/`_compress_content_block` shared two-tier cache exists — fable-B verifies SmartCrusher/dedup mirrors + `nested_blocks` counter). COR-16 NOT fixed but →sonnet.
- ✅ **WAVE 1 COMMITTED + pushed → main `76f643fa`**: serde `18da4e93` (COR-44) · compress `ced9aa6a` (COR-46/49/50) · tokenizers `9b907ba6` (COR-40). Gate G1-G5 GREEN, **0 bench movement** (floor identical), recovery=23, needle-100%. All 3 diffs PM-reviewed PASS. COR-45 was already done (G3 `fc5d03c3`).
- ⏳ **WAVE 2 DISPATCHED** (fresh, parallel, disjoint files):
  - **fableB** — content_router cluster COR-17/18/30/31/39/47/48. Re-verify-first spec (PR#5+G8 may have covered some); COR-18 confirmed-still-needed (hash(content) key), COR-47 partially-done (verify SmartCrusher/dedup mirrors + counter). 17+47 kept as bisectable hunks.
  - **sonSec** (sonnet) — MCP/store COR-32/51/54/56 (mcp_server/compression_store/marker_grammar — disjoint from fableB). COR-54 = query-path precision (json.loads/dumps undoes arbitrary_precision), COR-51 = MCP marker-hash TTL, COR-56 = collision keep-first, COR-32 = hash lowercase.
- **WAVE 3 (after Wave 2, all touch content_router/router_dispatch or TOIN → serialize):** sonnet COR-16 (analysis-intent keyword trim — has "security"/"vulnerability" literals) + COR-43 (pipeline fail-open, COR-7 region) + COR-21 (router_cache DoS/leak) + COR-38 (html_extractor + router_dispatch:198-202). Then COR-41 (TOIN misc + Rust in_memory/log/search) + COR-37 (compression_store eviction, **both TOIN-entangled — may defer to Q1 excision decision**). Then SEC cluster §3.2 + PERF/ARCH/TEST/DOC/API/SIMP.

### 🔥 GREAT EXCISION — GREENLIT (user 2026-07-02: "radera, i want it GONE 😡")
- **Scope = MAXIMAL LEAN** (user choice): DELETE telemetry/TOIN/feedback (~4.2k LOC: `headroom/telemetry/` + `cache/compression_feedback.py`) + Kompress ML (`kompress_compressor.py` ~1.5k + `[ml]` deps onnx/transformers/torch/huggingface-hub/numpy) + HTML/trafilatura (`html_extractor.py` + `[html]`) + dead Rust `content_detector` mirror (~700 LOC, SIMP-6) + ast-grep code-path (dead, 0% — EFF-2/API-6, drop `ast-grep-cli` dep) + tokenizers→tiktoken-only (drop `huggingface.py`/`mistral.py` backends). ≈10k+ LOC + 4 heavy deps.
- **Timing = NEXT** (after Wave-2 commits — excision touches content_router/mcp_server/router_dispatch TOIN+kompress call-sites → serialize after fableB/opusSec).
- **INVARIANT (proven safe):** ML+HTML already UNINSTALLED in venv yet gate GREEN → 0 bench contribution. 6-dataset floor + needle-100% + recovery-23 + MCP hook MUST stay green; gate after each removal chunk.
- **RESOLVES owner-decisions:** Q1 (telemetry DELETE) + Q3 (ast-grep delete/accept-0%-code) + Q4 (Kompress delete) + Q6 (Rust mirror delete). Kills SEC-2/3 + PERF-9/10 for free.
- **MOOT COR (do NOT fix — being deleted):** COR-38 (html_extractor) fully; COR-37/41/36 TOIN-parts (KEEP their non-TOIN bits: COR-41 Rust in_memory/log/search stat fixes, COR-36 mcp stats-honesty core). Re-scope 36/37/41 post-excision.
- **STILL-NEEDED COR (survive excision):** COR-16 (content_router), 21 (router_cache), 43 (compress fail-open) + sonSec's 32/51/54/56 (MCP/store core).
- **Execution (post-Wave-2, delegated agents):** trace every importer of each deleted subsystem (Python+Rust), remove + clean call-sites (TOIN/kompress/html injection → gone, router made total without them), update pyproject (drop `[ml]`/`[html]` extras + `ast-grep-cli`), honest README (drop ML/HTML/multi-tokenizer claims), gate per chunk. Motive (user): storage + compute + token-consumption when working those parts.
- **⬆ SCOPE ESCALATED → SEMI-NUCLEAR** (user 2026-07-02: "vill inte ha onödiga delar vars use-case aldrig kommer användas"). Beyond maximal-lean, ALSO sweep every module/subsystem whose use-case never runs on the Claude-Code-hook path: `signals`/`relevance` subsystems, all non-tiktoken tokenizer backends, dead flags/config/modules.
  - **OPERATIVE RULE (gate = arbiter, zero guessing):** delete anything whose removal leaves `{6-dataset floor + needle-100% + recovery-23 + MCP-hook import+compress smoke}` GREEN. Whatever's needed to keep them green STAYS (used ⇒ shows in bench/hook). Ambiguous → bench-gate + revert if anything moves. Genuinely uncertain → KEEP + flag (the "semi", not reckless).
- **⏸ MCP/store COR-32/51/54/56 DEFERRED:** 4 background-agent stream-idle-timeouts (sonSec×2, opusSec, sonSec orig) — big-file reads (compression_store.py ~2400 LOC) stall bg agents regardless of model. Not moot (mcp_server/compression_store survive excision). Run AFTER excision, FOREGROUND or split-per-finding (tiny scope). Sites known: COR-32 `_handle_retrieve` hash.lower(); COR-51 store singleton `default_ttl=MCP_SESSION_TTL`; COR-54 query-path `parse_float=Decimal`+`allow_nan=False`; COR-56 collision keep-first.
  - **NEW follow-up defects found mid-run (log, non-blocking):** (i) mixed-array `_dup_count` index-matching drops stamped rows from the mixed output (`dict:25→1`, recoverable via outer sentinel — not data-loss; COR-33 reduces, not eliminates); (ii) COR-13 full Buckets/Nested wire coverage; (iii) walker.rs:126 4th lossless-accept site (COR-13). **Infra:** `scripts/build_rust_extension.sh` broken in uv venv (no pip) → agents use `maturin develop --uv`; ci-precheck-python Rust step needs the same fix.
- **COR BATCH 2 — VETTED (fresh fable per connected-file group; bench-gate output-changers):**
  - **SAFE → fable** (~28 pure-correctness): COR-16/17/18 (router efficacy, bench-gated), 22/29 (kompress), 23/24 (Rust parity anchors/field_detect), 25/26/27 (log/search/diff compressors), 28/33/45 (crusher/walker), 30/31/39/47/48 (content_router routing), 34 (analyzer), 35 (planning), 40 (tokenizers), 41 (misc nits), 42/52 (cross_message_dedup), 43/46/49/50 (compress.py — 46/49 are highs), 44 (serde magic-token), 53 (cache_aligner). Fable instructed to STOP+flag any that turns security-adjacent (backstop).
  - **SECURITY-ADJACENT → opus/sonnet** (excluded from fable per user caveat): COR-21 (router_cache thread-safety/leak = DoS), 32/36/51 (mcp_server), 37/54/56 (compression_store), 38 (html_extractor).
  - **OWNER-DECISIONS → user** (QUESTIONS-FOR-USER.md): COR-10 (Bash), COR-14 (dotted-flatten).

**AFTER COR:** PERF cluster (§3.5 → fable, connecting-file batches, PERF-6→opus) · ARCH cluster (§3.3 incl. §4.1 ContentRouter decomposition + §4.2 CCR typed-FFI-refs → fable) · SEC/TEST/DOC/API/SIMP (→ opus/sonnet) · §5 owner decisions → user before Phase 3 Great Excision.

**Still-open:** §5's 15 owner decisions — surface as a batch before Phase 3 Excision. User chose "full zero-material" (DO-everything); HOW is open (delete-vs-shrink TOIN, restore-AST-vs-accept-0%-code, etc.).

Round-4 deferred cosmetics (logged, NOT blocking — re-recon arbitrates): C4 lazy-init compressor-singleton race (benign under GIL; threading-change risk); C8 CODEBASE-MAP ~15 anchors ~1-3 lines early (re-drifts next edit); C6 `compression_ratio` cross-type name-collision. `.gitignore` has OTHER pre-existing dead allowlist negations (`!scripts/install-git-hooks.sh`, `install.sh`, `install.ps1`, `version-sync.py`) — harmless no-ops, left per surgical-scope, flagged for optional future cleanup.

## Round-3 re-recon — findings (3 opus agents, diff-weighted a341bf4f..HEAD)
CODE verdict: **zero material** — extraction behavior-equivalent (all 5 outcomes' counters preserved, CCR guard intact, match totality holds), rename zero code stragglers, Rust 0 warnings, public API honest (39 exports resolve), CacheDisposition ADT exemplary+correctly-private. The material is ALL in the doc/packaging surface:

| id | sev | class | file | fix |
|---|---|---|---|---|
| M1 | P1 | MATERIAL | `.pre-commit-config.yaml` + RUST_DEV.md:148 | live hook runs ABSENT `scripts/sync-plugin-versions.py` → fails every commit; doc claims it works. DELETE dead hook + doc bullet. |
| M2 | P1 | MATERIAL (my batch) | CODEBASE-MAP.md | content_router.py line refs stale ~+36 (extraction `92088258` shifted lines, map never re-anchored) + compress.py:191→197. RE-ANCHOR all. |
| M3 | P1 | MATERIAL | README.md:109-117 | accuracy table (GSM8K/TruthfulQA/SQuAD/BFCL) UNBACKED — verified: numbers exist only in README/llms/archive, BENCHMARKS.md has zero accuracy content. |
| M4 | P1 | MATERIAL | README.md:219 | RTK "ships with binary / first-class part of our stack" FALSE — no rtk anywhere; was excised. Rewrite → comparison peer. |
| M5 | P1 | MATERIAL | README.md:100-107 | headline "Proof" savings table UNBACKED — 17,765→1,408 etc. match nothing. Real backed data = BASELINE.md (code@7 0%, logs@90 84.5% deletion-inflated/CCR-recoverable, search@90 40%). |
| M6 | P2 | MATERIAL | llms.txt:41 | telemetry "enabled by default" over-claims (beacon removed, local-only on-disk now) + phantom `--no-telemetry` flag (only `HEADROOM_TELEMETRY=off` real). |
| M7 | P2 | MATERIAL | pyproject.toml:74-77 (+README:96,172) | `[progress]` extra inert — pulls `rich` for `headroom.binaries` which DOESN'T EXIST. Remove extra + mentions. |
| M8 | P2 | MATERIAL | README.md:24 | `ENTERPRISE.md` nav = dead 404 (only archive/ has it). Remove nav entry. |
| N1 | P3 | NIT (my batch) | content_router.py:~1277 | `_ensure_ccr_backed` docstring "three result-cache HIT sites" → now ONE (extraction collapsed). |
| N2 | P3 | NIT | content_router.py:10-11 | module docstring lists KompressCompressor twice. Collapse. |
| N3 | P3 | NIT | compress.py:202 | `hooks: Any=None` → `CompressionHooks\|None=None` (public type, per RULES no-lie-signatures). |

BY-DESIGN (no fix): bump() widening (no-op); StorageError+5 exceptions exported-never-raised (public API design call); pyproject version 0.25 vs runtime 0.26 (git-computed, correct).

Batch-fix plan: mechanical/clear-cut (M1,M2,M6,M7,M8,N1,N2,N3) fix autonomously. README Proof/accuracy tables (M3,M4,M5) = outward-facing headline → surfaced to user for shape (delete vs replace-with-honest-BASELINE). Then re-recon round-4 (confirm zero material) → confirmation workflow.

## Remaining
- [x] Batch-fix round-3 material — ALL 11 (M1-M8, N1-N3) landed — `e64602e2`. README Proof-table → honest BASELINE (user chose Option A), accuracy-table deleted, RTK→peer, phantom [progress]/ENTERPRISE/hook removed, telemetry reworded, CODEBASE-MAP re-anchored, docstrings/signature fixed. Verified: 0 fabrications remain, pyproject/pre-commit/imports valid, gate G1-G4 green 750 passed.
- [x] Round-3 reconcile (advisor-caught) — `7fc45aaa`: my honest BASELINE table contradicted the "60–95%" headline (which IS backed by BENCHMARKS.md's 6-seed sweep, not fabricated) → added a bridge line scoping table-vs-sweep; verified llms.txt clean (0 fabrications); finished compress.py map anchors to exact.
- [x] **Round-4 re-recon** (3 fresh opus agents: doc-integrity / code-correctness / config-surface, holistic, baseline `a341bf4f..HEAD`) — **CODE zero-material** (all 5 CacheDisposition outcomes correct, no route_counts double-count, CCR guard single-seam, ML→Kompress rename complete, hooks typing sound). Found **3 MATERIAL all doc/config** → batch-fixed `c7693fa7`, gate G1-G5 GREEN (recovery 23, needle 100%, 0 bench regression): **M1** BENCHMARKS.md 6 broken repro citations → real `verify/measure.py` + re-runnable harnesses (430KB `raw_results.json` stays regenerable, NOT committed); **M2** README:11 headline "60–95%" scoped to "redundant workloads" (user chose (b) — 60-floor mapped to no measured tier) + retired "6 algorithms" (C1) + unified model→`Kompress-v2-base`; **M3** Makefile/CONTRIBUTING `make install-git-hooks` (exit 127, absent script + phantom pre-push) → `pre-commit install`, commit-stage hooks only. Plus **C5** content_router.py exhaustive `case other: raise` on BOTH CacheDisposition matches; **C10** .gitignore dead negation removed.
- [x] **200-agent confirmation workflow — RAN (round-5, `adversarial-critique.js`, 204 agents, 13.5M tok).** Contrary to "confirmation not discovery" it FOUND 5 NOVEL highs round-4 recon missed → mandate "workflow finds it = PM miss" fired. Census (triaged from RAW, not the 16KB synth slice — advisor caught the confirmation-bias): 0 crit · 5 high · 47 med · 69 low · 4 nit. **All 5 highs FIXED + gate-green** (`aa89cf6d` + `3403a4a8`):
    - [x] high #3/#4 DOCS `aa89cf6d` — README/llms.txt marketed CodeCompressor [retired] + IntelligentContext [never existed] → aligned to real set (SmartCrusher/Kompress-v2-base/Search/Log/Diff/HTMLExtractor/CacheAligner/CrossMessageDeduper/CCR); comment-rot fixed.
    - [x] high #2/#5 SECURITY `aa89cf6d` — compression_store.py retrieval-log redaction missed JSON quoted-key + bare AWS/GitHub secrets → regex fix + provider-token rule + query redaction + honest SECURITY.md; +7 tests. Gate G1-G4 green.
    - [x] high #1 CORRECTNESS `3403a4a8` — CSV "lossless" path corrupted JSON null + missing-key (both→"", unrecoverable, reachable; fuzz was green-by-avoiding-null). User chose true-lossless (a): two exact-match reserved sentinels `__null__`/`__missing__`, Rust encoder + Python decoder in byte-lockstep, escaped like ditto `=`. Gate G1-G5 GREEN incl bench floor-check (no compression regression).
  - [x] **PRE-STAMP GATE — DONE:** patched `adversarial-critique.js` ORIENT exclude (line 37) to name `.claude/`, `docs/audits/`, PLAN/handoff/codebase-CRITIQUE/recon-findings + any `*CRITIQUE*`/`*AUDIT*` md as session-scaffolding-not-under-review; real shipped docs (README/BENCHMARKS/CONTRIBUTING/RUST_DEV/llms.txt/CODEBASE-MAP) stay in scope. Durable for all future runs.
  - [ ] **NEXT — triage 47 MEDIUMS:** usage-themes (docs 11 / security 3 / correctness 3 / api 4 ≈ 21) may hide usage-affecting defects → 2nd fix pass; architecture ~14 = known-deferred god-object/CCR facets; types/perf/tests/simplicity ≈ 12 = mostly improvements. Do NOT yet declare "usage beyond-perfect" — security/correctness mediums not triaged. Then round-6 re-recon → confirm cleared. NORTH-STAR user call: how deep into the 120-item med/low backlog before MCP tool creation (usage-defects-only vs full god-object refactor). Separate open: BASELINE.md stale; run_bench dirties tree as a pytest side-effect.

## Cycle-6 commit ledger
`92088258` site-3 extraction · `0844692f` format guards · `1d8a69e9` ML→Kompress rename · `e64602e2` round-3 doc-integrity batch · `7fc45aaa` round-3 reconcile · `c7693fa7` round-4 doc-integrity + config batch · `aa89cf6d` round-5 stamp-highs 4/5 (docs #3/#4 + security #2/#5) · `3403a4a8` round-5 high #1 CSV true-lossless sentinels · (+ PLAN.md checkpoints cbda82af/5477ebfc/bf9234d0/e021bb3c)

## Fable-recon campaign commit ledger (Phase 0)
`c5b58ca8` gate honesty + FABLE-RECON-PLAN.md · `f1c44778` COR-1/COR-2 lossless-decoder data-loss fix · `7988bb5c` COR-3+TEST-26 verify substrate · `4ffd2541` TEST-4 bench-out isolation · `88f0e578` TEST-5/6 test anti-vacuity · `566f3449` TEST-27 lint pin+sweep · `590391a4` 0.4 re-baseline (6-dataset honest floor) · `d773285b` honest README Proof table — **PHASE 0 COMPLETE**
