// headroom-implement — opus PLAN -> parallel sonnet BUILD (TDD, isolated worktrees) ->
// sequential INTEGRATE (cherry-pick onto main, safe-git only) -> opus VERIFY. Phased.
//
// The opus planner partitions the target fixes into ordered, file-DISJOINT, dependency-aware
// BATCHES so parallel sonnet builders never touch the same file (no collision). Each builder
// works fully isolated (own worktree + own venv + shared sccache), TDD, and COMMITS in its
// worktree; the integrator cherry-picks those commits onto the real branch one at a time,
// rebuilding + running the full guardrail suite after each, reverting any that regress. Opus
// then verifies every acceptance criterion on the integrated tree.
//
// Reusable: run once with scope='P0+P1', verify, then again with scope='P2'.
// RUN: Workflow({ scriptPath, args: { scope, docs, baselineRef, codebaseMapPath } })

export const meta = {
  name: 'headroom-implement',
  description: 'opus plans + partitions into non-colliding batches -> parallel sonnet TDD builders (isolated worktrees, commit) -> sequential cherry-pick integrate onto main -> opus verify.',
  phases: [
    { title: 'Plan', detail: 'opus: ordered, file-disjoint, dependency-aware work-units + batches' },
    { title: 'Build', detail: 'sonnet: one TDD builder per unit, isolated worktree, commit + report sha' },
    { title: 'Integrate', detail: 'cherry-pick each unit onto main, rebuild + full suite, revert on regress (safe git)' },
    { title: 'Verify', detail: 'opus: all acceptance criteria + 0 regression + out-of-sample on integrated tree' },
  ],
}

const A = (typeof args === 'string') ? JSON.parse(args) : (args || {})
const REPO = A.repo || '/Users/k/dev/headroom'
const SCOPE = A.scope || 'P0+P1'
const DOCS = A.docs || ['EVAL-SUMMARY.md', 'EVAL-break.md', 'EVAL-quality.md']
const MAP_PATH = A.codebaseMapPath || '/tmp/headroom-CODEBASE-MAP.md'
const MAX_BATCHES = A.maxBatches || 10

const SETUP = `Isolated build (own venv + SHARED sccache; deps from pip cache; never touch the shared .venv): \`python3 -m venv .venv-eval && .venv-eval/bin/pip install -q maturin\` then \`export RUSTC_WRAPPER=sccache SCCACHE_DIR=/Users/k/dev/headroom/.sccache CARGO_INCREMENTAL=0 && .venv-eval/bin/maturin develop\`. FAIL-LOUD precedence: \`.venv-eval/bin/python -c "import headroom,inspect,os;print(os.path.realpath(inspect.getfile(headroom)))"\` MUST be under YOUR worktree — if not, stop and report self_verified=false.`

const CONTRACTS = `HARD CONTRACTS (a fix that breaks one is NOT done): (1) CCR recovery invariant — every dropped/substituted distinct item recoverable, 0 silent loss; (2) prompt-cache ordering — never drop msg index 0 / reorder cached prefix / rewrite cache_control; (3) Python<->Rust canonical-hash parity; (4) no overfitting to fixtures — fixes must hold on fresh out-of-sample data. Guardrails stay green: cargo test -p headroom-core (0 failed), recovery invariant 21/21, full pytest suite 0 failed, needle-recall 100%.`

const TWO_COPIES = `TWO COPIES EXIST: /Users/k/dev/headroom is the shared baseline; your worktree (cwd, confirm with \`pwd\`) is the ONLY place you edit. The codebase map (read it first: ${MAP_PATH}) uses repo-RELATIVE paths — resolve them in YOUR worktree, never against /Users/k/dev/headroom.`

// ---------------- schemas ----------------
const PLAN = {
  type: 'object',
  properties: {
    units: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' }, tier: { type: 'string' }, title: { type: 'string' },
          problem: { type: 'string' }, files: { type: 'array', items: { type: 'string' } },
          approach: { type: 'string' }, test_plan: { type: 'string' },
          acceptance: { type: 'string' }, depends_on: { type: 'array', items: { type: 'string' } },
        },
        required: ['id', 'tier', 'title', 'files', 'approach', 'test_plan', 'acceptance'],
      },
    },
    batches: { type: 'array', items: { type: 'array', items: { type: 'string' } } }, // ordered; each = file-disjoint, dep-free unit ids
    rationale: { type: 'string' },
  },
  required: ['units', 'batches'],
}

const BUILD = {
  type: 'object',
  properties: {
    unit_id: { type: 'string' },
    commit_sha: { type: 'string' },            // sha of the commit made IN the worktree (empty if build failed)
    files_changed: { type: 'array', items: { type: 'string' } },
    tests_added: { type: 'array', items: { type: 'string' } },
    suite_result: { type: 'string' },          // verbatim cargo/pytest/recovery results
    contracts_ok: { type: 'boolean' },
    self_verified: { type: 'boolean' },         // built green + acceptance test passes + precedence ok
    notes: { type: 'string' },
  },
  required: ['unit_id', 'commit_sha', 'contracts_ok', 'self_verified'],
}

const INTEGRATE = {
  type: 'object',
  properties: {
    applied: { type: 'array', items: { type: 'object', properties: { unit_id: { type: 'string' }, main_sha: { type: 'string' } }, required: ['unit_id'] } },
    failed: { type: 'array', items: { type: 'object', properties: { unit_id: { type: 'string' }, reason: { type: 'string' } }, required: ['unit_id', 'reason'] } },
    final_suite: { type: 'string' },           // suite result on main after this batch
    main_clean: { type: 'boolean' },
  },
  required: ['applied', 'failed', 'main_clean'],
}

const VERIFY = {
  type: 'object',
  properties: {
    all_acceptance_met: { type: 'boolean' },
    per_unit: { type: 'array', items: { type: 'object', properties: { unit_id: { type: 'string' }, met: { type: 'boolean' }, evidence: { type: 'string' } }, required: ['unit_id', 'met'] } },
    regressions: { type: 'array', items: { type: 'string' } },
    out_of_sample_check: { type: 'string' },    // fresh-data re-verification of the key fixes (multi-line, eviction, etc.)
    guardrails: { type: 'string' },             // verbatim cargo/pytest/recovery/needle
    gaps: { type: 'array', items: { type: 'string' } },
    verdict: { type: 'string', enum: ['done', 'gaps_remain'] },
    report_markdown: { type: 'string' },
  },
  required: ['all_acceptance_met', 'verdict', 'guardrails', 'report_markdown'],
}

// ---------------- Phase 1: PLAN ----------------
phase('Plan')
const plan = await agent(
`You are the implementation PLANNER (opus). Read these eval docs in ${REPO} first: ${DOCS.join(', ')}, plus the codebase map at ${MAP_PATH}. They contain a prioritized, evidence-backed list of data-integrity/contract fixes and robustness work found by a 150-agent eval.

GOAL: produce an executable plan to implement scope "${SCOPE}" (the P0 integrity/contract fixes and the P1 robustness items — NOT the P2 compression gains). Read the actual source for every fix so files[] and approach are real (file:line), not guessed.

${CONTRACTS}

OUTPUT a plan of work-units. For EACH unit: id, tier (P0/P1), title, problem, files (exact repo-relative paths it will touch), approach (concrete, citing file:line), test_plan (the failing test to write first — TDD), acceptance (objective pass condition), depends_on (unit ids that must land first).

UNIT GRANULARITY RULE (critical): if two pieces of work touch the SAME file, OR one depends on another's code change, put them in the SAME unit — one builder does them sequentially in its own worktree. (Concrete: the CCR eviction fix and the retrieve-hash fix BOTH touch headroom/cache/compression_store.py → ONE unit. Any Rust change that needs a matching Python decoder/mirror change → ONE unit, byte-parity included.) This guarantees each builder's commit touches a file-set no other builder touches.

THEN partition units into ordered BATCHES. RULE: within a batch, every unit's file-set is STRICTLY DISJOINT from every other unit's in that batch and has no cross-dependency — so parallel builders never collide AND no builder ever needs to see another builder's uncommitted change. NEVER split dependent or same-file work across batches (bundle into one unit instead). Order batches so foundational fixes land first (P0 integrity before P1 hardening); put the cross-language round-trip fuzz/property test in the LAST batch so it guards the finished decoder. Keep each unit's change reviewable.

Return units, batches (array of arrays of unit ids, in execution order), and rationale.`,
  { label: 'plan', phase: 'Plan', model: 'opus', schema: PLAN }
)
const unitById = {}
;(plan?.units || []).forEach((u) => { unitById[u.id] = u })
const batches = (plan?.batches || []).filter((b) => Array.isArray(b) && b.length).slice(0, MAX_BATCHES)
log(`plan: ${(plan?.units || []).length} units, ${batches.length} batches`)

// ---------------- Phases 2+3: BUILD then INTEGRATE, batch by batch ----------------
const integrated = []
const failed = []
let bi = 0
for (const batch of batches) {
  bi++
  const units = batch.map((id) => unitById[id]).filter(Boolean)
  if (!units.length) continue

  phase('Build')
  const built = (await parallel(units.map((u) => () =>
    agent(
`You are a TDD builder (sonnet). Implement ONE work-unit in your FRESH ISOLATED git worktree (branched off the current main). Commit your change IN the worktree so it can be cherry-picked.

${TWO_COPIES}

UNIT ${u.id} — ${u.title}
Problem: ${u.problem || ''}
Files: ${(u.files || []).join(', ')}
Approach: ${u.approach}
Test first (TDD): ${u.test_plan}
Acceptance: ${u.acceptance}

STEPS: (1) ${SETUP} (2) Write the failing test FIRST, run it, confirm it FAILS for the right reason. (3) Implement the smallest fix that makes it pass; if the fix spans Rust, also update the Python decoder/mirror for byte-parity. (4) Rebuild; run your new test + recovery invariant (\`.venv-eval/bin/python -m pytest tests/test_ccr_recovery_invariant.py -q\`) + the full suite (\`.venv-eval/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --continue-on-collection-errors --timeout=120\`) + \`cargo test -p headroom-core\`. (5) Verify every HARD CONTRACT holds. (6) Review \`git status\`, then \`git add\` ONLY the specific source file(s) you changed AND your new test file(s), BY PATH — NEVER \`git add -A\` (it would sweep stray untracked files, caches, or build artifacts into the commit, which then rides the cherry-pick onto the real branch). Double-check your new test is included. Then \`git commit -m "fix(${u.id}): ${u.title}"\` IN YOUR WORKTREE and report \`git rev-parse HEAD\` as commit_sha.

${CONTRACTS}

If you cannot make it pass without breaking a contract, set self_verified=false, contracts_ok=false, commit_sha="" and explain in notes — do NOT weaken a test or a type. Do NOT touch /Users/k/dev/headroom. Return the structured build result.`,
      { label: `build:${u.id}`, phase: 'Build', model: 'sonnet', isolation: 'worktree', schema: BUILD }
    )
  ))).filter(Boolean)

  const ready = built.filter((b) => b && b.self_verified && b.contracts_ok && b.commit_sha)
  log(`batch ${bi} build: ${built.length} returned, ${ready.length} self-verified`)
  built.filter((b) => !(b.self_verified && b.contracts_ok && b.commit_sha)).forEach((b) => failed.push({ unit_id: b.unit_id, reason: 'build self-verify failed: ' + (b.notes || '') }))

  if (!ready.length) continue

  phase('Integrate')
  const integ = await agent(
`You are the INTEGRATOR (sonnet). Apply this batch's verified commits onto the real branch on the MAIN checkout at ${REPO} (cwd there — NOT a worktree). The builder commits were each made in a separate worktree off the current main HEAD and touch DISJOINT files, so they cherry-pick cleanly.

USE ONLY SAFE GIT — allowed: \`git cherry-pick <sha>\`, \`git cherry-pick --abort\`, \`git revert --no-edit <sha>\`, \`git checkout HEAD -- <path>\`. FORBIDDEN: reset --hard, clean -f, checkout -f, any --force. Never discard unrelated work.

Commits to integrate (in this order):
${ready.map((b) => `  - ${b.unit_id}: ${b.commit_sha} (${(b.files_changed || []).join(', ')})`).join('\n')}

FOR EACH, in order: (1) \`git cherry-pick <sha>\`. If it conflicts, \`git cherry-pick --abort\` and record it in failed[] with the reason — do NOT force it. (2) Rebuild: \`export RUSTC_WRAPPER=sccache SCCACHE_DIR=/Users/k/dev/headroom/.sccache CARGO_INCREMENTAL=0 && .venv/bin/maturin develop\`. (3) Run \`cargo test -p headroom-core\` + \`.venv/bin/python -m pytest tests/test_ccr_recovery_invariant.py -q\` + the full pytest suite + \`.venv/bin/python -m benchmarks.run_bench\` (needle-recall must stay 100%), then \`git checkout HEAD -- benchmarks/baseline_results.json benchmarks/BASELINE.md\`. (4) If all green, keep the commit (record in applied[] with the new main sha from \`git rev-parse HEAD\`). If anything regresses, \`git revert --no-edit HEAD\` and record it in failed[] with the failing output. (5) Confirm the working tree has no leftover modifications before the next cherry-pick.

${CONTRACTS}

Report applied[], failed[], the final suite result on main, and main_clean (true if \`git status --porcelain\` shows only expected untracked files).`,
    { label: `integrate:b${bi}`, phase: 'Integrate', model: 'sonnet', schema: INTEGRATE }
  )
  integrated.push(...((integ && integ.applied) || []))
  failed.push(...((integ && integ.failed) || []))
  log(`batch ${bi} integrate: ${((integ && integ.applied) || []).length} applied, ${((integ && integ.failed) || []).length} failed, main_clean=${integ && integ.main_clean}`)
}

// ---------------- Phase 4: VERIFY ----------------
phase('Verify')
const verify = await agent(
`You are the VERIFIER (opus). The "${SCOPE}" fixes have been integrated onto the real branch at ${REPO}. Verify INDEPENDENTLY that the work is actually done and correct — do not trust the builders' self-reports.

Integrated units: ${integrated.map((x) => x.unit_id).join(', ') || '(none)'}
Failed/skipped units: ${failed.map((x) => x.unit_id).join(', ') || '(none)'}
Plan units + acceptance: ${JSON.stringify((plan?.units || []).map((u) => ({ id: u.id, acceptance: u.acceptance })), null, 1)}

DO: (1) For each integrated unit, check its acceptance criterion is objectively met on the current tree (read the code + run the relevant test). (2) Re-verify the two worst original defects on FRESH out-of-sample data you generate yourself (NOT the committed fixtures): a homogeneous array with embedded-newline string fields must now reconstruct byte-exact via the lossless path; and many sequential compress() calls overflowing the in-memory CCR cap must NOT leave an unbacked <<ccr:HASH>> sentinel (recovery still 100%). Prove with sha256, not asserts. (3) Run the full guardrails: \`cargo test -p headroom-core\`, recovery invariant, full pytest suite, \`.venv/bin/python -m benchmarks.run_bench\` (needle-recall 100%) then restore the baseline files. (4) Look for regressions in compression ratio or behavior. (5) List any gaps (unmet acceptance, failed units that still need doing).

${CONTRACTS}

Return: all_acceptance_met, per_unit verdicts with evidence, regressions, out_of_sample_check (your fresh-data results with hashes), guardrails (verbatim), gaps, verdict ('done' only if every in-scope acceptance is met with 0 regression and the out-of-sample checks pass), and report_markdown (a concise status report).`,
  { label: 'verify', phase: 'Verify', model: 'opus', schema: VERIFY }
)
log(`verify: verdict=${verify?.verdict}, acceptance_met=${verify?.all_acceptance_met}, gaps=${(verify?.gaps || []).length}`)

return { scope: SCOPE, plan, integrated, failed, verify }
