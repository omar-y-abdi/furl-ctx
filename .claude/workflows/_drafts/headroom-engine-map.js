// ---------------------------------------------------------------------------
// headroom-engine-map  —  workflow-architect
//
// Phase-2 consensus_gate: understand a compression/dedup engine BEFORE redesigning.
// Parallel deep-reads (one agent per facet of the keep/drop machinery) -> barrier
// -> single synthesis agent emits the consensus document: the current keep-vs-drop
// algorithm, the EXACT file:line points where distinct items ("needles") get
// silently dropped, where a single varying field defeats whole-row dedup, and how
// CCR recoverability/signalling works today. Feeds DESIGN.md.
//
// READ-ONLY: reader agents must not edit anything.
//
// RUN: Workflow({ scriptPath, args: { repo, facets: [{name, focus, paths, question}] } })
// SAVE: wf_lib.py save <this file> --group compression
// ---------------------------------------------------------------------------

export const meta = {
  name: 'headroom-engine-map',
  description: 'Map a compression engine\'s keep/drop algorithm + exact needle-loss and varying-field-defeat points; feeds a redesign.',
  whenToUse: 'Before redesigning a dedup/compression engine: locate where it drops distinct items and where varying fields defeat dedup.',
  phases: [
    { title: 'Read', detail: 'parallel deep-read, one agent per engine facet' },
    { title: 'Synthesize', detail: 'consensus doc: algorithm + needle-loss + varying-field + CCR map' },
  ],
}

const A = (typeof args === 'string') ? JSON.parse(args) : args
if (!A || typeof A.repo !== 'string') throw new Error('expects args.repo (string)')
if (!Array.isArray(A.facets) || A.facets.length === 0) throw new Error('expects args.facets (non-empty [{name, focus, paths, question}])')

const REPO = A.repo
const FACETS = A.facets

const READING = {
  type: 'object',
  properties: {
    facet: { type: 'string' },
    keep_drop_mechanism: { type: 'string' },        // how this facet decides keep vs drop
    needle_loss_points: {                            // where distinct items get silently dropped
      type: 'array',
      items: {
        type: 'object',
        properties: {
          location: { type: 'string' },             // file:line
          mechanism: { type: 'string' },
          severity: { type: 'string', enum: ['low', 'medium', 'high'] },
        },
        required: ['location', 'mechanism', 'severity'],
      },
    },
    varying_field_points: {                          // where one varying field defeats dedup
      type: 'array',
      items: {
        type: 'object',
        properties: { location: { type: 'string' }, mechanism: { type: 'string' } },
        required: ['location', 'mechanism'],
      },
    },
    ccr_recoverability: { type: 'string' },          // are dropped items CCR-recoverable / signalled?
    evidence: { type: 'array', items: { type: 'string' } },  // file:line citations
  },
  required: ['facet', 'keep_drop_mechanism', 'needle_loss_points', 'evidence'],
}

const CONSENSUS = {
  type: 'object',
  properties: {
    current_algorithm: { type: 'string' },
    needle_loss: {
      type: 'array',
      items: {
        type: 'object',
        properties: { location: { type: 'string' }, mechanism: { type: 'string' }, fix_hint: { type: 'string' } },
        required: ['location', 'mechanism', 'fix_hint'],
      },
    },
    varying_field_defeat: {
      type: 'array',
      items: {
        type: 'object',
        properties: { location: { type: 'string' }, mechanism: { type: 'string' }, fix_hint: { type: 'string' } },
        required: ['location', 'mechanism', 'fix_hint'],
      },
    },
    ccr_map: { type: 'string' },
    imp1_safe_dedup_options: {
      type: 'array',
      items: {
        type: 'object',
        properties: { option: { type: 'string' }, approach: { type: 'string' }, tradeoffs: { type: 'string' } },
        required: ['option', 'approach', 'tradeoffs'],
      },
    },
    imp2_field_aware_approach: { type: 'string' },
    open_questions: { type: 'array', items: { type: 'string' } },
  },
  required: ['current_algorithm', 'needle_loss', 'varying_field_defeat', 'ccr_map', 'imp1_safe_dedup_options', 'imp2_field_aware_approach'],
}

// ---- Phase 1: parallel deep-read (barrier) --------------------------------
phase('Read')
const readings = (await parallel(
  FACETS.map(f => () => agent(
`You are reverse-engineering a compression/dedup engine to find EXACTLY where it loses information. READ-ONLY — do not edit anything. Scope all commands to ${REPO}.

Facet: "${f.name}"
Focus: ${f.focus}
Start from these paths (follow imports/calls outward as needed): ${(f.paths || []).join(', ') || '(discover via rg)'}

Answer precisely, with file:line citations for every claim:
${f.question}

Specifically nail down:
1. keep_drop_mechanism — how does this code decide which items/rows/lines to KEEP vs DROP? (sampling? clustering? relevance threshold? representative selection?) Quote the decision site.
2. needle_loss_points — the EXACT file:line where a DISTINCT item that differs from its cluster/sample gets silently dropped (the "90 results -> kept 24" mechanism). Rate severity.
3. varying_field_points — where does a SINGLE varying column/field (timestamp, id, frame counter) prevent whole-row dedup, collapsing the ratio? Quote the hashing/grouping/equality site.
4. ccr_recoverability — when an item is dropped, is it stored in CCR and recoverable? Does the output SIGNAL to the model what was dropped? Cite the store/marker code.

Read the actual code — do not infer from names. evidence[] must be real file:line references.`,
    { label: `read:${f.name}`, phase: 'Read', schema: READING, agentType: 'Explore' }
  ))
)).filter(Boolean)

log(`read ${readings.length}/${FACETS.length} facets`)
const totalNeedle = readings.reduce((n, r) => n + (r.needle_loss_points?.length || 0), 0)
const totalVarying = readings.reduce((n, r) => n + (r.varying_field_points?.length || 0), 0)
log(`needle-loss points: ${totalNeedle} | varying-field points: ${totalVarying}`)

// ---- Phase 2: synthesize consensus doc ------------------------------------
phase('Synthesize')
const consensus = await agent(
`You are writing the consensus document that will drive a compression-engine redesign (Phase-2 consensus_gate).

Repo: ${REPO}
Per-facet readings (JSON, with file:line evidence):
${JSON.stringify(readings, null, 1)}

Synthesize into one coherent picture:
- current_algorithm: a precise prose description of how the engine decides keep vs drop end-to-end (entry -> sampling/clustering/scoring -> drop), naming the key files.
- needle_loss: the deduplicated list of EXACT points (file:line) where distinct items get silently dropped, each with a concrete fix_hint (e.g. "detect cluster outliers and pin them", "emit a CCR marker + dropped-count signal").
- varying_field_defeat: the points where a single varying field defeats dedup, each with a fix_hint toward columnar/field-aware decomposition (stable subfields compressed, varying keys kept compactly).
- ccr_map: how CCR recoverability + drop-signalling works today, and the gap vs "same answers".
- imp1_safe_dedup_options: concrete options (needle/anomaly-aware retention | explicit lossless toggle | CCR-recoverable+signalled), each with real tradeoffs grounded in THIS codebase.
- imp2_field_aware_approach: the concrete columnar decomposition approach for THIS engine.
- open_questions: anything unresolved that needs a benchmark measurement or a decision before implementing.

Ground every claim in the file:line evidence from the readings. Do not invent mechanisms not present in the readings.`,
  { label: 'synthesize-consensus', phase: 'Synthesize', schema: CONSENSUS }
)

return { facetCount: readings.length, needlePoints: totalNeedle, varyingPoints: totalVarying, consensus, readings }
