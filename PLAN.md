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

## Current position
HEAD = `1d8a69e9` (ML→Kompress rename); site-3 extraction `92088258` + format guards `0844692f`.
Gated-GREEN (G1-G5 incl. bench, 750 passed, recovery 23, floor needle 100%), zero uncommitted code.
Cycle-6 CODE work COMPLETE (extraction + rename). Round-3 re-recon DONE → found MATERIAL (doc-integrity, not code).

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
- [ ] **Round-4 re-recon** — confirm zero NEW/residual material. MUST be HOLISTIC not diff-only (advisor): the round-3 diff changed the table but a diff-weighted pass would miss the untouched headline/tagline + `.txt` surface. Sweep the FULL doc set for savings-claim consistency (README tagline/GIF/table/BENCHMARKS/llms.txt all agree), incl. `.txt`. Fresh agents. (Very long session — /compact STRONGLY recommended first for recon quality — deep recon is the whole point; a context-starved lead defeats it.)
- [ ] **200-agent confirmation workflow** — ONLY after re-recon confirms beyond-perfect. Confirmation, never discovery.

## Cycle-6 commit ledger
`92088258` site-3 extraction · `0844692f` format guards · `1d8a69e9` ML→Kompress rename · `e64602e2` round-3 doc-integrity batch · `7fc45aaa` round-3 reconcile · (+ PLAN.md checkpoints cbda82af/5477ebfc/bf9234d0/e021bb3c)
