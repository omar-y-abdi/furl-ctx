// ---------------------------------------------------------------------------
// simplify-audit-v2 — exhaustive, tool-grounded, multi-lens fat-finder (Headroom)
//
// REPORT-ONLY. All agents are read-only specialized review types (no Edit/Write).
// Grounded in real analyzer output (vulture/ruff/deptry/cargo/git) captured to
// args.groundTruth. 10 distinct lenses (each a different agent TYPE, blind to the
// others) → loop-until-dry → perspective-diverse adversarial verify → completeness
// critic → synth. Synth RETURNS markdown; the orchestrator writes the file.
//
// RUN:  Workflow({ scriptPath:'<this>', args:{ groundTruth:'<abs path>' } })
// ---------------------------------------------------------------------------

export const meta = {
  name: 'simplify-audit-v2',
  description: 'Exhaustive tool-grounded fat-finder for Headroom: 10 read-only lenses (dead-code, dup, dep-bloat, abstraction, dead-config, api-surface, test-bloat, archaeology, doc-cruft, rust) over real analyzer output, loop-until-dry, perspective-diverse verify, completeness critic. Report-only ranked cut-list.',
  whenToUse: 'Deep re-audit of Headroom for every kind of removable fat after a first pass already took the obvious cuts. Finds line-level dead code, duplication, unused deps, dead flags, over-abstraction, archaeology — grounded in tool output, reachability-verified.',
  phases: [
    { title: 'Sweep', detail: '10 lenses × loop-until-dry' },
    { title: 'Verify', detail: 'perspective-diverse refutation of delete-claims' },
    { title: 'Critic', detail: 'completeness — what fat is still hidden' },
    { title: 'Synthesize', detail: 'dedup + rank biggest-safe-cut-first' },
  ],
}

const GT = (args && args.groundTruth) || '/Users/k/dev/headroom/.claude/runtime/fat-groundtruth.md'

const READONLY =
  'READ-ONLY auditor. Use ONLY Read/Grep/Glob and read-only shell (rg, grep, wc, find, ' +
  'sed -n, cat, git log/blame, .venv/bin/vulture, .venv/bin/ruff, .venv/bin/deptry, ' +
  '`cargo check`). NEVER Edit/Write/NotebookEdit; NEVER git mutate (checkout/add/commit/ ' +
  'reset/switch/branch/stash/worktree-remove); NEVER pip-install or maturin/cargo build. ' +
  'Exclude target/, .venv, .venv-eval, .sccache/, .claude/worktrees/, *.so, node_modules/, ' +
  'archive/, .git/. Repo root: /Users/k/dev/headroom (branch verify/phase2-audit-report). ' +
  `Tool ground-truth already captured at ${GT} — Read it first; re-run scoped tools to confirm/extend. ` +
  'Your final output is DATA for a synthesizer, not a human message.'

const REACH =
  'For any module/file/symbol DELETION claim, classify reachability from the public API ' +
  'compress() (entry headroom/compress.py -> headroom/pipeline.py loads transforms via ' +
  'entry_points) by checking ALL FOUR coupling vectors: (1) static import; (2) lazy/deferred ' +
  'import inside functions; (3) dynamic import (importlib/__import__/string names) + ' +
  'headroom/__init__ + cache/__init__ _LAZY_EXPORTS re-export tables; (4) pyproject ' +
  'entry_points/scripts/deps. LIVE=exercised by a normal compress(); VESTIGIAL=import only ' +
  'in a dead/guarded branch; DEAD=no referrer. Name the exact untangle for vestigial cuts. ' +
  'DO NOT flag the hard invariants (CCR recovery, prompt-cache ordering, Py<->Rust hash parity) ' +
  'or already-known-LIVE modules (telemetry/, onnx_runtime.py, wiki/). A tool flag is a LEAD, ' +
  'not proof — a vulture/ruff hit on an exported symbol can be a false positive; verify reachability.'

const PRIOR =
  'CONTEXT — a first pass (v1) already ARCHIVED (do not re-report as new): REALIGNMENT/, sql/, ' +
  'docker, claude_analysis_ttl.py, marketing md, cache/semantic.py, cache/prefix_tracker.py, ' +
  'shared_context.py, proxy/interceptors/, binaries.py, _OPTIONAL_EXPORTS, create_pipeline. ' +
  'v1 DEFERRED as entangled (re-audit for the BEST untangle, this is high-value): cache-optimizer ' +
  'cluster (anthropic/openai/google/registry/dynamic_detector/compression_feedback ~3.8k; ' +
  'registry<-tokenizers, compression_feedback<-compression_store:1089 lazy), relevance/ ' +
  '(compression_store:48 unconditional BM25), models/ml_models.py, proxy/helpers.py (SSE live), ' +
  'ccr/{batch_processor,mcp_server}.py (mcp_server KEEP — needed for the planned MCP retrieve plane). ' +
  'Find what v1 MISSED: line-level dead code, duplication, unused deps, dead flags, over-abstraction, ' +
  'complexity fat, archaeology (the 19GB dead .claude/worktrees/).'

const TAGS =
  'TAGS: delete=dead/vestigial code; dup=duplicated/clone logic (name the canonical copy to keep); ' +
  'dep=unused/replaceable dependency (name it); stdlib=hand-rolled what stdlib ships; ' +
  'yagni=single-impl abstraction / factory-of-one / delegating wrapper / speculative param / ' +
  'boolean-blindness / config nobody sets; deadflag=config field/env/feature-flag never read; ' +
  'shrink=same behavior fewer lines / complexity reduction; archaeology=stale orphan / commented-out / ' +
  'marker graveyard / disk cruft.'

const FINDINGS = {
  type: 'object',
  properties: { findings: { type: 'array', items: {
    type: 'object',
    properties: {
      lens: { type: 'string' },
      tag: { type: 'string', enum: ['delete', 'dup', 'dep', 'stdlib', 'yagni', 'deadflag', 'shrink', 'archaeology'] },
      title: { type: 'string' },
      paths: { type: 'string' },
      est_loc_cut: { type: 'number' },
      replacement: { type: 'string' },
      reachability: { type: 'string', enum: ['live', 'vestigial', 'dead', 'na'] },
      safe_to_cut_now: { type: 'boolean' },
      untangle_needed: { type: 'string' },
      tool_evidence: { type: 'string', description: 'the vulture/ruff/deptry/cargo/grep line or grep proof, or "manual"' },
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

// 10 lenses, each a distinct agent TYPE, blind to the others (multi-modal sweep)
const LENSES = [
  { key: 'deadcode-py', type: 'ecc:python-reviewer', focus:
    'LINE-LEVEL dead Python: unused funcs/methods/classes/vars, unreachable branches, dead returns. ' +
    'Re-run `.venv/bin/vulture headroom --min-confidence 60 --exclude "archive/"` and ' +
    '`.venv/bin/ruff check headroom --select F401,F811,F841,ERA001,RUF100,F501,PLR1711 --output-format concise`. ' +
    'Triage each hit by reachability (exported != used). Report the real dead ones with the tool line as evidence.' },
  { key: 'deadcode-rust', type: 'ecc:rust-reviewer', focus:
    'Dead Rust: run `cargo check -p headroom-core 2>&1 | grep -iE "never (used|read|constructed)|warning: unused"`; ' +
    'find `#[allow(dead_code)]`/`#[allow(unused)]` masking real dead code (e.g. live_zone.rs:1299); pub items/fns/ ' +
    'enum variants with zero referrers; dead legacy paths (e.g. RoutingPolicy::LosslessFirst if unused). Light bar — ' +
    'the Rust core is hardened; report only genuinely-dead, not style.' },
  { key: 'duplication', type: 'Explore', focus:
    'Duplication / clones: near-identical functions, copy-pasted blocks, PARALLEL implementations of one idea. ' +
    'Known: verify/heldout/measure.py == verify/measure.py (dup), benchmarks/metrics.py overlaps verify/measure.py, ' +
    'the 4 cache provider optimizers (anthropic/openai/google) share shape. Use `rg` to find repeated signatures/ ' +
    'docstrings/constants. For each: name the canonical copy to keep and the dup LOC to cut.' },
  { key: 'dep-bloat', type: 'code-architect', focus:
    'Dependency bloat: re-run `.venv/bin/deptry headroom --known-first-party headroom` (ignore the polluted worktree ' +
    'lines in ground-truth). Find pyproject deps/extras imported nowhere in the live keep-set, heavy deps used for ' +
    'one trivial call (replaceable by stdlib), and extras for amputated features. Also scan crates/*/Cargo.toml for ' +
    'deps not referenced in src/. Name each dep + where (or that) it is used.' },
  { key: 'abstraction-bloat', type: 'ecc:type-design-analyzer', focus:
    'Over-abstraction: Protocol/ABC/interface with ONE implementation, factory/registry for one product, classes that ' +
    'only wrap+delegate, speculative kwargs/config never passed non-default, boolean-blindness params, dataclasses that ' +
    'duplicate another. The CompressionStoreBackend Protocol (one impl) is a known lead. For each, give the leaner inline form.' },
  { key: 'dead-config', type: 'config-auditor', focus:
    'Dead configuration: HeadroomConfig / sub-config fields declared but never READ (grep `.<field>`), env vars nobody ' +
    'checks, feature flags always-one-value, RoutingPolicy/mode enums with a dead arm. Known leads: config.py default_mode, ' +
    'prefix_freeze, cache_optimizer. Confirm each is unread across headroom/ (not just config.py) before flagging.' },
  { key: 'api-surface', type: 'ecc:python-reviewer', focus:
    'Public-API over-exposure: entries in headroom/__init__.__all__ + _LAZY_EXPORTS (and cache/ccr/transforms __init__) ' +
    'that NO code (incl tests) outside the defining module consumes — the library exporting internals as public surface ' +
    'it must then maintain. For each, is it a documented API or just leaked internals? Removable = breaking-but-cheap.' },
  { key: 'test-bloat', type: 'ecc:pr-test-analyzer', focus:
    'Test bloat: redundant/overlapping test files, tests for ARCHIVED/dead features, duplicated fixtures/helpers, ' +
    'over-mocked tests that assert nothing real, parametrize blocks that repeat. Keep every test guarding a live invariant ' +
    '(CCR recovery, parity, cache ordering). Run `.venv/bin/ruff check tests --select F401,F811` for dead test imports.' },
  { key: 'archaeology', type: 'Explore', focus:
    'Repo archaeology: the 19GB dead `.claude/worktrees/wf_ad2e78a5-*` (8 worktrees, `git worktree prune`+rm). Stale orphan ' +
    'files (git log oldest, untouched + unreferenced). Marker graveyards: `rg -n -i "TODO|FIXME|XXX|HACK" headroom crates -g"!archive/**"`. ' +
    'Commented-out code (ruff ERA001 hits). Backup/_old/_legacy/_v2/.orig files. Generated artifacts checked in (gifs/pngs, *.so, uv.lock vs poetry).' },
  { key: 'doc-cruft', type: 'ecc:comment-analyzer', focus:
    'Doc/comment cruft: commented-out code blocks (the 7 ERA001 sites), stale docstrings describing removed proxy/CLI ' +
    'features, duplicate/contradictory docs, README/llms.txt claims that no longer match (60-95% headline vs ~30%), ' +
    'wiki pages documenting amputated features (proxy.md, cli.md, mcp.md). Report cuttable doc LOC + which to keep.' },
]

const seenKey = (f) => `${(f.paths || '').toLowerCase()}|${(f.title || '').slice(0, 36).toLowerCase()}`
const lensPrompt = (l, round, seenList) =>
  `${READONLY}\n\n${TAGS}\n\n${REACH}\n\n${PRIOR}\n\nYOUR LENS (${l.key}): ${l.focus}\n\n` +
  (round > 1
    ? `This is sweep round ${round}. Already-found (DO NOT repeat — find only what these MISSED):\n` +
      seenList.slice(0, 90).join('\n') + '\n\nReport ONLY genuinely-new fat your lens uniquely sees.'
    : `Read the ground-truth file, run your scoped tools, read the actual code. Report every real finding your lens sees.`) +
  `\nest_loc_cut = lines actually removed. tool_evidence = the exact tool/grep line proving it (or "manual"). ` +
  `Be a lazy senior dev: real removable fat only, no style nitpicks, no false "safe to delete".`

phase('Sweep')
const seen = new Set()
const all = []
const notRun = new Set()
let round = 0
let dry = 0
const MAX_ROUNDS = 3
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
  if (fresh.length < 4) dry++
}

phase('Verify')
// perspective-diverse refutation of every delete/dup/dep claim marked safe-to-cut-now
const toVerify = all.filter(f => ['delete', 'dup', 'dep', 'deadflag'].includes(f.tag) && f.safe_to_cut_now === true)
const PERSPECTIVES = [
  'REACHABILITY refuter: find ANY live import/usage (all 4 vectors) that proves it is still reached by compress().',
  'BEHAVIOR refuter: would removing this silently change output, drop a side effect, or break an invariant no test covers?',
  'TOOL-FALSE-POSITIVE refuter: is the tool/grep evidence misleading (exported symbol, dynamic use, parity mirror, calibration knob)?',
]
const verified = await parallel(
  toVerify.map(f => () =>
    parallel(PERSPECTIVES.map((p, i) => () =>
      agent(
        `${READONLY}\n\nYou are verifier #${i + 1}. ${p}\n\nCLAIM (${f.lens}/${f.tag}): ${f.title}\nPaths: ${f.paths}\n` +
        `Reachability: ${f.reachability}\nest_loc_cut: ${f.est_loc_cut}\nEvidence: ${f.tool_evidence || 'n/a'}\n` +
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
  `${READONLY}\n\nCOMPLETENESS CRITIC. Below are ${survivors.length} fat-findings (JSON) from a 10-lens sweep over Headroom. ` +
  `Your job: find what is STILL HIDDEN. Ask: which directory/file was NOT covered? which modality (dup across Py<->Rust, ` +
  `dead Cargo features, generated-checked-in files, whole-subsystem redundancy) did no lens run? what is the single biggest ` +
  `remaining lever nobody flagged? Read the ground-truth (${GT}) + repo, then return findings (same schema) for genuinely-new ` +
  `fat only. If coverage is truly exhaustive, return an empty findings array.\n\nEXISTING:\n${JSON.stringify(survivors).slice(0, 12000)}`,
  { label: 'completeness-critic', phase: 'Critic', schema: FINDINGS, agentType: 'code-architect' }
)
const criticFresh = ((critic && critic.findings) || []).filter(f => f && f.paths && !seen.has(seenKey(f)))
criticFresh.forEach(f => survivors.push({ ...f, panel: ['critic'], survives: null }))
log(`Critic: +${criticFresh.length} findings the lenses missed.`)

phase('Synthesize')
const synthPrompt =
  `${READONLY}\n\nYou are a lazy senior dev writing the EXHAUSTIVE simplification audit (v2). Below are ${survivors.length} ` +
  `tool-grounded, reachability-checked findings (JSON). Write lazy-dev-AUDIT-v2.md as MARKDOWN:\n` +
  `- DROP findings whose panel refuted them; keep a short "Refuted" appendix.\n- Dedup across lenses (same path+idea = one row, cite all lenses that caught it).\n` +
  `- RANK biggest-cut-first by est_loc_cut, grouped in tiers:\n  TIER 1 SAFE NOW (dead/vestigial verified, no untangle) — table [rank|what|paths|~LOC|tag|lens|evidence].\n` +
  `  TIER 2 CUT AFTER UNTANGLE — table + exact untangle step per row.\n  TIER 3 SHRINK (yagni/dup/stdlib/delegating-wrapper inside live code — real LOC removal, low risk) — table.\n  TIER 0 NON-CODE DISK/ARCHAEOLOGY (worktrees, markers, commented-out, doc cruft) — table.\n` +
  `- COMPLEXITY-LINT QUARANTINE: pure lint churn (PLR2004 magic-values, C901, PLR0912/0913/0915 too-many-*, RUF012, SIM*) ` +
  `goes in a SEPARATE labeled "OPTIONAL — high-churn, regression-risk on CCR/parity/cache invariants, NOT recommended" bucket. ` +
  `NEVER a cut tier — churning 154 magic-values across a hardened engine is the opposite of lazy-senior.\n` +
  `- Mark each uncertain (needsReview) finding "(needs review)"; do not present it as verified-safe.\n` +
  `- Top: 5-line exec summary — total ~LOC cuttable now vs after-untangle, the 19GB worktree note, the single biggest lever, ` +
  `and any lenses reported NOT-RUN (coverage gaps, NOT zero-fat): ${JSON.stringify([...notRun])}.\n` +
  `- Note what v2 found that v1 MISSED. Be honest if the amputated bloat is still load-bearing.\n` +
  `- End: one line that this is REPORT-ONLY and applying is a separate gated archive+test step.\n\nFINDINGS JSON:\n${JSON.stringify(survivors)}`
const report = await agent(synthPrompt, { label: 'synthesize', phase: 'Synthesize', agentType: 'code-architect' })

return {
  branch: 'verify/phase2-audit-report',
  lenses: LENSES.length,
  rounds: round,
  totalFindings: all.length,
  refuted: allWithVerdicts.filter(f => f.survives === false).length,
  criticAdded: criticFresh.length,
  survivors: survivors.length,
  report,
  findings: survivors,
}
