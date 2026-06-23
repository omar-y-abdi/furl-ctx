// ---------------------------------------------------------------------------
// adversarial-critique — fresh-eyed, ALL-OPUS, whole-codebase critique (Headroom)
//
// No scope. ~10 DIVERSE opus critics survey the entire tree, each from a distinct
// senior-engineer lens, hunting everything that CAN or NEEDS to be improved —
// architecture, correctness, simplicity (lazy-dev), types, perf, security, tests,
// docs, API. Loop-until-dry. Then an ADVERSARIAL challenge stage separates MATERIAL
// criticism from taste / by-design / already-handled (so the report is real signal,
// not nitpick noise). Synth = ranked critique + honest overall verdict. REPORT-ONLY.
//
// RUN: Workflow({ scriptPath:'<this>', args:{ map:'<abs path or "">'} })
// ---------------------------------------------------------------------------

export const meta = {
  name: 'adversarial-critique',
  description: 'Fresh-eyed all-opus whole-codebase critique for Headroom: ~10 diverse senior-engineer lenses hunt everything improvable (architecture, correctness, simplicity, types, perf, security, tests, docs, API), loop-until-dry, then adversarially separate material criticism from taste/by-design. Report-only ranked critique + honest verdict.',
  whenToUse: 'When you want an unvarnished, comprehensive, fresh-eyed critique of the whole repo — what can and needs to be improved, no scope limits — with adversarial filtering so the findings are material, not nitpicks.',
  phases: [
    { title: 'Critique', detail: '~10 diverse opus lenses × loop-until-dry', model: 'opus' },
    { title: 'Challenge', detail: 'adversarially separate material from taste/by-design', model: 'opus' },
    { title: 'Verdict', detail: 'rank + honest overall verdict', model: 'opus' },
  ],
}

const MAP = (args && args.map) || ''

const ORIENT =
  'Repo: /Users/k/dev/headroom (branch verify/phase2-audit-report). A FORKED LLM-context COMPRESSION ENGINE: ' +
  'Rust core crates/headroom-core (+ pyo3 bridge crates/headroom-py) + Python headroom/{transforms,ccr,cache,...}. ' +
  '~36.6k code LOC (Rust 22.6k + Python 14k). Public entry: `from headroom import compress`. The live route is ' +
  'compress() -> TransformPipeline -> CacheAligner -> ContentRouter -> compressors (Kompress/Search/Log/SmartCrusher/' +
  'csv_schema) + a CCR store (compression_store.py) with a `<<ccr:HASH>>` sentinel recovery plane + an MCP server ' +
  '(ccr/mcp_server.py). Hard invariants: CCR recovery 100% byte-exact, Py<->Rust hash parity, prompt-cache prefix ' +
  'ordering. The proxy was removed (standalone hook/MCP fork). 626 tests, ~59% coverage. ' +
  (MAP ? `A code map may exist at ${MAP} — read it for orientation. ` : '') +
  'Use Read/Grep/Glob + read-only shell to survey BROADLY (sample many files across your lens, not just one). ' +
  'Exclude archive/, target/, .venv*, .git/, node_modules/. READ-ONLY: never Edit/Write/git-mutate/build.'

const STANCE =
  'YOUR STANCE: a sharp, fresh-eyed senior/staff engineer who just inherited this repo and is doing an honest, ' +
  'UNVARNISHED review. Assume NOTHING is good until you have read it. The owner believes the codebase is well-written — ' +
  'your job is to TEST that claim and surface what they cannot see. Be specific and concrete: every criticism = a real ' +
  'file:line + the actual problem + why it matters + a concrete improvement. Lazy-dev lens throughout: the best code is ' +
  'code never written — flag over-engineering, speculative abstraction, anything that should not exist, redundancy, ' +
  'accidental complexity. But go BEYOND bloat: design/architecture debt, correctness/edge-case/invariant risks, weak ' +
  'types, perf foot-guns, security gaps, brittle or low-value tests, stale/misleading docs, inconsistent or leaky APIs. ' +
  'NO SCOPE LIMITS. Rank your findings by real impact. Do not invent problems to fill a quota — if a thing is genuinely ' +
  'good, say so briefly; spend your effort on what is genuinely improvable. Severity honestly: critical / high / medium / low / nitpick.'

const FINDING = {
  type: 'object',
  properties: { findings: { type: 'array', items: {
    type: 'object',
    properties: {
      lens: { type: 'string' },
      theme: { type: 'string', enum: ['architecture', 'correctness', 'simplicity', 'types', 'performance', 'security', 'tests', 'docs', 'api', 'other'] },
      severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'nitpick'] },
      title: { type: 'string' },
      location: { type: 'string', description: 'file:line(s) or module' },
      problem: { type: 'string' },
      why_it_matters: { type: 'string' },
      improvement: { type: 'string', description: 'the concrete fix / better approach' },
      effort: { type: 'string', enum: ['trivial', 'small', 'medium', 'large'] },
      confidence: { type: 'number' },
    },
    required: ['lens', 'theme', 'severity', 'title', 'location', 'problem', 'improvement'],
  } }, praise: { type: 'array', items: { type: 'string', description: 'genuinely-good things worth keeping (brief)' } } },
  required: ['findings'],
}

const CHALLENGE = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['material', 'taste', 'by-design', 'already-handled', 'wrong'] },
    reasoning: { type: 'string' },
    severity_adjusted: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'nitpick'] },
  },
  required: ['verdict', 'reasoning'],
}

// ~10 DIVERSE opus lenses — each a distinct senior-engineer perspective (multi-perspective: diversity catches what redundancy can't).
const LENSES = [
  { key: 'architecture', type: 'code-architect', focus: 'ARCHITECTURE & DESIGN across the whole tree: module boundaries, coupling, abstraction debt, the Py<->Rust split, the transforms/ccr/cache layering, the compress() pipeline shape, leaky seams, god-objects/files, circular or surprising dependencies, the hook/MCP integration design. Is the structure coherent or accreted? What would you re-draw?' },
  { key: 'rust-core', type: 'ecc:rust-reviewer', focus: 'THE RUST CORE (crates/, 22.6k LOC): idioms, unsafe usage, error handling, lifetimes/ownership smells, the pyo3 bridge surface, allocations on hot paths, panics-as-control-flow, the SmartCrusher/compaction/formatter complexity, dead feature-gates. Is the Rust carrying its weight or over-built?' },
  { key: 'python', type: 'ecc:python-reviewer', focus: 'THE PYTHON (headroom/, 14k LOC): PEP8/idioms, type-hint quality, the public __all__/_LAZY_EXPORTS surface, the ContentRouter/SmartCrusher Python side, thread-locals, the config dataclasses, error handling, dynamic imports. Pythonic or fighting the language?' },
  { key: 'types', type: 'ecc:type-design-analyzer', focus: 'TYPE DESIGN: are invariants expressed in types or enforced by convention? stringly-typed states, boolean-blindness, primitive obsession (hashes/tokens/ratios as bare str/int/float), illegal states representable, Optional/Result honesty, the CCR sentinel + hash + ratio types. Where would a stronger type kill a class of bug?' },
  { key: 'correctness', type: 'code-reviewer', focus: 'CORRECTNESS & ROBUSTNESS, fresh bug hunt (independent of the recent 25-bug pass — find MORE): edge cases, silent-failure/swallowed-error paths, off-by-one in size/token/threshold math, state leaking across calls, concurrency (the worker threads, semaphores, thread-locals), malformed-input handling, the recovery/eviction/parity invariant boundaries. What breaks?' },
  { key: 'simplicity', type: 'Explore', focus: 'LAZY-DEV SIMPLICITY (the ladder): what should NOT exist — single-impl abstractions, factories-of-one, delegating wrappers, speculative params/config nobody sets, hand-rolled stdlib, dead feature-gates, parallel implementations of one idea, accidental complexity. Where is this engine doing in 200 lines what 50 would do? Be ruthless: the best code is code never written.' },
  { key: 'performance', type: 'ecc:performance-optimizer', focus: 'PERFORMANCE of a COMPRESSION engine: hot-path allocations, repeated work, O(n^2) over messages/rows, tokenizer/hash recomputation, the Py<->Rust boundary crossings, the CCR store lookups, run_bench-relevant paths. Where is real measurable waste (not premature micro-opt)?' },
  { key: 'compression-efficacy', type: 'computer-scientist-analyst', focus: 'THE COMPRESSION ITSELF — the highest-value question for THIS engine: is the approach actually GOOD, and could it be FUNDAMENTALLY better? Evaluate the achieved ratios vs what is theoretically/practically achievable on the target data (tool outputs, logs, JSON arrays). Is row-drop + CCR-offload the right architecture, or a workaround? Is the SmartCrusher routing/strategy sound? What known-better techniques (dictionary/entropy coding, structural diffing, schema-aware columnar, semantic dedup, learned compressors) are NOT used and could lift it? CHALLENGE the lossy-by-deletion design AS A CHOICE (not just "is it honest about it") — when is dropping rows the wrong call vs a recoverable transform? Read run_bench/BENCHMARKS.md + the compaction/formatter/crusher Rust. Where is the engine leaving compression on the table?' },
  { key: 'security', type: 'ecc:security-reviewer', focus: 'SECURITY & DATA-SAFETY: the log redaction (_redact_retrieval_log_payload), input validation at the compress()/hook/MCP boundaries, the CCR store keying/eviction (data confusion/overwrite), untrusted tool-output handling, the explicit_hash/ttl guards, any injection/path/credential exposure, the MCP retrieve plane. What could leak or be abused?' },
  { key: 'tests', type: 'ecc:pr-test-analyzer', focus: 'TEST-SUITE QUALITY with fresh eyes (the suite was just hardened — be skeptical of that too): are the 626 tests actually mutation-resistant, or coverage-theater? over-stubbed paths, env-gated code never really exercised, round-trip tests that are parallel-mutation-blind, missing boundary/property tests, the recovery/parity invariant coverage, flakiness. What does green NOT prove here?' },
  { key: 'docs-api', type: 'ecc:comment-analyzer', focus: 'DOCS, COMMENTS & API HONESTY: stale/misleading docstrings & comments (comment rot), the README/wiki vs reality (the honest savings claims, removed features), the public API consistency & discoverability, naming, the compression-format documentation, anything that would mislead a new contributor or a consumer of the library. Does the doc match the code?' },
  { key: 'staff-skeptic', type: 'claude', focus: 'THE HOLISTIC STAFF-ENGINEER TAKE: step back. If you inherited this repo today, what are the TOP few things that would genuinely alarm or disappoint you? What is the single biggest weakness? Is the engineering honest about its own limits (the lossy-by-deletion / sampling-blindness weakness, the compression ceilings)? Is it production-ready for its stated use (hook/MCP for Claude Code/Codex)? What is the elephant in the room nobody named? Give the unvarnished verdict + the 3-5 highest-leverage improvements.' },
]

const seenKey = (f) => `${(f.location || '').toLowerCase().slice(0, 48)}|${(f.title || '').toLowerCase().slice(0, 40)}`
const critiquePrompt = (l, round, seen) =>
  `${ORIENT}\n\n${STANCE}\n\nYOUR LENS (${l.key}): ${l.focus}\n\n` +
  (round > 1
    ? `Round ${round}. Already-found (do NOT repeat — find what these MISSED, go deeper or wider):\n${seen.slice(0, 70).join('\n')}\n\nReport ONLY genuinely-new criticism your lens uniquely sees.`
    : `Survey broadly across the tree from your lens. Report every real, concrete finding (and a short praise list of what is genuinely good).`) +
  `\nYou are OPUS and fresh-eyed — bring the depth. Be concrete (file:line), honest on severity, and propose the improvement.`

phase('Critique')
const seen = new Set()
const all = []
const praise = []
const notRun = new Set()
let round = 0, dry = 0
const MAX_ROUNDS = 2
while (round < MAX_ROUNDS && dry < 1 && (!budget.total || budget.remaining() > 200_000)) {
  round++
  const res = await parallel(LENSES.map(l => () =>
    agent(critiquePrompt(l, round, [...seen]), { label: `R${round}:${l.key}`, phase: `Critique R${round}`, schema: FINDING, agentType: l.type, model: 'opus' })
  ))
  const dead = LENSES.filter((l, i) => !res[i]).map(l => l.key)
  if (dead.length) { dead.forEach(k => notRun.add(k)); log(`⚠ round ${round}: lenses NULL (NOT RUN, not "clean"): ${dead.join(', ')}`) }
  const fresh = res.filter(Boolean).flatMap(r => (r.findings || [])).filter(f => f && f.location && !seen.has(seenKey(f)))
  fresh.forEach(f => seen.add(seenKey(f)))
  all.push(...fresh)
  res.filter(Boolean).forEach(r => (r.praise || []).forEach(p => praise.push(p)))
  log(`Critique round ${round}: ${fresh.length} new findings (${all.length} total)`)
  if (fresh.length < 6) dry++
}

phase('Challenge')
// Adversarially challenge every non-nitpick finding: material, or taste/by-design/already-handled/wrong? (keeps the report real signal)
const toChallenge = all.filter(f => f.severity !== 'nitpick')
const challenged = await parallel(toChallenge.map(f => () =>
  agent(
    `${ORIENT}\n\nYou are an adversarial reviewer of a CRITIQUE. A fresh-eyed critic (${f.lens}) claims:\n` +
    `[${f.severity}/${f.theme}] ${f.title}\nLocation: ${f.location}\nProblem: ${f.problem}\nWhy: ${f.why_it_matters || 'n/a'}\n` +
    `Proposed improvement: ${f.improvement}\n\nGO READ THE ACTUAL CODE at that location. Verdict: material (a real, worth-fixing ` +
    `issue), taste (subjective/style preference, not a defect), by-design (a deliberate, justified choice — e.g. an invariant ` +
    `that must live internally, a calibration knob, a documented tradeoff), already-handled (a guard/test/path the critic missed), ` +
    `or wrong (the critic misread the code). Default to a HONEST adjusted severity. Be fair: confirm the real ones, deflate the rest.`,
    { label: `challenge:${f.lens}:${(f.title || '').slice(0, 20)}`, phase: 'Challenge', schema: CHALLENGE, agentType: 'Explore', model: 'opus' }
  ).then(v => v && ({ ...f, ...v }))
))
const judged = challenged.filter(Boolean)
const material = judged.filter(f => f.verdict === 'material')
const byDesign = judged.filter(f => f.verdict === 'by-design')
const reallyDeflated = judged.filter(f => f.verdict === 'taste' || f.verdict === 'already-handled' || f.verdict === 'wrong')
const nitpicks = all.filter(f => f.severity === 'nitpick')
log(`Challenge: ${toChallenge.length} challenged; ${material.length} MATERIAL, ${judged.filter(f => f.verdict === 'taste').length} taste, ${judged.filter(f => f.verdict === 'by-design').length} by-design, ${judged.filter(f => f.verdict === 'already-handled').length} already-handled, ${judged.filter(f => f.verdict === 'wrong').length} wrong.`)

phase('Verdict')
const synth = await agent(
  `${ORIENT}\n\nYou are a staff engineer writing the HONEST CRITIQUE of this codebase the owner asked for ("test my claim that ` +
  `it's well-written; tell me what fresh eyes see"). Below: MATERIAL findings (adversarially-confirmed), plus taste/by-design/` +
  `already-handled (deflated), plus praise. Write codebase-CRITIQUE.md as MARKDOWN:\n` +
  `- OPEN with the UNVARNISHED VERDICT (3-5 sentences): is this codebase actually well-written? what is its real engineering ` +
  `character, its single biggest strength and single biggest weakness? Be honest, not flattering.\n` +
  `- TOP IMPROVEMENTS table FIRST, ranked by leverage (impact/effort): rank | theme | severity | what | location | the improvement | effort.\n` +
  `- Then BY THEME (architecture, correctness, simplicity, types, performance, security, tests, docs, api): the material findings, ` +
  `each with location + concrete improvement. Group, dedupe across lenses (cite all lenses that caught it).\n` +
  `- A "DELIBERATE CHOICES WORTH RE-EXAMINING" section: the adversarial pass ruled these by-design, BUT a deliberate choice can ` +
  `still be the blind spot the owner asked to have pierced. Surface each AIRED WITH ITS TRADEOFF (what was chosen, what it costs, ` +
  `when it would be the wrong call) — do NOT bury these in the deflated appendix. The biggest such choice is the lossy-by-deletion / ` +
  `row-drop+CCR architecture and the proxy-removal; treat them as open questions, not settled.\n` +
  `- A short "DEFLATED" appendix: claims the adversarial pass ruled taste / already-handled / wrong (so the owner sees the critique ` +
  `was filtered, not credulous) — this is what keeps the report honest.\n` +
  `- A "GENUINELY GOOD" section: what the fresh eyes agreed is well-done (from praise + confirmed-good) — brief, earned, not flattery.\n` +
  `- This repo was just through a heavy human+AI session: proxy route DELETED (standalone hook/MCP fork), cleanup was archive-not-` +
  `refactor, a 25-bug test-hardening pass, score.py used to grade test quality. If any finding bears on THOSE decisions, relay it at ` +
  `FULL STRENGTH — do not soften criticism of the recent work just because it was recent.\n` +
  `- CLOSE with the 3-5 highest-leverage next moves.\n` +
  `- Note any lens that returned NOT-RUN (coverage gap, not "nothing wrong"): ${JSON.stringify([...notRun])}.\n` +
  `- REPORT-ONLY: this critiques, it changes nothing.\n\n` +
  `MATERIAL:\n${JSON.stringify(material).slice(0, 16000)}\n\nDELIBERATE-CHOICES (by-design — air the tradeoff, don't bury):\n${JSON.stringify(byDesign).slice(0, 5000)}\n\nDEFLATED (taste/already-handled/wrong):\n${JSON.stringify(reallyDeflated).slice(0, 4000)}\n\nNITPICKS:\n${JSON.stringify(nitpicks).slice(0, 2000)}\n\nPRAISE:\n${JSON.stringify(praise).slice(0, 2000)}`,
  { label: 'verdict-synthesis', phase: 'Verdict', agentType: 'claude', model: 'opus' }
)

return {
  branch: 'verify/phase2-audit-report',
  lenses: LENSES.length,
  rounds: round,
  totalFindings: all.length,
  material: material.length,
  byDesign: byDesign.length,
  deflated: reallyDeflated.length,
  nitpicks: nitpicks.length,
  notRun: [...notRun],
  critique: synth,
  materialFindings: material,
}
