// ---------------------------------------------------------------------------
// headroom-recoverability-refute  —  workflow-architect (adversarial verify)
//
// Trust check for the engine's core claim after Imp1-1A: "no silent loss — every
// distinct item the lossy path drops is either visible in the output OR fully
// CCR-recoverable." Parallel skeptics each try to CONSTRUCT a counterexample:
// an input to compress() where a distinct item is dropped AND not recoverable.
// Refute-by-default: assume the claim holds only if no skeptic breaks it.
//
// Each skeptic writes + runs its own probe script (ad-hoc, under /tmp) against the
// installed engine; READ-ONLY w.r.t. the repo (no engine/benchmark edits, no commits).
//
// RUN: Workflow({ scriptPath, args: { repo, angles: [{name, hypothesis}] } })
// ---------------------------------------------------------------------------

export const meta = {
  name: 'headroom-recoverability-refute',
  description: 'Adversarially refute "no silent loss / every dropped item is CCR-recoverable" by constructing counterexamples.',
  whenToUse: 'Verifying a lossy compressor never loses a distinct item unrecoverably, before trusting a "same answers" claim.',
  phases: [
    { title: 'Refute', detail: 'parallel skeptics each try to build a silent-loss counterexample' },
    { title: 'Verdict', detail: 'synthesize: is the no-silent-loss claim refuted?' },
  ],
}

const A = (typeof args === 'string') ? JSON.parse(args) : args
if (!A || typeof A.repo !== 'string') throw new Error('expects args.repo (string)')
if (!Array.isArray(A.angles) || A.angles.length === 0) throw new Error('expects args.angles (non-empty [{name, hypothesis}])')
const REPO = A.repo
const ANGLES = A.angles

const PROBE = {
  type: 'object',
  properties: {
    angle: { type: 'string' },
    refuted: { type: 'boolean' },                 // true = found a real silent-loss / unrecoverable drop
    counterexample: { type: 'string' },           // the exact input + what was lost, or "none found"
    recoverable_path_checked: { type: 'string' },  // how recovery was attempted (CCR marker parse + retrieve)
    evidence: { type: 'array', items: { type: 'string' } }, // probe output / file:line
    confidence: { type: 'string', enum: ['low', 'medium', 'high'] },
  },
  required: ['angle', 'refuted', 'counterexample', 'evidence', 'confidence'],
}

const VERDICT = {
  type: 'object',
  properties: {
    claim_holds: { type: 'boolean' },
    refutations: { type: 'array', items: { type: 'string' } }, // confirmed silent-loss cases
    residual_risks: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
  required: ['claim_holds', 'refutations', 'summary'],
}

phase('Refute')
const probes = (await parallel(
  ANGLES.map(a => () => agent(
`You are an adversary trying to BREAK this claim about the Headroom compression engine:
"After the unconditional-CCR-persist change (sub-step 1A), the lossy path never silently loses a distinct item — every dropped item is either present in the compressed output OR fully recoverable via CCR."

Default stance: the claim is REFUTED (set refuted=true) UNLESS you fail to break it after a genuine effort. Be skeptical.

Repo (installed editable, READ-ONLY — do NOT edit engine/benchmark files, do NOT commit): ${REPO}
Use the venv: ${REPO}/.venv/bin/python. compress() is \`from headroom import compress\`. CCR retrieval via \`from headroom.cache.compression_store import CompressionStore\` (.store/.retrieve) and/or the Rust \`ccr_get\`; the dropped-rows marker is the sentinel \`{"_ccr_dropped": "<<ccr:HASH N>>"}\` appended to the output array — parse the HASH and retrieve to confirm the original is recoverable.

YOUR ANGLE: "${a.name}" — ${a.hypothesis}

Method: WRITE a small probe script under /tmp (not in the repo), construct a real input that exercises your angle, call compress(), then attempt to recover EVERY input item from (output items) ∪ (CCR-recovered original via the marker hash). A distinct input item that is in NEITHER is a silent-loss counterexample → refuted=true with the exact input + the lost item as the counterexample. Try hard: marker-off config, ccr disabled, non-dict arrays (lists of strings/numbers), deeply nested, very high cardinality (1000+), unicode, the Python-shim vs Rust path, arrays that hit each planner (TimeSeries/Cluster/TopN/SmartSample).

Read the relevant engine code (crusher.rs CCR emit ~691-706, compression_store.py, ccr/mod.rs) to find where persistence could still be skipped. Cite file:line + paste probe output as evidence. If you cannot construct a counterexample, refuted=false and say what you tried.`,
    { label: `refute:${a.name}`, phase: 'Refute', schema: PROBE, agentType: 'general-purpose' }
  ))
)).filter(Boolean)

const broke = probes.filter(p => p.refuted)
log(`probes: ${probes.length} | refutations: ${broke.length}`)

phase('Verdict')
const verdict = await agent(
`Synthesize the adversarial verdict on: "the lossy path never silently loses a distinct item — every dropped item is visible OR CCR-recoverable."

Probe results (JSON):
${JSON.stringify(probes, null, 1)}

- claim_holds = true ONLY if NO probe produced a real, reproduced silent-loss counterexample (a distinct input item present in neither the output nor the CCR-recovered original).
- refutations: list each confirmed counterexample (angle + the exact lost item + why recovery failed).
- residual_risks: low-confidence or unverified concerns worth a follow-up.
- summary: one honest paragraph — does "no silent loss" hold on the current committed engine?

Do not accept a refutation that wasn't actually reproduced with probe output. Do not dismiss one that was.`,
  { label: 'verdict', phase: 'Verdict', schema: VERDICT }
)

return { probeCount: probes.length, refutations: broke.length, verdict, probes }
