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

**Phase 0 — wave 2b RUNNING [sonnet]:** TEST-27 lint gate — pin ruff/mypy across ci.yml+pre-commit+pyproject, `ruff --fix`/`format` sweep, fix 5 mypy errors (csv_schema_decoder:566 / router_policy:68,82,115 / pipeline:136), wire ci-precheck-python.
**Phase 0 — wave 2c NEXT:** 0.4 re-baseline (regen BASELINE.md+baseline_results.json via `run_bench --out benchmarks`; README Proof-table update **SURFACED TO USER** per advisor — outward-facing, arbitrated ≥3×; investigate if numbers move wildly = lossy-search trap).
**THEN Phase 1** (invariant gaps COR-4/5/6/7/8 + COR-20, Rust+Python, `maturin develop` before pytest), then §5 owner decisions batched to user at Phase-1→2 boundary, then the 2 refactors → fable.

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
`c5b58ca8` gate honesty + FABLE-RECON-PLAN.md · `f1c44778` COR-1/COR-2 lossless-decoder data-loss fix · `7988bb5c` COR-3+TEST-26 verify substrate · `4ffd2541` TEST-4 bench-out isolation · `88f0e578` TEST-5/6 test anti-vacuity
