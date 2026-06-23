// ---------------------------------------------------------------------------
// test-quality-audit — READ-ONLY test-hardening planner for Headroom (Phase 4)
//
// The suite is coverage-ish but mutation-WEAK (score.py baseline: B1_fixed_vector=0,
// D2_param=0.017, A2_private=166). This pass audits the high-value LIVE modules
// against the test-quality 10-rule anti-fragility contract, finds untested behaviors
// where bugs hide, and produces a RANKED hardening plan + adversarially-confirmed
// bug suspects. The teammate then IMPLEMENTS sequentially (safe — no parallel
// suite/git race). REPORT-ONLY: no test is written here.
//
// RUN: Workflow({ scriptPath:'<this>', args:{ baseline:'<abs path>' } })
// ---------------------------------------------------------------------------

export const meta = {
  name: 'test-quality-audit',
  description: 'Read-only test-hardening planner for Headroom Phase 4: audit high-value LIVE modules against the test-quality anti-fragility contract, find untested behaviors where bugs hide, adversarially confirm bug suspects, produce a ranked per-module hardening plan. Report-only; the teammate implements.',
  whenToUse: 'Plan a mutation-resistance test-hardening pass after capturing a coverage + score.py baseline. Finds contract violations, boundary gaps, and real bugs per module; ranks by bug-finding value.',
  phases: [
    { title: 'Audit', detail: 'per-module contract audit (parallel)' },
    { title: 'ConfirmBugs', detail: 'adversarial confirm/refute each suspected bug' },
    { title: 'Plan', detail: 'rank hardening plan + confirmed bugs' },
  ],
}

const BL = (args && args.baseline) || '/Users/k/dev/headroom/.claude/runtime/test-baseline.md'

const READONLY =
  'READ-ONLY test auditor. Use ONLY Read/Grep/Glob + read-only shell (rg, grep, wc, sed -n, ' +
  '.venv/bin/python -c "..." for REPL-verifying library/engine behavior, .venv/bin/python -m pytest -k ... --co for collection). ' +
  'NEVER Edit/Write/NotebookEdit, NEVER git-mutate, NEVER maturin/cargo build, NEVER pip-install. ' +
  'Repo /Users/k/dev/headroom (branch verify/phase2-audit-report @ 90b1df8a). Exclude archive/, target/, .venv*. ' +
  `Read the baseline first: ${BL}. Your final output is DATA for a synthesizer, not a human message.`

const CONTRACT =
  'THE TEST-QUALITY ANTI-FRAGILITY CONTRACT (the standard a hardened test meets — flag every violation + every gap):\n' +
  '1. Assert exception TYPE + structured payload, not error-message wording (substring match = brittle).\n' +
  '2. PIN a fixed expected literal; never recompute the expected value with the same lib/logic the code uses (parallel-mutation blind). ★ baseline B1=0 — this is the biggest gap.\n' +
  '3. Test through the PUBLIC API; no private-symbol imports / obj._private() (★ baseline A2=166 — heavy).\n' +
  '4. Test that a value AFFECTS behavior, not a constructor readback (readbacks survive every mutation).\n' +
  '5. No or-joined assertions that accept either outcome.\n' +
  '6. Parametrize / fixtures / inheritance; do not unroll near-identical cases (★ baseline D2=0.017).\n' +
  '7. ★ BOUNDARY test EVERY comparison in the source: enumerate each <, <=, >, >=, ==, != and check each has a test at the boundary (off-by-one is the classic mutation survivor). For ordering code, inputs where plain vs reversed picks different results.\n' +
  '8. REPL-verify any stdlib/engine assumption before asserting on it (run it, do not assert from memory).\n' +
  '9. Prefer real-I/O fixtures (tmp_path, real store, real router) over unittest.mock.\n' +
  '10. Coverage is a NON-REGRESSION FLOOR, not the goal. A test that only adds coverage without mutation-sensitivity is not a win.'

const HUNT =
  'YOUR GOAL is the user\'s thesis: better tests FIND BUGS → improve the engine → better compression. So beyond cataloguing ' +
  'contract violations, actively HUNT untested behaviors where a real bug could hide — especially on the hard invariants ' +
  '(CCR recovery byte-exact, Py<->Rust hash parity, prompt-cache prefix ordering idx0/order/cache_control, default lossless ' +
  'decode). The eval `break` pass already found silent-loss holes this way (multi-line CSV field -> total loss; inter-call ' +
  'eviction -> unbacked sentinel). Look for: lossy paths with no recovery test, boundary/off-by-one in size/token/threshold ' +
  'math, branches that silently pass-through on malformed input, state that leaks across calls, dict/order assumptions. ' +
  'For each suspected bug: name the exact file:line, the input that triggers it, and the wrong behavior. REPL-verify if you can.'

const AUDIT_SCHEMA = {
  type: 'object',
  properties: {
    module: { type: 'string' },
    current_cov_pct: { type: 'number' },
    contract_violations: { type: 'array', items: { type: 'object', properties: {
      rule: { type: 'string' }, where: { type: 'string' }, fix: { type: 'string' },
    }, required: ['rule', 'where', 'fix'] } },
    boundary_gaps: { type: 'array', items: { type: 'string', description: 'a source comparison (file:line, the op) with no boundary test' } },
    suspected_bugs: { type: 'array', items: { type: 'object', properties: {
      title: { type: 'string' }, location: { type: 'string' }, trigger: { type: 'string' },
      wrong_behavior: { type: 'string' }, invariant_at_risk: { type: 'string' }, repl_checked: { type: 'boolean' },
    }, required: ['title', 'location', 'wrong_behavior'] } },
    test_plan: { type: 'array', items: { type: 'string', description: 'one concrete test to add/refactor, contract-aligned' } },
    est_new_tests: { type: 'number' },
    priority: { type: 'number', description: '1=highest bug-finding value' },
  },
  required: ['module', 'contract_violations', 'boundary_gaps', 'suspected_bugs', 'test_plan', 'priority'],
}

const BUG_VERDICT = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['real', 'false_alarm', 'uncertain'] },
    evidence: { type: 'string' },
    repro: { type: 'string', description: 'the minimal input/trace that demonstrates it, or why it cannot trigger' },
  },
  required: ['verdict', 'evidence'],
}

// 8 high-value targets (LIVE engine/compress-path × low coverage), from the baseline.
const MODULES = [
  { key: 'kompress_compressor', path: 'headroom/transforms/kompress_compressor.py', test: 'tests/test_*kompress*.py', cov: 13, note: 'LIVE default text/code compressor — biggest untested surface (586 stmts)' },
  { key: 'cache_aligner', path: 'headroom/transforms/cache_aligner.py', test: 'tests/test_*cache_align*.py', cov: 16, note: 'LIVE compress-path; guards the cache-PREFIX ORDERING invariant — highest-stakes' },
  { key: 'smart_crusher', path: 'headroom/transforms/smart_crusher.py', test: 'tests/test_*smart_crush*.py', cov: 70, note: 'core engine: TOIN loop, anchor selection, crush/offload' },
  { key: 'content_router', path: 'headroom/transforms/content_router.py', test: 'tests/test_transforms_content_router.py', cov: 60, note: 'routes EVERY compress() call — the dispatch/role-skip/protect logic' },
  { key: 'parser', path: 'headroom/parser.py', test: 'tests/test_*parser*.py', cov: 42, note: 'parsing — boundary/off-by-one bug-prone (221 stmts)' },
  { key: 'mcp_server', path: 'headroom/ccr/mcp_server.py', test: 'tests/test_ccr_eviction_loud_miss.py', cov: 20, note: 'the user\'s MCP retrieve plane — must be solid; just un-coupled from proxy' },
  { key: 'ccr_store', path: 'headroom/cache/compression_store.py', test: 'tests/test_ccr_recovery_invariant.py', cov: 0, note: 'CCR store: recovery byte-exact + eviction + feedback. 21 recovery tests exist — harden internals, boundaries, eviction edges' },
  { key: 'csv_schema_decoder', path: 'headroom/transforms/csv_schema_decoder.py', test: 'tests/test_ccr_recovery_invariant.py', cov: 0, note: 'the lossless recovery decoder (the TRAP) — byte-exact reverse; multi-line CSV field was a prior silent-loss hole' },
]

const auditPrompt = (m) =>
  `${READONLY}\n\n${CONTRACT}\n\n${HUNT}\n\nYOUR MODULE: ${m.path} (current coverage ~${m.cov}%). ${m.note}\n` +
  `Read the module source AND its existing tests (find them: ls/grep tests/ for the module's symbols; hint pattern ${m.test}). ` +
  `Enumerate every source comparison for boundary gaps. Inventory contract violations in the EXISTING tests (cite test file:line). ` +
  `Hunt suspected bugs per the goal above (REPL-verify with .venv/bin/python -c where you can). Produce a concrete, contract-aligned ` +
  `test_plan (each item = one test to add or one fragile test to rewrite). Set priority 1 (highest bug value) .. 5. Be a careful senior ` +
  `test engineer: real mutation-resistance + real bugs only, no coverage-chasing busywork, no tests through private internals.`

phase('Audit')
const audits = await parallel(
  MODULES.map(m => () => agent(auditPrompt(m), { label: `audit:${m.key}`, phase: 'Audit', schema: AUDIT_SCHEMA, agentType: 'ecc:pr-test-analyzer' }))
)
const liveAudits = audits.filter(Boolean)
const deadAudits = MODULES.filter((m, i) => !audits[i]).map(m => m.key)
if (deadAudits.length) log(`⚠ audit lenses returned NULL (NOT RUN, not "clean"): ${deadAudits.join(', ')}`)
const allBugs = liveAudits.flatMap(a => (a.suspected_bugs || []).map(b => ({ ...b, module: a.module })))
log(`Audit: ${liveAudits.length}/${MODULES.length} modules; ${allBugs.length} suspected bugs; ${liveAudits.reduce((n, a) => n + (a.test_plan || []).length, 0)} planned tests`)

phase('ConfirmBugs')
// Adversarially confirm each suspected bug — REPL/trace it, default to uncertain over a confident wrong verdict.
const confirmed = await parallel(
  allBugs.map(b => () =>
    agent(
      `${READONLY}\n\nYou are a skeptical bug-confirmer. A test-audit flagged this SUSPECTED bug in ${b.module}:\n` +
      `Title: ${b.title}\nLocation: ${b.location || 'n/a'}\nTrigger: ${b.trigger || 'n/a'}\nWrong behavior: ${b.wrong_behavior}\n` +
      `Invariant at risk: ${b.invariant_at_risk || 'n/a'}\n\nTRY TO REPRODUCE IT: read the exact code path, and REPL-verify with ` +
      `.venv/bin/python -c "..." (construct the trigger input, call the public path, observe). Verdict: real (you reproduced wrong ` +
      `behavior), false_alarm (the code is correct / a guard you missed handles it), or uncertain. Default to uncertain over a ` +
      `confident wrong verdict. Give the minimal repro or why it cannot trigger.`,
      { label: `confirm:${(b.location || b.title).slice(0, 24)}`, phase: 'ConfirmBugs', schema: BUG_VERDICT, agentType: 'Explore' }
    ).then(v => v && ({ ...b, ...v }))
  )
)
const verifiedBugs = confirmed.filter(Boolean)
const realBugs = verifiedBugs.filter(b => b.verdict === 'real')
const uncertainBugs = verifiedBugs.filter(b => b.verdict === 'uncertain')
log(`ConfirmBugs: ${realBugs.length} REAL, ${uncertainBugs.length} uncertain, ${verifiedBugs.filter(b => b.verdict === 'false_alarm').length} false-alarm (dropped).`)

phase('Plan')
const synth = await agent(
  `${READONLY}\n\nYou are writing the Phase-4 test-hardening PLAN for Headroom. Inputs below: per-module audits (JSON) + ` +
  `adversarially-confirmed bugs. Write test-hardening-PLAN.md as MARKDOWN:\n` +
  `- OPEN with the headline: total planned tests, # REAL bugs confirmed (these validate the thesis: better tests -> find bugs -> ` +
  `better compression), and the score.py axes to move.\n` +
  `- ★ PRIORITIZE B1_fixed_vector (0 -> up): pinning recovered-byte / compressed-output LITERALS is the best lever AND is ` +
  `behavior-SAFE — it directly reinforces the recovery invariant, no risk. Lead the per-module plans with B1 + boundary tests.\n` +
  `- ★ A2_private_symbol (166) is NOT a blanket "make public": some invariants LIVE INTERNALLY on this engine (Py<->Rust hash ` +
  `parity, the <<ccr:HASH>> sentinel format, anchor-selector internals) with NO public path — testing them via internals is a ` +
  `LEGITIMATE KEEP, not a violation. Only reduce A2 where a genuine public path reaches the same behavior. Never delete ` +
  `internal-invariant coverage to win the axis.\n` +
  `- CONFIRMED BUGS FIRST (table: bug | module | location | repro | invariant-at-risk) — these are SURFACED TO THE USER as a ` +
  `fix / defer / intended-behavior DECISION. ★ Do NOT instruct the teammate to fix them: a bug fix changes invariant-bound engine ` +
  `behavior (recovery/parity/cache-prefix) which is NOT pre-authorized — and the eval \`break\` Cluster G looked like a silent-loss ` +
  `bug but was BY-DESIGN. Bug-fixing and test-hardening are SEPARATE TRACKS. Mark uncertain bugs separately (needs-review).\n` +
  `- ★ TEST-HARDENING LOCKS CURRENT BEHAVIOR and proceeds independently of the bug decisions: a mutation-sensitive test pins what ` +
  `the engine does TODAY (so a future change is caught), it does not assert a hoped-for fix. (A test asserting "correct" behavior ` +
  `for a real bug would fail the all-pass gate — another reason the tracks are separate.)\n` +
  `- PER-MODULE HARDENING PLAN, ranked by priority (bug value): module | current cov | top contract violations | boundary gaps | ` +
  `concrete test_plan | est new tests. The teammate works this list TOP-DOWN, iterate-to-plateau per module, coverage as floor, ` +
  `gate (full pytest green + recovery 21 + module cov >= floor + every new test mutation-sensitive).\n` +
  `- Note any module whose audit lens returned NOT-RUN (coverage gap, not "clean"): ${JSON.stringify(deadAudits)}.\n` +
  `- Two-track reminder: this plan is PYTHON (score.py-validated). Rust hardening is a separate hand pass (+ optional cargo-mutants).\n` +
  `- End: REPORT-ONLY; the teammate implements sequentially (no parallel suite/git race).\n\n` +
  `AUDITS:\n${JSON.stringify(liveAudits).slice(0, 16000)}\n\nCONFIRMED BUGS:\n${JSON.stringify(verifiedBugs)}`,
  { label: 'synthesize-plan', phase: 'Plan', agentType: 'ecc:planner' }
)

return {
  branch: 'verify/phase2-audit-report',
  head: '90b1df8a',
  modulesAudited: liveAudits.length,
  notRun: deadAudits,
  suspectedBugs: allBugs.length,
  realBugs: realBugs.length,
  uncertainBugs: uncertainBugs.length,
  plannedTests: liveAudits.reduce((n, a) => n + (a.test_plan || []).length, 0),
  plan: synth,
  audits: liveAudits,
  confirmedBugs: verifiedBugs,
}
