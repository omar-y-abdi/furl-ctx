// ---------------------------------------------------------------------------
// rust-fat-audit — focused, HARD pass on crates/ (the part v2 audits "light")
//
// REPORT-ONLY. Read-only ecc:rust-reviewer / code-architect agents. The core
// question: with the proxy/live_zone path archived, which Rust is reachable ONLY
// via dead entry points? Grounded in .claude/runtime/rust-fat-groundtruth.md
// (pyo3 surface + live-caller map). 4 lenses -> adversarial verify (invariant-
// aware) -> synth. Synth RETURNS markdown; orchestrator writes + consolidates
// with the v2 report into one final doc.
//
// RUN AFTER v2 completes (avoids cargo target-lock contention).
// ---------------------------------------------------------------------------

export const meta = {
  name: 'rust-fat-audit',
  description: 'Hard focused dead-Rust audit of crates/: trace pyo3 reachability from the LIVE compress() path, find what only the archived proxy/live_zone path reaches, dead pub items (cargo dead_code), cross-language compressor redundancy, unused deps. Invariant-aware adversarial verify. Report-only.',
  whenToUse: 'Deep dead-Rust pass after the proxy amputation: which crates/ modules are reachable only via the dead live_zone/proxy path. Complements the broad v2 audit which treats Rust lightly.',
  phases: [
    { title: 'Reachability', detail: '4 lenses: pyo3-reachability, dead-items, cross-lang-dup, deps' },
    { title: 'Verify', detail: 'invariant-aware refutation of dead-Rust claims' },
    { title: 'Synthesize', detail: 'ranked dead-Rust cut-list' },
  ],
}

const GT = (args && args.groundTruth) || '/Users/k/dev/headroom/.claude/runtime/rust-fat-groundtruth.md'

const READONLY =
  'READ-ONLY Rust auditor. Use ONLY Read/Grep/Glob + read-only shell (rg, grep, wc, ' +
  '`cargo check -p headroom-core`, `cargo tree`, `cargo +nightly udeps` if present). NEVER ' +
  'Edit/Write; NEVER git mutate; NEVER `cargo build --release`/maturin/edit Cargo.toml. ' +
  'Exclude target/, .venv*, archive/, .claude/worktrees/. Repo root /Users/k/dev/headroom. ' +
  `Rust ground-truth (pyo3 surface + live-caller map) at ${GT} — Read it first. Your final ` +
  'output is DATA for a synthesizer, not a human message.'

const CONTEXT =
  'CRITICAL CONTEXT (verify, do not just trust): the LIVE public path is compress() (Python) -> ' +
  'ContentRouter (Python) which calls GRANULAR Rust pyo3 helpers (detect_content_type, ' +
  'is_json_array_of_dicts, protect_tags/restore_tags, detect_log_format, parse_search_lines, ' +
  'score_line, content_has_error_indicators) + the PySmartCrusher pyclass (crush/crush_array_json). ' +
  'The whole-body Rust entry `compress_*_live_zone` has NO live Python caller (only proxy/helpers.py ' +
  'which is archived/deferred-dead + compression_policy.py which is proxy-coupled). So `live_zone.rs` ' +
  '(2899 LOC) and anything reachable ONLY from it — likely the Rust `pipeline/orchestrator.rs` + ' +
  '`pipeline/offloads/*` + the full `log_compressor.rs`/`diff_compressor.rs`/`search_compressor.rs` ' +
  'compressor structs (which the live Python path replaces with its OWN Python compressors, calling ' +
  'only the granular Rust helpers) — may be DEAD after the proxy amputation. PROVE it per module: ' +
  'enumerate the FULL pyo3 surface (BOTH #[pyfunction] AND #[pymethods]/#[pyclass] in headroom-py/' +
  'src/lib.rs), trace each to a live Python caller, then map every crates/ module as ' +
  'LIVE (reached from a live pyo3 entry) vs DEAD-PROXY-ONLY (reached only from live_zone/dead entries). ' +
  'A Rust module with its own #[cfg(test)] tests is still DEAD if no production path reaches it — but ' +
  'note which crates/headroom-core/tests/*.rs files exercise it (they must be archived together).'

const GUARD =
  'DO NOT propose cutting: the SmartCrusher subsystem (crusher.rs, compaction/, analyzer.rs, ' +
  'planning.rs, orchestration.rs, anchor_selector.rs), ccr/ (CCR recovery invariant), tokenizer/, ' +
  'cache_control.rs (prompt-cache ordering), the granular live pyo3 helpers, or auth_mode.rs/' +
  'compression_policy.rs (Py<->Rust parity invariant, Phase F). Feature-gated code (magika/' +
  'embeddings/redis) is INTENTIONAL conditional compilation — note it, do not cut. A `cargo` ' +
  'warning or grep hit is a LEAD, not proof — confirm reachability before calling anything dead.'

const FINDINGS = {
  type: 'object',
  properties: { findings: { type: 'array', items: {
    type: 'object',
    properties: {
      lens: { type: 'string' },
      tag: { type: 'string', enum: ['delete', 'dup', 'dep', 'shrink'] },
      title: { type: 'string' },
      paths: { type: 'string' },
      est_loc_cut: { type: 'number' },
      reachability: { type: 'string', enum: ['live', 'dead-proxy-only', 'dead', 'na'] },
      safe_to_cut_now: { type: 'boolean' },
      rust_tests_to_archive: { type: 'string', description: 'crates/.../tests/*.rs that cover it, or "none"' },
      untangle_needed: { type: 'string' },
      tool_evidence: { type: 'string' },
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

const LENSES = [
  { key: 'pyo3-reachability', type: 'ecc:rust-reviewer', focus:
    'THE core lens. Enumerate the COMPLETE pyo3 surface in crates/headroom-py/src/lib.rs (every ' +
    '#[pyfunction] AND every #[pymethods] on every #[pyclass]). For each, grep its live Python callers ' +
    '(exclude headroom/proxy/** and headroom/transforms/compression_policy.py — proxy-coupled/dead). ' +
    'Then for EACH crates/headroom-core module, decide: reached from a LIVE pyo3 entry, or only from ' +
    'live_zone.rs / the dead whole-body pipeline? Output every DEAD-PROXY-ONLY module with LOC + the ' +
    'rust tests that cover it. Prime suspects: live_zone.rs, pipeline/orchestrator.rs, pipeline/offloads/*, ' +
    'and the full log/diff/search_compressor.rs structs (vs the live granular helpers detect_log_format/parse_search_lines).' },
  { key: 'dead-items', type: 'ecc:rust-reviewer', focus:
    'Run `cargo check -p headroom-core 2>&1 | rg -i "never (used|read|constructed)|warning: unused|field is never"`. ' +
    'Find dead pub fns/structs/enum-variants/consts with zero referrers (grep each). Find `#[allow(dead_code)]`/' +
    '`#[allow(unused)]` masks (e.g. live_zone.rs:1299) and check what they hide. Dead legacy enum arms ' +
    '(e.g. RoutingPolicy::LosslessFirst if never constructed). Report with the cargo/grep line as evidence.' },
  { key: 'cross-lang-dup', type: 'code-architect', focus:
    'Cross-language redundancy: the repo has BOTH Rust compressors (log/diff/search_compressor.rs) and ' +
    'Python compressors (headroom/transforms/{log,diff,search}_compressor.py). Determine which set the live ' +
    'compress() path actually uses (the Python ContentRouter lazy-loads the Python ones). If the Rust ' +
    'compressor structs are reached only via the dead live_zone, they are redundant dead duplicates of the ' +
    'live Python ones. Name the canonical (live) copy and the dead duplicate LOC. Also check formatter.rs/' +
    'compactor.rs are NOT dup (they ARE the live SmartCrusher).' },
  { key: 'deps-features', type: 'code-architect', focus:
    'Cargo dependency bloat: for each dep in crates/headroom-core/Cargo.toml + headroom-py/Cargo.toml, grep ' +
    'src/ for actual use; flag deps referenced nowhere (cargo-machete style). Note feature-gated deps ' +
    '(magika/embeddings/redis) are intentional optional — do NOT flag as cut, just confirm they compile to ' +
    'zero in the shipped wheel. Flag any dep pulled in ONLY by dead-proxy modules (cuttable once those go).' },
]

const lensPrompt = (l) =>
  `${READONLY}\n\n${CONTEXT}\n\n${GUARD}\n\nYOUR LENS (${l.key}): ${l.focus}\n\n` +
  `Read the ground-truth, read the actual Rust, run your scoped tool. est_loc_cut = lines removed. ` +
  `tool_evidence = the cargo/grep proof. reachability MUST reflect a real pyo3-to-Python trace. ` +
  `Lazy senior dev: real dead Rust only, no style nitpicks on the hardened core.`

phase('Reachability')
const swept = await parallel(
  LENSES.map(l => () => agent(lensPrompt(l), { label: `lens:${l.key}`, phase: 'Reachability', schema: FINDINGS, agentType: l.type }))
)
const deadLenses = LENSES.filter((l, i) => !swept[i]).map(l => l.key)
if (deadLenses.length) log(`⚠ lenses returned NULL (NOT RUN, not "clean"): ${deadLenses.join(', ')}`)
const all = swept.filter(Boolean).flatMap(r => (r && r.findings) || [])
log(`Reachability: ${all.length} Rust findings across ${LENSES.length - deadLenses.length}/${LENSES.length} lenses.`)

phase('Verify')
const PERSPECTIVES = [
  'REACHABILITY refuter: find ANY live pyo3 entry or Rust production path (not #[cfg(test)]) that reaches this module.',
  'INVARIANT refuter: does this module carry or test a hard invariant (CCR recovery, Py<->Rust parity, cache ordering) such that removing it weakens a guarantee?',
  'TEST-IMPACT refuter: which cargo tests cover it — would archiving it red the Rust suite in a way that signals it is actually live?',
]
const toVerify = all.filter(f => f.tag === 'delete' && (f.reachability === 'dead-proxy-only' || f.reachability === 'dead'))
const verified = await parallel(
  toVerify.map(f => () =>
    parallel(PERSPECTIVES.map((p, i) => () =>
      agent(
        `${READONLY}\n\n${GUARD}\n\nYou are verifier #${i + 1}. ${p}\n\nCLAIM (${f.lens}): ${f.title}\nPaths: ${f.paths}\n` +
        `Reachability: ${f.reachability}\nest_loc_cut: ${f.est_loc_cut}\nTests: ${f.rust_tests_to_archive || '?'}\n` +
        `Evidence: ${f.tool_evidence || 'n/a'}\n\nDefault to uncertain over a confident wrong verdict.`,
        { label: `verify:${f.lens}#${i + 1}`, phase: 'Verify', schema: VERDICT, agentType: 'ecc:rust-reviewer' }
      ).then(v => v && v.verdict)
    )).then(vs => {
      const v = vs.filter(Boolean)
      const refuted = v.filter(x => x === 'refuted').length
      const confirmed = v.filter(x => x === 'confirmed').length
      return { ...f, panel: v, survives: refuted < 2, needsReview: !(refuted === 0 && confirmed >= 2) }
    })
  )
)
const vmap = new Map(verified.filter(Boolean).map(f => [`${f.paths}|${f.title}`, f]))
const merged = all.map(f => vmap.get(`${f.paths}|${f.title}`) || { ...f, panel: ['not_verified'], survives: true, needsReview: false })
const survivors = merged.filter(f => f.survives !== false)
log(`Verify: ${toVerify.length} dead-claims panel-checked; ${merged.filter(f => f.survives === false).length} refuted; ${survivors.filter(f => f.needsReview).length} uncertain (kept).`)

phase('Synthesize')
const report = await agent(
  `${READONLY}\n\nWrite the dead-Rust section as MARKDOWN. ${survivors.length} reachability-verified findings (JSON) below. ` +
  `Rank biggest-cut-first by est_loc_cut. Group: TIER 1 SAFE (dead-proxy-only, panel-confirmed, list the rust tests to archive ` +
  `with each) / TIER 2 NEEDS-REVIEW (uncertain) / NON-CUTS (live, with why) / DEPS. Top: total dead-Rust LOC + the single biggest ` +
  `module + which rust tests must move with it. Mark needsReview rows "(needs review)". Honest: if live_zone's subtree turns out ` +
  `partially live, say so. This is REPORT-ONLY.\n\nFINDINGS JSON:\n${JSON.stringify(survivors)}`,
  { label: 'synthesize', phase: 'Synthesize', agentType: 'code-architect' }
)

return { branch: 'verify/phase2-audit-report', lenses: LENSES.length, findings: survivors.length, refuted: merged.filter(f => f.survives === false).length, report, items: survivors }
