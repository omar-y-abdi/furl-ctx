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
- [ ] S7 — rename `_try_ml_compressor`→`_try_kompress` — **DEFERRED (not half-done).** Recon rider scoped "same file / 5 min" but is actually a ~20-site cross-cutting ML→Kompress vocab migration: method `_try_ml_compressor` (content_router.py def+call, 4 test files) + LIVE dispatcher param `try_ml_compressor` (router_dispatch.py 80/127/205/212/249) + type alias `_TryMlCompressor`. Method-only rename would INTRODUCE inconsistency (method `_try_kompress` passed as param `try_ml_compressor`). It's the last old-vocab holdout (round-1 migrated the rest). Surgical-changes rule (trace-to-request) + it's my rider not user's ask → capture for a deliberate COMPLETE pass, don't rush into the extraction diff. Codebase currently CONSISTENT (untouched). Needs user greenlight.
- [ ] **Round-3 re-recon** — diff-weighted on the round-2 batch → confirm zero MATERIAL (by-design/nitpick don't count). Loop if material.
- [ ] **200-agent confirmation workflow** — ONLY when re-recon confirms beyond-perfect. `adversarial-critique.js`, `args.map=CODEBASE-MAP.md`. Confirmation, never discovery.

## Notes / residual (re-recon arbitrates — not reactive-fix)
- `exceptions.py` StorageError exported-but-never-raised.
- README accuracy-table (GSM8K/TruthfulQA/SQuAD/BFCL) numbers not backed by any in-repo file.
- RTK-binary-shipping claim (README attribution) unconfirmed in the live package.
- RUST_DEV.md pre-commit bullet references absent `scripts/sync-plugin-versions.py`.

## Current position
HEAD = `92088258` (site-3 extraction), gated-GREEN (G1-G5 incl. bench, 750 passed +12, recovery 23, floor needle 100%), zero uncommitted code.
Cycle-6 code work COMPLETE. Remaining = VERIFY-phase re-recon + confirmation:
- [ ] **Round-3 re-recon** — diff-weighted on the round-2+site-3 batch (`a341bf4f..HEAD`) → confirm zero MATERIAL (by-design/nitpick don't count). Loop if material.
- [ ] **200-agent confirmation workflow** — ONLY after re-recon confirms beyond-perfect. Confirmation, never discovery.
- [ ] S7 rename (deferred, above) — user greenlight.
