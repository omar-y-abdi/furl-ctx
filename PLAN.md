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

## REMAINING (in progress — next actionable)
- [ ] **site-3 lookup-half extraction** — user chose EXTRACT. P0 recovery hot-path; CHECKPOINTED for fresh context (advisor budget-fork: don't half-do it low on context). Full exec spec in `recon-findings.md` SITE-3 block:
      1. PIN `route_counts` FIRST via stub observer (all 5 outcomes × string+block) — the advisor's blind-spot: current pins assert `_cache.stats`, NOT the `route_counts` dict where the two sites differ + which ships to observer/`/stats`.
      2. Extract `_lookup_cached_disposition(...) -> CacheDisposition` (3 actions: `ServeOriginal | ServeCached | Recompute`); ALL effects inside (counter bumps, move_to_skip, invalidate, `_ensure_ccr_backed`, stale-vs-miss split); formatting stays in callers.
      3. Rewire block site → gate → string site → gate.
      Fold the `_try_ml_compressor`→`_try_kompress` rename (same file).
- [ ] **Round-3 re-recon** — diff-weighted on the round-2 batch → confirm zero MATERIAL (by-design/nitpick don't count). Loop if material.
- [ ] **200-agent confirmation workflow** — ONLY when re-recon confirms beyond-perfect. `adversarial-critique.js`, `args.map=CODEBASE-MAP.md`. Confirmation, never discovery.

## Notes / residual (re-recon arbitrates — not reactive-fix)
- `exceptions.py` StorageError exported-but-never-raised.
- README accuracy-table (GSM8K/TruthfulQA/SQuAD/BFCL) numbers not backed by any in-repo file.
- RTK-binary-shipping claim (README attribution) unconfirmed in the live package.
- RUST_DEV.md pre-commit bullet references absent `scripts/sync-plugin-versions.py`.

## Current position
HEAD = `f1119fce`, gated-GREEN (G1-G4, 738 passed, recovery 23), zero uncommitted code.
Next actionable = **site-3** (fresh context recommended — long session).
