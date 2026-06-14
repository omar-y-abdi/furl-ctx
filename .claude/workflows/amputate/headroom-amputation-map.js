// ---------------------------------------------------------------------------
// headroom-amputation-map  —  workflow-architect
//
// Map a forked repo's modules to a KEEP-set: which candidates the keep-set
// actually depends on (must stay) vs. truly severable (safe to drop), backed by
// grep/import evidence. Parallel coupling-analysis (one agent per candidate
// GROUP) -> barrier -> single synthesis agent emits an ordered, batched delete
// plan with build-check points, keep-set trims, and pyproject changes.
//
// READ-ONLY: analysis agents must not delete or edit anything. The plan is acted
// on by the caller inline, with a build check after each batch.
//
// RUN:  Workflow({ scriptPath: '<this file>', args: {
//         repo: '/Users/k/dev/headroom',
//         keepset: [ 'headroom/transforms/', 'headroom/ccr/', 'headroom/compress.py', ... ],
//         groups:  [ { name, paths: [...] }, ... ],
//       }})
// ITERATE: Workflow({ scriptPath: '<this file>', resumeFromRunId: '<runId>' })
// SAVE:    wf_lib.py save <this file> --group amputate
// ---------------------------------------------------------------------------

export const meta = {
  name: 'headroom-amputation-map',
  description: 'Map a forked repo to a keep-set: evidence-backed KEEP/DROP/TRIM verdicts + ordered delete plan.',
  whenToUse: 'Stripping a large forked repo down to a small keep-set safely, without breaking the kept code.',
  phases: [
    { title: 'Analyze', detail: 'parallel coupling analysis, one agent per candidate group' },
    { title: 'Plan', detail: 'synthesize verdicts into an ordered, batched delete plan' },
  ],
}

// ---- args contract --------------------------------------------------------
const A = (typeof args === 'string') ? JSON.parse(args) : args
if (!A || typeof A.repo !== 'string') throw new Error('expects args.repo (string)')
if (!Array.isArray(A.keepset) || A.keepset.length === 0) throw new Error('expects args.keepset (non-empty string[])')
if (!Array.isArray(A.groups) || A.groups.length === 0) throw new Error('expects args.groups (non-empty [{name, paths}])')

const REPO = A.repo
const KEEPSET = A.keepset
const GROUPS = A.groups

// ---- schemas --------------------------------------------------------------
const VERDICT = {
  type: 'object',
  properties: {
    candidate: { type: 'string' },
    verdict: { type: 'string', enum: ['KEEP', 'DROP', 'TRIM', 'UNSURE'] },
    keepset_depends_on_it: { type: 'boolean' },
    evidence: { type: 'array', items: { type: 'string' } },       // grep lines / file refs proving the verdict
    imports_from_keepset: { type: 'array', items: { type: 'string' } }, // keep-set modules this pulls in
    risk: { type: 'string', enum: ['low', 'medium', 'high'] },
    notes: { type: 'string' },
  },
  required: ['candidate', 'verdict', 'keepset_depends_on_it', 'evidence', 'risk'],
}
const GROUP_RESULT = {
  type: 'object',
  properties: { verdicts: { type: 'array', items: VERDICT } },
  required: ['verdicts'],
}
const PLAN = {
  type: 'object',
  properties: {
    drop_batches: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          batch: { type: 'number' },
          paths: { type: 'array', items: { type: 'string' } },
          rationale: { type: 'string' },
        },
        required: ['batch', 'paths', 'rationale'],
      },
    },
    keep: { type: 'array', items: { type: 'string' } },
    trim: {
      type: 'array',
      items: {
        type: 'object',
        properties: { path: { type: 'string' }, what_to_trim: { type: 'string' } },
        required: ['path', 'what_to_trim'],
      },
    },
    init_py_exports_to_keep: { type: 'array', items: { type: 'string' } },
    pyproject_changes: { type: 'array', items: { type: 'string' } },
    unsure: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' } },
  },
  required: ['drop_batches', 'keep', 'trim', 'risks'],
}

const KEEPSET_TXT = KEEPSET.map(k => `  - ${k}`).join('\n')

// ---- Phase 1: parallel coupling analysis (barrier) ------------------------
phase('Analyze')
const groupResults = (await parallel(
  GROUPS.map(g => () => agent(
`You are a coupling analyst stripping a forked Python+Rust repo down to a small KEEP-set.

REPO (scope ALL commands here, READ-ONLY — never edit or delete anything): ${REPO}

The KEEP-set — these modules MUST survive; everything else is a deletion candidate:
${KEEPSET_TXT}

Your candidate GROUP "${g.name}" — analyze EACH of these paths:
${g.paths.map(p => `  - ${p}`).join('\n')}

For each candidate path decide whether the KEEP-set depends on it. Method (use ripgrep \`rg\` from ${REPO}):
1. FORWARD dep — does any KEEP-set file import this candidate? For a dir headroom/X, run:
   rg -n "import headroom\\.X|from headroom\\.X|from \\.X |import X\\b" <each keep-set path>
   Also check headroom/__init__.py and headroom/compress.py specifically (they gate the public import surface).
2. REVERSE dep — what does the candidate import FROM the keep-set? rg the candidate's own files for "headroom.transforms|headroom.ccr|headroom.compress|headroom.tokenizer|headroom.config".
3. Build coupling — is the candidate referenced in pyproject.toml ([project.scripts], entry-points, optional-deps/extras, package data)? rg the candidate name in pyproject.toml.
4. For Rust crates (crates/X) — check Cargo.toml workspace members + whether headroom-core/headroom-py depend on it.

Verdict rule:
- KEEP  = a KEEP-set file imports it (transitively) OR the kept build needs it.
- DROP  = NOTHING in the keep-set imports it AND the kept build does not need it. Safe to delete.
- TRIM  = it IS in the keep-set but carries obvious bloat (orchestration, scoring engines, dead submodules) that can be cut.
- UNSURE= evidence is ambiguous; say exactly what you could not resolve.

Return one verdict object per candidate path in the group. evidence[] MUST contain the actual grep lines (or "no matches for <pattern>") that justify the verdict — not assertions. Put any keep-set modules the candidate pulls in into imports_from_keepset (this drives safe deletion order). Do NOT modify the repo.`,
    { label: `analyze:${g.name}`, phase: 'Analyze', schema: GROUP_RESULT, agentType: 'Explore' }
  ))
)).filter(Boolean)

const allVerdicts = groupResults.flatMap(r => r.verdicts || [])
log(`analyzed ${allVerdicts.length} candidates across ${groupResults.length}/${GROUPS.length} groups`)
const drops = allVerdicts.filter(v => v.verdict === 'DROP').length
const unsure = allVerdicts.filter(v => v.verdict === 'UNSURE').length
log(`DROP=${drops}  KEEP=${allVerdicts.filter(v => v.verdict === 'KEEP').length}  TRIM=${allVerdicts.filter(v => v.verdict === 'TRIM').length}  UNSURE=${unsure}`)

// ---- Phase 2: synthesize ordered delete plan ------------------------------
phase('Plan')
const plan = await agent(
`You are planning a SAFE incremental amputation of a forked repo, from a set of evidence-backed coupling verdicts.

REPO: ${REPO}
KEEP-set (must survive):
${KEEPSET_TXT}

Verdicts (JSON):
${JSON.stringify(allVerdicts, null, 1)}

Produce an ordered delete plan:
- drop_batches: order so that NO batch deletes a module that a not-yet-deleted DROP module still imports (use imports_from_keepset and reverse deps to sequence; leaf consumers first). Group ~3-6 paths per batch so a build check runs after each. Every path in a batch MUST have verdict DROP.
- keep: the union of KEEP candidates (+ the keep-set itself).
- trim: TRIM candidates with a concrete what_to_trim note.
- init_py_exports_to_keep: which symbols headroom/__init__.py should still export after stripping (only keep-set surface).
- pyproject_changes: deps / entry-points / extras / scripts tied to dropped modules that must be removed.
- unsure: any UNSURE candidate, with the exact unresolved question — these are NOT auto-dropped.
- risks: anything that could break the kept build.

Never place an UNSURE or KEEP candidate in a drop_batch. Be conservative: when in doubt, it goes to unsure, not drop.`,
  { label: 'synthesize-plan', phase: 'Plan', schema: PLAN }
)

return { verdictCount: allVerdicts.length, unsureCount: unsure, plan, verdicts: allVerdicts }
