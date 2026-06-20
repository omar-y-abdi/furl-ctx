# EVAL LAUNCH SPEC — execute on user "go"

Workflow: `headroom-parallel-eval` (saved: `.claude/workflows/eval/headroom-parallel-eval.js`). Validated, 0 warnings.
Baseline pinned: `608fc7ac` on `verify/phase2-audit-report` (after wyg1sl7ew fully landed). RE-READ HEAD at
go-time to confirm unchanged (worktrees branch off live HEAD; baselineRef is informational).

## PHASE 0 (when user says go — repo now free)
1. Confirm idle: `git rev-parse HEAD` (record), `git status --short` (only untracked .claude/+PLAN.md+EVAL*.md),
   `ps aux | grep -iE 'maturin|cargo|rustc' | grep -v grep` (empty).
2. Rebuild shared engine at HEAD (sanity + REQUIRED for break mode's read-only shared engine):
   `.venv/bin/maturin develop` → must be GREEN. `cargo test -p headroom-core 2>&1 | grep 'test result:'` → 0 failed.
3. Fresh baseline numbers: `.venv/bin/python -m benchmarks.run_bench` → record the 3 dataset rows + needle-recall.
   THEN restore: `git checkout HEAD -- benchmarks/baseline_results.json benchmarks/BASELINE.md`.
4. If fresh numbers differ from the script default, pass them via `args.baselineNumbers`.

## PHASE 0.5 — RECON (run ONCE; map fed into all 3 modes)
Workflow({ name: 'headroom-codebase-map', args: { repo: '/Users/k/dev/headroom' } })  // ~12 read-only Explore agents
→ capture `MAP = result.map.map_markdown`. Also write it to `.claude/runtime/CODEBASE-MAP.md` for review.
Read-only (no worktree/build) → safe to run even before full go if desired.

## LAUNCH (sequential — one at a time; 3 parallel would thrash 12 cores / cap 10)
HEAD = <re-read at go-time> ; use it as baselineRef. MAP = recon output (pass as codebaseMap in EVERY run).
Stop logic: escalateAt:2 → after 2 dry rounds, ONE widen-scope "last chance" round; stop only if that's also dry.

### 1) optimize  (run first — biggest value)
Workflow({ name: 'headroom-parallel-eval', args: {
  mode: 'optimize', baselineRef: '<HEAD>', codebaseMap: '<MAP>', roundSize: 6, maxAgents: 48, escalateAt: 2 } })
→ on return: write `EVAL-optimize.md` ← synthesis.doc_markdown. Verify tree clean (worktrees auto-discarded).

### 2) break  (needs the shared .venv built at HEAD from Phase 0 step 2)
Workflow({ name: 'headroom-parallel-eval', args: {
  mode: 'break', baselineRef: '<HEAD>', codebaseMap: '<MAP>', roundSize: 10, maxAgents: 50, escalateAt: 2 } })
→ write `EVAL-break.md` ← synthesis.doc_markdown.

### 3) quality
Workflow({ name: 'headroom-parallel-eval', args: {
  mode: 'quality', baselineRef: '<HEAD>', codebaseMap: '<MAP>', roundSize: 6, maxAgents: 48, escalateAt: 2 } })
→ write `EVAL-quality.md` ← synthesis.doc_markdown.

## AFTER ALL 3
Present the 3 action-docs (ranked TODO / confirmed defects / quality backlog) to the user. User picks what I build.
Each run returns { mode, rounds, attempts, wins, ledger, synthesis } — also surface wins/attempts counts honestly.

## NOTES
- Each worktree agent builds in its OWN .venv-eval (enforced in prompt) — never the shared .venv.
- run_bench overwrites baseline files → restore after any real run (also inside worktrees, but those are discarded).
- Branch reconcile (ff main ← verify/phase2-audit-report) is a SEPARATE post-eval step — flag, don't do unasked.
