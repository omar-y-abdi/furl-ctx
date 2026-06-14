// headroom-implement — opus PLAN -> parallel sonnet BUILD (TDD, isolated worktrees, commit).
//
// SAFETY (post-incident): NO agent ever runs git on the main checkout. Builders work ONLY in
// their own isolated worktrees and only `git add <paths>` + `git commit` THERE. Integration
// (cherry-pick onto the real branch) and final verification are done by the ORCHESTRATOR
// deterministically, OUTSIDE this workflow — so no agent can switch branches / reset / pollute main.
//
// The opus planner makes every unit INDEPENDENT: each unit owns a file-set DISJOINT from every
// other unit, and any dependent or same-file work is BUNDLED into a single unit (one builder does
// it sequentially). Because units are independent + disjoint, their commits cherry-pick cleanly
// onto the branch in any order — no cross-batch dependency, no reliance on worktree freshness.
//
// RUN: Workflow({ scriptPath, args: { scope, docs, codebaseMapPath } })
// THEN the orchestrator cherry-picks the returned builder shas onto the branch + runs opus verify.

export const meta = {
  name: 'headroom-implement',
  description: 'opus plans INDEPENDENT disjoint-file units (dependents bundled) -> parallel sonnet TDD builders commit in isolated worktrees. Orchestrator integrates + verifies (no agent touches main git).',
  phases: [
    { title: 'Plan', detail: 'opus: independent, file-disjoint work-units (dependent/same-file work bundled into one unit)' },
    { title: 'Build', detail: 'sonnet: one TDD builder per unit, isolated worktree, commit IN worktree, report sha (never touches main)' },
  ],
}

const A = (typeof args === 'string') ? JSON.parse(args) : (args || {})
const REPO = A.repo || '/Users/k/dev/headroom'
const SCOPE = A.scope || 'P0+P1'
const DOCS = A.docs || ['EVAL-SUMMARY.md', 'EVAL-break.md', 'EVAL-quality.md']
const MAP_PATH = A.codebaseMapPath || '/tmp/headroom-CODEBASE-MAP.md'
const MAX_UNITS = A.maxUnits || 16

const SETUP = `Isolated build (own venv + SHARED sccache; deps from pip cache; never touch the shared .venv): \`python3 -m venv .venv-eval && .venv-eval/bin/pip install -q maturin\` then \`export RUSTC_WRAPPER=sccache SCCACHE_DIR=/Users/k/dev/headroom/.sccache CARGO_INCREMENTAL=0 && .venv-eval/bin/maturin develop\`. FAIL-LOUD precedence: \`.venv-eval/bin/python -c "import headroom,inspect,os;print(os.path.realpath(inspect.getfile(headroom)))"\` MUST be under YOUR worktree — if not, stop, self_verified=false.`

const CONTRACTS = `HARD CONTRACTS (a fix that breaks one is NOT done): (1) CCR recovery invariant — every dropped/substituted distinct item recoverable, 0 silent loss; (2) prompt-cache ordering — never drop msg index 0 / reorder cached prefix / rewrite cache_control; (3) Python<->Rust canonical-hash parity; (4) no overfitting to fixtures — fixes must hold on fresh out-of-sample data. Guardrails stay green: cargo test -p headroom-core (0 failed), recovery invariant 21/21, full pytest suite 0 failed, needle-recall 100%.`

const GIT_SAFETY = `GIT — STRICT (you are in an ISOLATED WORKTREE; the shared checkout at /Users/k/dev/headroom must NEVER be touched by you): the ONLY git commands you may run are \`git status\`, \`git add <explicit paths>\`, \`git commit\`, \`git rev-parse HEAD\`, \`git diff\` — all inside your worktree. You are FORBIDDEN from running git checkout / switch / reset / branch / stash / rebase / merge / cherry-pick / clean, or any git command naming a branch. Never \`cd\` out of your worktree. If git is in an unexpected state, STOP and report self_verified=false — do NOT try to fix it.`

const TWO_COPIES = `TWO COPIES EXIST: /Users/k/dev/headroom is the shared baseline; your worktree (cwd, confirm with \`pwd\`) is the ONLY place you edit. Read the codebase map first: ${MAP_PATH} — its paths are repo-RELATIVE; resolve them in YOUR worktree.`

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
          approach: { type: 'string' }, test_plan: { type: 'string' }, acceptance: { type: 'string' },
        },
        required: ['id', 'tier', 'title', 'files', 'approach', 'test_plan', 'acceptance'],
      },
    },
    disjointness_note: { type: 'string' },
    integration_order: { type: 'array', items: { type: 'string' } },
  },
  required: ['units'],
}

const BUILD = {
  type: 'object',
  properties: {
    unit_id: { type: 'string' },
    commit_sha: { type: 'string' },
    worktree_path: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    tests_added: { type: 'array', items: { type: 'string' } },
    suite_result: { type: 'string' },
    contracts_ok: { type: 'boolean' },
    self_verified: { type: 'boolean' },
    notes: { type: 'string' },
  },
  required: ['unit_id', 'commit_sha', 'contracts_ok', 'self_verified'],
}

// ---------------- Phase 1: PLAN ----------------
phase('Plan')
const plan = await agent(
`You are the implementation PLANNER (opus). Read these eval docs in ${REPO}: ${DOCS.join(', ')}, plus the codebase map at ${MAP_PATH}. They hold a prioritized, evidence-backed list of data-integrity / contract fixes and robustness work found by a 150-agent eval. Read the ACTUAL source for every fix so files[] + approach are real (file:line).

GOAL: a plan to implement scope "${SCOPE}" (the P0 integrity/contract fixes + the P1 robustness items — NOT the P2 compression gains).

${CONTRACTS}

CRITICAL — INDEPENDENT UNITS: each work-unit is built by a separate agent in its own worktree and its commit is cherry-picked onto the branch independently. So:
 - Every unit must own a file-set STRICTLY DISJOINT from every other unit's. No two units may touch the same file.
 - If two fixes touch the same file, OR one depends on another's code change (e.g. the cross-language round-trip fuzz/property TEST depends on the multi-line decoder FIX; a Rust change needs its matching Python decoder change), BUNDLE them into ONE unit — one builder does both sequentially. Bundling dependents is REQUIRED: a separate unit would build against the un-fixed baseline and fail or be meaningless.
 - Result: every unit is self-contained and independently correct against the current baseline, so its commit applies cleanly regardless of order.

For EACH unit: id, tier (P0/P1), title, problem, files (exact repo-relative paths — the disjoint set this unit owns), approach (concrete, file:line), test_plan (the failing test to write first — TDD), acceptance (objective pass condition). Also return disjointness_note (how you guaranteed disjoint file-sets + which dependents you bundled) and integration_order (suggested cherry-pick order, e.g. foundational P0 first, the fuzz-test unit last).`,
  { label: 'plan', phase: 'Plan', model: 'opus', schema: PLAN }
)
const units = (plan?.units || []).slice(0, MAX_UNITS)
log(`plan: ${units.length} independent units`)

// ---------------- Phase 2: BUILD (all units in parallel; each isolated) ----------------
phase('Build')
const built = (await parallel(units.map((u) => () =>
  agent(
`You are a TDD builder (sonnet). Implement ONE work-unit entirely inside your FRESH ISOLATED git worktree, and COMMIT it there so the orchestrator can cherry-pick your commit. You do NOT integrate — just build, verify, commit in your worktree.

${TWO_COPIES}

${GIT_SAFETY}

UNIT ${u.id} — ${u.title}
Problem: ${u.problem || ''}
Files (the disjoint set this unit owns): ${(u.files || []).join(', ')}
Approach: ${u.approach}
Test first (TDD): ${u.test_plan}
Acceptance: ${u.acceptance}

STEPS: (1) ${SETUP} (2) Write the failing test FIRST; run it; confirm it FAILS for the right reason. (3) Implement the smallest fix; if it spans Rust, also update the Python decoder/mirror for byte-parity (still within this unit's files). (4) Rebuild; run your new test + recovery invariant (\`.venv-eval/bin/python -m pytest tests/test_ccr_recovery_invariant.py -q\`) + full suite (\`.venv-eval/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --continue-on-collection-errors --timeout=120\`) + \`cargo test -p headroom-core\`. (5) Verify every HARD CONTRACT holds. (6) \`git status\`, then \`git add\` ONLY the specific source file(s) you changed AND your new test file(s), BY PATH (never \`git add -A\`); confirm your new test is staged; then \`git commit -m "fix(${u.id}): ${u.title}"\` IN YOUR WORKTREE. Report commit_sha=\`git rev-parse HEAD\`, worktree_path=\`pwd\`, files_changed, tests_added.

${CONTRACTS}

If you cannot make it pass without breaking a contract, set self_verified=false, contracts_ok=false, commit_sha="" and explain in notes — never weaken a test/type, never \`git add -A\`, never touch main. Return the structured build result.`,
    { label: `build:${u.id}`, phase: 'Build', model: 'sonnet', isolation: 'worktree', schema: BUILD }
  )
))).filter(Boolean)

const ready = built.filter((b) => b && b.self_verified && b.contracts_ok && b.commit_sha)
log(`build: ${built.length} returned, ${ready.length} self-verified + committed`)

return { scope: SCOPE, plan, built, ready }
