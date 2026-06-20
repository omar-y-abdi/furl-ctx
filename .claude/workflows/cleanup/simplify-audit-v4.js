// ---------------------------------------------------------------------------
// simplify-audit-v4 — 4th-pass FEATURE-REACHABILITY fat-finder (Headroom)
//
// Context: Tier-1+2 cuts already applied (~9.6k LOC). Tools now come back CLEAN
// (cargo 0-warnings, vulture 2, ruff 34 line-nits). So the residual fat is NOT
// line-level dead code — it's whole VESTIGIAL FEATURE MODULES that import cleanly
// but are never invoked on the live compress() route, plus the public-surface
// deprecation tail. This pass traces reachability from the real entry points.
//
// REPORT-ONLY. Read-only specialized lenses → loop-until-dry (cap 2) →
// perspective-diverse adversarial verify → completeness critic → synth markdown.
//
// RUN:  Workflow({ scriptPath:'<this>', args:{ groundTruth:'<abs path>' } })
// ---------------------------------------------------------------------------

export const meta = {
  name: 'simplify-audit-v4',
  description: '4th-pass feature-reachability fat-finder for Headroom: after Tier-1+2 cuts left the tree tool-clean, trace whole vestigial feature-modules (import-live but never run on the compress() route), the Rust reachability-only dead set, and the public-surface deprecation tail. 6 read-only lenses, loop-until-dry, perspective-diverse verify, completeness critic. Report-only, ranked.',
  whenToUse: 'Re-audit Headroom AFTER applied cut-rounds have made static tools clean. Finds vestigial feature modules, runtime-unreachable pub items, and API-surface bloat that vulture/ruff/cargo cannot see — reachability-traced from compress()/hook/mcp entries.',
  phases: [
    { title: 'Sweep', detail: '6 reachability lenses × loop-until-dry (cap 2)' },
    { title: 'Verify', detail: 'perspective-diverse refutation of vestigial/delete claims' },
    { title: 'Critic', detail: 'completeness — what feature/surface is still unaudited' },
    { title: 'Synthesize', detail: 'dedup + rank biggest-safe-cut-first' },
  ],
}

const GT = (args && args.groundTruth) || '/Users/k/dev/headroom/.claude/runtime/fat-groundtruth-v4.md'

const READONLY =
  'READ-ONLY auditor. Use ONLY Read/Grep/Glob and read-only shell (rg, grep, wc, find, ' +
  'sed -n, cat, git log/blame, .venv/bin/vulture, .venv/bin/ruff, .venv/bin/deptry, `cargo check`). ' +
  'NEVER Edit/Write/NotebookEdit; NEVER git mutate; NEVER pip-install or maturin/cargo build. ' +
  'Exclude target/, .venv*, .sccache/, .claude/worktrees/, *.so, node_modules/, archive/, .git/, AND proxy/ ' +
  '(proxy is CONDEMNED — deleted in step 3 — do not audit its internals). Repo root: /Users/k/dev/headroom ' +
  '(branch verify/phase2-audit-report @ de3fd231). ' +
  `Fresh post-cut tool ground-truth is at ${GT} — Read it FIRST. ` +
  'Your final output is DATA for a synthesizer, not a human message.'

const REACH =
  'THE CORE METHOD — reachability from the LIVE ENTRY POINTS. The live route is: public compress() ' +
  '(headroom/compress.py → TransformPipeline → CacheAligner → ContentRouter selects a compressor by policy) ' +
  'PLUS the real harness entries (the planned PostToolUse hook + ccr/mcp_server.py retrieve plane). ' +
  'For any module/feature/symbol, classify by checking ALL FOUR coupling vectors: (1) static import; ' +
  '(2) lazy/deferred import inside functions; (3) dynamic import (importlib/__import__/string names) + ' +
  'headroom/__init__ + cache/__init__ _LAZY_EXPORTS re-export tables + pyproject entry_points; ' +
  '(4) for Rust, the pyo3 #[pyfunction]/#[pymethods] bridge in crates/headroom-py/src/lib.rs. ' +
  'Verdict: LIVE = actually executed by a normal compress() / mcp retrieve; TEST-ONLY = only its own ' +
  'tests construct/call it (import-resolves + cargo/pytest green but zero production caller); ' +
  'VESTIGIAL = exported/imported but only in a dead or never-taken branch; DEAD = no referrer at all. ' +
  'A clean import / green test / public export is NOT proof of liveness — that is exactly the gate-blindness ' +
  'this pass exists to pierce. For each non-LIVE finding name the exact untangle (which __init__/registry/ ' +
  'mod.rs edge to cut). NEVER flag the hard invariants or the CONFIRMED-LIVE set in the ground-truth.'

const PRIOR =
  'CONTEXT. Three prior passes + two applied cut-rounds already happened. ALREADY CUT (in archive/, do not ' +
  're-report): Rust pipeline/ subtree, safety.rs, live_zone.rs, recommendations.rs, lib.rs live_zone FFI; ' +
  'Python conftest fixtures, ccr/batch_processor.py, stale JSON. CONFIRMED LIVE (do not flag): telemetry/, ' +
  'onnx_runtime.py, wiki/, ml_models.py+dynamic_detector.py, compression_feedback.py, relevance/bm25.py+base.py, ' +
  'cache optimizer cluster (public surface), ccr/mcp_server.py, SmartCrusher, ccr/ store, tokenizer*, ' +
  'cache_control.rs, log/diff/search_compressor.rs, src/auth_mode.rs, src/compression_policy.rs. ' +
  'The job now: find what THREE passes missed — whole vestigial FEATURE modules that resolve but never run, ' +
  'runtime-unreachable Rust, and the API-surface deprecation tail. The bar is HIGH: tools are clean, so a ' +
  'real finding here is a reachability argument, not a tool hit.'

const LADDER =
  'THE LAZY-DEV LADDER — apply as a reflex to every candidate, stop at the FIRST rung that holds, and tag by it. ' +
  'Rung 1 does-this-need-to-exist (speculative/unreached → delete); Rung 2 does stdlib already do it (hand-rolled ' +
  'logic the standard library ships → stdlib, name the function); Rung 3 does a native platform/engine feature ' +
  'cover it (a dep or hand-rolled code doing what the platform/an existing core primitive already does → native, ' +
  'name it); Rung 4 does an already-installed dependency solve it (don\'t hand-roll → dep). Two rungs apply → take ' +
  'the higher (earlier) one. The ladder is the ranking lens: "best code is code never written" — a whole unreached ' +
  'feature (rung 1) outranks a hand-rolled-stdlib shrink (rung 2) outranks a one-line tidy. Do NOT apply the ladder ' +
  'to: input validation at trust boundaries, error handling that prevents data loss, security, the hard invariants, ' +
  'or a hardware/calibration knob — those stay even if they look heavy.'

const TAGS =
  'TAGS (ladder-aligned): delete=dead/vestigial code or whole unreached module (rung 1, replacement: nothing); ' +
  'stdlib=hand-rolled logic the standard library ships (rung 2, name the stdlib function); native=dep or code doing ' +
  'what the platform / an existing engine primitive already does (rung 3, name it); dep=dependency that dies with a ' +
  'vestigial feature (name it); yagni=single-impl abstraction / factory-of-one / delegating wrapper / speculative ' +
  'param nobody passes; deadflag=config field/env/policy-arm never read on the live path; surface=public ' +
  '__all__/_LAZY_EXPORTS export with ~0 runtime instantiation (deprecation candidate, NOT pure-move); shrink=same ' +
  'behavior fewer lines; archaeology=stale orphan file / commented-out / test for an already-cut feature / doc cruft.'

const FINDINGS = {
  type: 'object',
  properties: { findings: { type: 'array', items: {
    type: 'object',
    properties: {
      lens: { type: 'string' },
      tag: { type: 'string', enum: ['delete', 'stdlib', 'native', 'dup', 'dep', 'yagni', 'deadflag', 'surface', 'shrink', 'archaeology'] },
      rung: { type: 'number', description: 'lazy-dev ladder rung that holds (1=delete/unreached … 6=minimum-code); lower outranks' },
      title: { type: 'string' },
      paths: { type: 'string' },
      est_loc_cut: { type: 'number' },
      replacement: { type: 'string' },
      reachability: { type: 'string', enum: ['live', 'test-only', 'vestigial', 'dead', 'na'] },
      safe_to_cut_now: { type: 'boolean' },
      untangle_needed: { type: 'string' },
      evidence: { type: 'string', description: 'the grep/trace proof of NO live caller (the 4-vector check), or tool line, or "manual"' },
      confidence: { type: 'number' },
    },
    required: ['lens', 'tag', 'title', 'paths', 'est_loc_cut', 'reachability', 'safe_to_cut_now'],
  } } },
  required: ['findings'],
}

const VERDICT = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['confirmed', 'refuted', 'uncertain'] },
    blockers: { type: 'string' },
    reasoning: { type: 'string' },
  },
  required: ['verdict', 'reasoning'],
}

// 6 sharp lenses, each a distinct agent TYPE, blind to the others.
const LENSES = [
  { key: 'feature-reach-py', type: 'ecc:python-reviewer', focus:
    'THE CRUX. Whole vestigial Python FEATURE modules: import cleanly but never run on the live compress()/mcp route. ' +
    'Prime suspects from deptry optional-deps (trace each to a live caller or declare it dead): ' +
    'transforms/kompress_compressor.py (torch/safetensors learned compressor), transforms/code_compressor.py ' +
    '(tree_sitter), component_tracker.py (psutil — v1 flagged "appears unreferenced"). ALSO sweep transforms/ for ' +
    'every *_compressor.py and ask: does ContentRouter actually ROUTE to it under any normal policy, or is it an ' +
    'unrouted alternative? Check observability.py, integrations/, models/, any registry of compressors. ' +
    'For each: trace the 4 vectors, give a LIVE/TEST-ONLY/VESTIGIAL verdict + the est LOC of the whole module if dead.' },
  { key: 'rust-reach', type: 'ecc:rust-reviewer', focus:
    'Rust reachability-only dead set (cargo is 0-warnings, so this is NOT about compiler warnings). Find pub modules / ' +
    'pub fns / enum variants / pyo3-exported functions in crates/ that are reachable from no LIVE caller — only from ' +
    'their own #[cfg(test)] tests, or from a policy arm never taken on the live compress() path. Trace each candidate ' +
    'from the pyo3 bridge (crates/headroom-py/src/lib.rs) to a real Python caller. Check transforms/*_compressor.rs and ' +
    'any RoutingPolicy/mode variants for a dead arm. Light bar — report only genuinely runtime-unreachable, with the trace.' },
  { key: 'api-surface', type: 'ecc:type-design-analyzer', focus:
    'Public-API deprecation tail. Quantify entries in headroom/__init__.__all__ + _LAZY_EXPORTS (+ cache/ccr/transforms ' +
    '__init__) that have ~0 runtime instantiation anywhere outside their defining module + tests — internals leaked as ' +
    'maintained public surface. Known: relevance EmbeddingScorer/HybridScorer/create_scorer, cache optimizer registry. ' +
    'FIND MORE. For each: total LOC behind the export, and the exact deprecation untangle (drop __all__/_LAZY_EXPORTS + ' +
    'docs). Tag=surface (these are NOT pure-move cuts). Also: single-implementation Protocol/ABC, factory-of-one, ' +
    'delegating wrappers inside the LIVE keep-set (tag=yagni, real inline-able LOC).' },
  { key: 'dead-config', type: 'config-auditor', focus:
    'Dead configuration on the live path: HeadroomConfig / sub-config fields declared but never READ (grep `.<field>` ' +
    'across headroom/ excl proxy/archive), env vars nobody checks, RoutingPolicy/mode enums with an arm never selected, ' +
    'feature flags pinned to one value. Confirm each is unread across the whole live tree before flagging. ' +
    'Tag=deadflag. Name the field + proof of zero live read.' },
  { key: 'dup-shrink', type: 'Explore', focus:
    'Ladder rungs 2-3 + duplication + dead tests + archaeology (NOT style). (1) STDLIB (rung 2): hand-rolled logic the ' +
    'Python/Rust stdlib already ships — manual dict/zip building, hand-rolled itertools/functools/collections, custom ' +
    'base64/hex/json/hashlib reimplementations, manual context managers. Name the stdlib function that replaces it. ' +
    '(2) NATIVE (rung 3): a dep or hand-rolled block doing what an existing engine primitive already does (e.g. a 2nd ' +
    'hashing path beside the canonical compute_item_hash, a reimplemented tokenizer count beside Tokenizer). (3) DUP: ' +
    'near-identical functions / parallel impls — known leads: verify/heldout/measure.py vs verify/measure.py, ' +
    'benchmarks/metrics.py overlap, provider optimizers sharing shape. Name the canonical copy + dup LOC. (4) DEAD ' +
    'TESTS: tests/ files exercising an ALREADY-CUT feature (live_zone/pipeline/batch_processor/safety) — `rg` imports of ' +
    'archived modules in tests/. (5) ARCHAEOLOGY: commented-out blocks (ruff ERA001 — 6/7 prior were false-positive ' +
    'comment-headers, verify), TODO/FIXME graveyards, _old/_legacy/.orig files, generated artifacts checked in.' },
  { key: 'cross-lang-dup', type: 'code-architect', focus:
    'Cross-language + whole-subsystem redundancy the single-language lenses miss. Is any logic implemented in BOTH Python ' +
    'and Rust where only one is live (e.g. a Python compressor shadowed by a Rust pyclass, or vice-versa)? Is there a ' +
    'whole subsystem kept "just in case" (e.g. an alternate cache backend, an unused storage adapter, a second tokenizer ' +
    'path) with no live entry? Check headroom/models/, headroom/cache/ backends, crates offloads/ remnants. Name the live ' +
    'side to keep and the redundant side to cut, with the reachability trace.' },
]

const seenKey = (f) => `${(f.paths || '').toLowerCase()}|${(f.title || '').slice(0, 36).toLowerCase()}`
const lensPrompt = (l, round, seenList) =>
  `${READONLY}\n\n${LADDER}\n\n${TAGS}\n\n${REACH}\n\n${PRIOR}\n\nYOUR LENS (${l.key}): ${l.focus}\n\n` +
  (round > 1
    ? `This is sweep round ${round}. Already-found (DO NOT repeat — find only what these MISSED):\n` +
      seenList.slice(0, 80).join('\n') + '\n\nReport ONLY genuinely-new fat your lens uniquely sees.'
    : `Read the ground-truth file, trace reachability from the live entries, read the actual code. Report every real finding your lens sees.`) +
  `\nest_loc_cut = lines actually removable. evidence = your 4-vector trace proving no live caller (or the tool line). ` +
  `Set rung = the ladder rung that holds (1 unreached/delete … 6 minimum-code); tag accordingly. ` +
  `Be a lazy senior dev: stop at the first rung that holds, whole-feature and real removable fat only, NO style nitpicks, ` +
  `NO confident-but-wrong "safe to delete". When unsure, mark reachability honestly and safe_to_cut_now=false.`

phase('Sweep')
const seen = new Set()
const all = []
const notRun = new Set()
let round = 0
let dry = 0
const MAX_ROUNDS = 2
while (round < MAX_ROUNDS && dry < 1 && (!budget.total || budget.remaining() > 120_000)) {
  round++
  const roundResults = await parallel(
    LENSES.map(l => () =>
      agent(lensPrompt(l, round, [...seen]), { label: `R${round}:${l.key}`, phase: `Sweep R${round}`, schema: FINDINGS, agentType: l.type })
    )
  )
  // LOUD: a null slot = agentType didn't resolve / agent died — NOT "lens found nothing clean".
  const deadLenses = LENSES.filter((l, i) => !roundResults[i]).map(l => l.key)
  if (deadLenses.length) {
    deadLenses.forEach(k => notRun.add(k))
    log(`⚠ round ${round}: lenses returned NULL (NOT RUN, not "clean"): ${deadLenses.join(', ')}`)
  }
  const roundFindings = roundResults.filter(Boolean).flatMap(r => (r && r.findings) || [])
  const fresh = roundFindings.filter(f => f && f.paths && !seen.has(seenKey(f)))
  fresh.forEach(f => seen.add(seenKey(f)))
  all.push(...fresh)
  log(`Sweep round ${round}: ${fresh.length} new findings (${all.length} total)`)
  if (fresh.length < 3) dry++
}

phase('Verify')
// perspective-diverse refutation of every vestigial/delete/dep/deadflag claim marked safe-to-cut-now
const toVerify = all.filter(f => ['delete', 'dup', 'dep', 'deadflag'].includes(f.tag) && f.safe_to_cut_now === true)
const PERSPECTIVES = [
  'REACHABILITY refuter: find ANY live caller (all 4 vectors, incl dynamic/lazy/_LAZY_EXPORTS/pyo3) that proves it IS reached on a normal compress()/mcp path. A test-only caller does NOT count as live.',
  'BEHAVIOR refuter: would removing this silently change compression output, drop a side effect (telemetry/feedback/eviction), or break an invariant no test covers?',
  'TOOL/TRACE-FALSE-POSITIVE refuter: is the no-caller trace misleading (dynamic dispatch by string name, registry auto-registration, a calibration knob, a parity mirror, an entry_point)?',
]
const verified = await parallel(
  toVerify.map(f => () =>
    parallel(PERSPECTIVES.map((p, i) => () =>
      agent(
        `${READONLY}\n\nYou are verifier #${i + 1}. ${p}\n\nCLAIM (${f.lens}/${f.tag}): ${f.title}\nPaths: ${f.paths}\n` +
        `Reachability: ${f.reachability}\nest_loc_cut: ${f.est_loc_cut}\nEvidence: ${f.evidence || 'n/a'}\n` +
        `Untangle: ${f.untangle_needed || '(none)'}\n\nDefault to uncertain over a confident wrong verdict. ` +
        `Find a real blocker -> refuted. Conclusively safe by your lens -> confirmed.`,
        { label: `verify:${f.lens}#${i + 1}`, phase: 'Verify', schema: VERDICT, agentType: 'Explore' }
      ).then(v => v && v.verdict)
    )).then(vs => {
      const v = vs.filter(Boolean)
      const refuted = v.filter(x => x === 'refuted').length
      const confirmed = v.filter(x => x === 'confirmed').length
      // Drop ONLY majority-refuted. 0-refute-but-not-2-confirm = uncertain -> KEEP, flag needs-review.
      return { ...f, panel: v, survives: refuted < 2, needsReview: !(refuted === 0 && confirmed >= 2) }
    })
  )
)
const verifiedMap = new Map(verified.filter(Boolean).map(f => [seenKey(f), f]))
const allWithVerdicts = all.map(f => verifiedMap.get(seenKey(f)) || { ...f, panel: ['not_verified'], survives: null, needsReview: false })
const survivors = allWithVerdicts.filter(f => f.survives !== false)
const droppedN = allWithVerdicts.filter(f => f.survives === false).length
const uncertainN = survivors.filter(f => f.needsReview).length
log(`Verify: ${toVerify.length} claims panel-checked; ${droppedN} majority-refuted (dropped); ${uncertainN} uncertain (KEPT as needs-review).`)

phase('Critic')
const critic = await agent(
  `${READONLY}\n\nCOMPLETENESS CRITIC. Below are ${survivors.length} fat-findings (JSON) from a 6-lens reachability sweep over ` +
  `the POST-CUT Headroom tree (tools are clean; this pass hunts vestigial features + surface). Your job: find what is STILL ` +
  `HIDDEN. Ask: which transforms/*_compressor or feature module did NO lens trace to a verdict? which directory ` +
  `(headroom/models, headroom/integrations, headroom/cache backends, crates offloads remnants) was not covered? is there a ` +
  `whole subsystem kept "just in case" with no live entry? what is the single biggest remaining LOC lever nobody flagged? ` +
  `Read the ground-truth (${GT}) + repo, then return findings (same schema) for genuinely-new fat only. If coverage is truly ` +
  `exhaustive, return an empty findings array (honest — do not invent).\n\nEXISTING:\n${JSON.stringify(survivors).slice(0, 12000)}`,
  { label: 'completeness-critic', phase: 'Critic', schema: FINDINGS, agentType: 'code-architect' }
)
const criticFresh = ((critic && critic.findings) || []).filter(f => f && f.paths && !seen.has(seenKey(f)))
criticFresh.forEach(f => survivors.push({ ...f, panel: ['critic'], survives: null }))
log(`Critic: +${criticFresh.length} findings the lenses missed.`)

phase('Synthesize')
const synthPrompt =
  `${READONLY}\n\nYou are a lazy senior dev writing the 4th-pass simplification audit. Below are ${survivors.length} ` +
  `reachability-traced findings (JSON) against the POST-CUT tree (44,217 LOC; Tier-1+2 already removed ~9.6k). ` +
  `Write lazy-dev-AUDIT-v4.md as MARKDOWN:\n` +
  `- OPEN with the honest headline: the static tools came back CLEAN (cargo 0-warnings, vulture 2, ruff 34 nits) — so this ` +
  `pass hunted reachability, not tool hits. State up front how much GENUINELY-removable fat was found (could be small — that ` +
  `is a valid, honest result for a 4th pass; do not inflate).\n` +
  `- DROP panel-refuted findings; keep a short "Refuted" appendix (what looked dead but is live, and why — this is the ` +
  `pass earning its keep).\n- Dedup across lenses (same path+idea = one row, cite all lenses).\n` +
  `- RANK by the LAZY-DEV LADDER then LOC: within each tier, lower rung first (rung 1 unreached-delete > rung 2 stdlib > ` +
  `rung 3 native > rung 4 dep > yagni/shrink), ties broken by est_loc_cut. Show the rung + tag per row. The ladder is the ` +
  `point: a whole unreached feature beats a hand-rolled-stdlib shrink beats a one-line tidy.\n` +
  `  TIER 1 SAFE NOW (whole vestigial module / dead code, verified no live caller, minimal untangle) — table ` +
  `[rank|what|paths|~LOC|rung|tag|reachability|evidence].\n` +
  `  TIER 2 CUT AFTER UNTANGLE (vestigial but 1 co-requisite edge) — table + exact untangle per row.\n` +
  `  TIER 3 SURFACE DEPRECATION (public-API exports with ~0 runtime use — drop __all__/_LAZY_EXPORTS+docs+version bump, ` +
  `NOT pure-move) — table with LOC behind each.\n` +
  `  TIER 0 ARCHAEOLOGY (dup, dead tests for cut features, commented-out, doc cruft, stray artifacts) — table.\n` +
  `- Mark each uncertain (needsReview) finding "(needs review)".\n` +
  `- ★ Every stdlib/native row bypassed the verify panel — label each one "unverified replacement — confirm edge-case ` +
  `parity at apply time" so it does not read as pre-blessed. Lazy means less code, NOT the flimsier algorithm.\n` +
  `- COMPLEXITY-LINT QUARANTINE: any pure-lint churn goes in a separate "OPTIONAL — high-churn, NOT recommended" bucket, ` +
  `never a cut tier.\n` +
  `- Note any lenses that reported NOT-RUN (coverage gaps, NOT zero-fat): ${JSON.stringify([...notRun])}.\n` +
  `- State what v4 found that the first 3 passes missed, and be honest if the tree is now essentially lean.\n` +
  `- End: one line that this is REPORT-ONLY; applying is a separate gated archive+test step; proxy/ is excluded (condemned in step 3).\n\nFINDINGS JSON:\n${JSON.stringify(survivors)}`
const report = await agent(synthPrompt, { label: 'synthesize', phase: 'Synthesize', agentType: 'code-architect' })

return {
  branch: 'verify/phase2-audit-report',
  head: 'de3fd231',
  lenses: LENSES.length,
  rounds: round,
  totalFindings: all.length,
  refuted: allWithVerdicts.filter(f => f.survives === false).length,
  criticAdded: criticFresh.length,
  survivors: survivors.length,
  notRun: [...notRun],
  report,
  findings: survivors,
}
