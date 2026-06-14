// ---------------------------------------------------------------------------
// headroom-max-compression — two fable agents push the engine's compression to
// its limits along two frontiers: (A) lossless/structural (toward the entropy
// floor), (B) lossy-but-recoverable (more removed, still 100% recoverable).
// Deep analysis -> find every high-value technique -> implement highest-ROI ->
// benchmark real gain -> report gains + ranked roadmap. Sequential (both may
// touch smart_crusher; shared maturin build + git index).
// ---------------------------------------------------------------------------

export const meta = {
  name: 'headroom-max-compression',
  description: 'Two fable agents maximize compression (lossless frontier, then lossy-recoverable frontier) within the recovery + cache-safety + parity contracts.',
  phases: [
    { title: 'Lossless', detail: 'fable: maximize zero-loss reduction (columnar/template/dedup/encoding)' },
    { title: 'LossyRecoverable', detail: 'fable: maximize removed-but-recoverable (semantic dedup/relevance/sampling)' },
  ],
}

const A = (typeof args === 'string') ? JSON.parse(args) : args
const REPO = (A && A.repo) || '/Users/k/dev/headroom'

const REPORT = {
  type: 'object',
  properties: {
    frontier: { type: 'string' },
    analysis_summary: { type: 'string' },              // where compression is left on the table, with file:line
    techniques_found: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          where: { type: 'string' },                   // file:line / path
          est_gain: { type: 'string' },                // estimated benchmark impact
          parity_risk: { type: 'string', enum: ['none', 'low', 'medium', 'high'] },
          implemented: { type: 'boolean' },
        },
        required: ['name', 'where', 'est_gain', 'parity_risk', 'implemented'],
      },
    },
    implemented: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          what: { type: 'string' },
          benchmark_before: { type: 'string' },
          benchmark_after: { type: 'string' },
          commit: { type: 'string' },
        },
        required: ['name', 'what', 'benchmark_after'],
      },
    },
    benchmark_delta: { type: 'string' },               // overall measured gain on the honest suite
    roadmap: {
      type: 'array',
      items: {
        type: 'object',
        properties: { name: { type: 'string' }, est_gain: { type: 'string' }, why_deferred: { type: 'string' } },
        required: ['name', 'est_gain', 'why_deferred'],
      },
    },
    contracts_intact: { type: 'boolean' },
    guardrails: { type: 'string' },
  },
  required: ['frontier', 'analysis_summary', 'techniques_found', 'implemented', 'contracts_intact'],
}

const CONTRACTS = `HARD CONTRACTS — pushing compression must NOT break ANY of these (a "smaller" output that violates one is a FAILURE, not a win):
- CCR recovery invariant: every distinct item removed/substituted on the public compress() path stays recoverable via a <<ccr:HASH>> pointer surfaced in the output + the original in the CCR store. Locked by tests/test_ccr_recovery_invariant.py (17 tests). 0 silent loss.
- Prompt-cache ordering (P0): NEVER drop the message at index 0, NEVER reorder the cached message prefix. Compression operates on content WITHIN messages, not on message order. If you touch message-level handling, add/keep a test proving index-0 + order are preserved.
- cache_control markers (P0): byte-faithful passthrough of anything carrying cache_control; do not rewrite cached blocks.
- Python<->Rust parity: compute_item_hash / canonical CCR hash / the retrieve contract / existing dict default-config output are byte-stable and parity-pinned. Prefer ADDITIVE improvements (new auto-selected lossless modes) that don't change existing outputs; if you must change shared output, change BOTH Python and Rust in lockstep and update parity fixtures with explicit justification — never weaken a parity test to pass.
- No synthetic low-entropy benchmark inputs. Real data only. You MAY add NEW real datasets (captured locally) where a gain shows.
Read DESIGN.md, BENCHMARKS.md, and recent git log FIRST.`

const GUARDRAIL = `GUARDRAIL — after each technique, ALL must hold (paste verbatim). Revert anything that breaks one or fails to show a real gain:
- cargo: \`cargo test -p headroom-core 2>&1 | grep "test result:"\` — 0 failed.
- build: \`.venv/bin/maturin develop\` — GREEN.
- python: \`.venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --continue-on-collection-errors --timeout=120\` — >=380 passed, 0 failed.
- contract: \`.venv/bin/python -m pytest tests/test_ccr_recovery_invariant.py -q\` — 17 passed.
- bench: \`.venv/bin/python -m benchmarks.run_bench\` — needle-recall (output OR CCR) stays 100%; record the lossless/drop numbers BEFORE and AFTER your change; then \`git checkout HEAD -- benchmarks/baseline_results.json benchmarks/BASELINE.md\`.
- Commit each technique separately (descriptive message, no Co-Authored-By). A routing-gate hook needs ./.claude/workflow/DEFAULT_WORKFLOW.md read (cwd-relative, exists) before Write/Bash.`

const HONESTY = `BE HONEST: some data is genuinely incompressible (code@7 = 0% — distinct source files near the entropy floor; do NOT fake a gain there). Report techniques that did NOT pan out and WHY. A measured +3% that is real beats a claimed +40% that isn't. Every number from the real benchmark.`

// ---- Frontier A: lossless / structural ----
phase('Lossless')
const lossless = await agent(
`You are a compression researcher. Your ONLY goal: push the LOSSLESS (zero-information-loss) compression ratio of this engine toward its theoretical limit (the Shannon entropy floor) on REAL data. Current honest lossless: code 0%, search 40%, repeated_logs 54%. The headroom is on structured/repetitive data.

REPO: ${REPO} (x86_64 venv at .venv; \`.venv/bin/maturin develop\` rebuilds the ext)

ROUND 3 — INDEPENDENT VERIFICATION CAUGHT US. An adversarial verifier (read verify/REPORT.md + verify/raw_results.json) re-tested the engine on FRESH, REAL, high-entropy out-of-sample data (freshly-cloned public repos, real API dumps, near-unique rows with realistic uuids/timestamps/hashes). The dev compression claims were measured on LOW-entropy fixtures and DO NOT hold on realistic near-unique data:
- search@90 high-entropy: claim 92.7% -> fresh 36.1% (UNSTABLE 24.5-93.7%) — lossless folding collapses on unique paths + uuid match-text.
- logs@90 high: 93.0% -> 80.2%. disk@9 high: 50% -> 43.3%.
The instability (24-94%) is ITSELF a failure: the engine behaves erratically on near-unique rows.

YOUR JOB (lossless frontier): diagnose WHY lossless folding/columnar collapses on near-unique structured rows, then find GENERAL lossless techniques that extract real reduction from high-entropy STRUCTURED data — WITHOUT inflating numbers.
- HONEST PHYSICS FIRST: genuinely-random unique values sit at the Shannon entropy floor and CANNOT be losslessly compressed much. If a column is true entropy, SAY SO — never fake a lossless gain on random data. Distinguish "lossless can't (physics)" from "we left structure on the table (fixable)".
- The real lossless headroom on "near-unique" rows is the STRUCTURE that repeats even when values differ: shared schema/keys, shared prefixes/suffixes/common substrings across rows, shared path components, common templates with unique tails. Implement GENERAL techniques (cross-row prefix/suffix/common-substring sharing, schema/key factoring, template+unique-tail splitting) that work on ANY near-unique structured data.
- Every encoding stays reconstruction-exact (decode == original; pin it through the public decoder + a round-trip test). Keep JSON formatter byte-parity.

ANTI-CHEAT (NON-NEGOTIABLE, user-mandated): falsifying gains by special-casing, hardcoding, pattern-matching, or tuning to the verify/ data is a HARD FAIL. Do NOT import, read-to-tune, or branch on verify/ fixtures or their generators. The goal is met ONLY when compression improves on ALL kinds of near-unique structured rows. Develop against your OWN freshly-generated high-entropy data (different from verify/ AND benchmarks/); the FINAL judge is a held-out adversarial re-verification the orchestrator runs on NEW data you never saw. If you can only hit the number by overfitting, REPORT that honestly instead — a real +X% that generalizes beats a fake +Y% that doesn't.

${CONTRACTS}

${HONESTY}

${GUARDRAIL}

Return the structured report: analysis_summary (where lossless bytes/tokens are left on the table, file:line), techniques_found (ranked, with est_gain + parity_risk + implemented), implemented (with real before/after benchmark numbers + commit), benchmark_delta (overall measured lossless gain), roadmap (bigger techniques deferred + why), contracts_intact, guardrails (verbatim).`,
  { label: 'lossless-frontier', phase: 'Lossless', schema: REPORT }
)
log(`Lossless: ${lossless?.implemented?.length || 0} implemented, contracts_intact=${lossless?.contracts_intact}, delta=${lossless?.benchmark_delta || '?'}`)

// ---- Frontier B: lossy-but-recoverable (runs after A) ----
phase('LossyRecoverable')
const lossy = await agent(
`You are a compression researcher. Your ONLY goal: maximize how much the engine REMOVES from the visible output while keeping 100% recoverable (every removed item fetchable via CCR) AND zero needles lost. The lossless frontier was just improved + committed; build on it. Current: logs drop ratio 74%, info retention 100%, needle-recall (output OR CCR) 100%.

REPO: ${REPO} (x86_64 venv at .venv)

ROUND 3 — same verification context (read verify/REPORT.md + verify/raw_results.json). The biggest miss is on the LOSSY-recoverable path on HIGH-ENTROPY near-unique data: search@90 high-entropy is 36.1% mean but ranges 24.5-93.7% — ERRATIC. At scale (search@900 high) it reaches 86.9%, so the lossy drop+recover path CAN compress near-unique data (by hiding+recovering it) — it just does so UNRELIABLY on smaller / near-unique arrays.

YOUR JOB (lossy frontier): make the lossy-recoverable path RELIABLY aggressive on high-entropy near-unique data so it reaches the dev-claim level (~92% search, ~93% logs, ~71% multiturn) CONSISTENTLY — the 24-94% scatter must collapse to a tight HIGH band (low variance is part of the success criterion; an unstable high mean is still a fail).
- DIAGNOSE the erratic routing/keep-set/budget behavior on near-unique SMALL arrays: why does min-tokens sometimes keep almost everything (36%) and sometimes hit 94% on the same shape? Find the inconsistency (gating threshold, adaptive_k, keep-set sizing, the lossless-vs-lossy token comparison) and make it deterministic + aggressive where lossless can't help (entropy floor).
- The lever: on high-entropy data the engine should RELIABLY drop+recover aggressively — everything stays 100% recoverable + signalled (the recovery invariant), so a smaller visible render is the right call. Fix routing so min-tokens consistently picks the aggressive recoverable render.
- RESPECT cache-ordering: NEVER drop/move message index 0, NEVER reorder the cached prefix, NEVER rewrite cache_control blocks (a test must prove it).
- HONESTY: lossy "savings" on high-entropy data are real (recoverable) but carry retrieval cost — REPORT effective-savings-under-retrieval at {0,25,50}% for your improved cases (the verifier measures this; do not pretend it away). The TTL/result-cache silent-loss bug is being fixed separately — build on the fixed engine. Maintain 100% recovery + needle-signal.

ANTI-CHEAT (NON-NEGOTIABLE, user-mandated): no special-casing, hardcoding, pattern-matching, or tuning to verify/ data — a HARD FAIL. The fix must generalize to ALL near-unique structured rows. Develop against your OWN freshly-generated high-entropy data (not verify/, not benchmarks/); the held-out re-verification on NEW data is the final judge. If the number is only reachable by overfitting, report that honestly instead.

${CONTRACTS}
(For this frontier especially: the win is a HIGHER drop ratio / lower token count at CONSTANT 100% information-retention + needle-recall. A drop that loses recoverability is a FAILURE.)

${HONESTY}

${GUARDRAIL}
(Plus: needle-recall (output OR CCR) MUST stay 100% — if any technique drops it below 100%, revert.)

Return the structured report: analysis_summary, techniques_found (ranked), implemented (with before/after drop-ratio + token numbers + retention proof + commit), benchmark_delta, roadmap, contracts_intact, guardrails (verbatim — FULL pytest + bench since you run last).`,
  { label: 'lossy-frontier', phase: 'LossyRecoverable', schema: REPORT }
)
log(`Lossy: ${lossy?.implemented?.length || 0} implemented, contracts_intact=${lossy?.contracts_intact}, delta=${lossy?.benchmark_delta || '?'}`)

return { lossless, lossy }
