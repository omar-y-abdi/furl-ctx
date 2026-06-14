// ---------------------------------------------------------------------------
// headroom-adversarial-verify — independent break-test of the compression claims
// on FRESH out-of-sample data. Phase 1: a verifier builds a NEW self-contained
// harness in verify/ (new generators, real external data, engine's own
// compress/decode/retrieve), runs the full sweep. Phase 2: a SECOND independent
// auditor tries to break the harness itself (is it cheating? real hash-compare?
// cold cache? default params?), spot-re-runs, and writes the honest REPORT.md.
// NEITHER agent may modify the engine or tune anything.
// ---------------------------------------------------------------------------

export const meta = {
  name: 'headroom-adversarial-verify',
  description: 'Independent adversarial verification of compression claims on fresh out-of-sample data, with a second agent auditing the harness for cheating.',
  phases: [
    { title: 'HarnessRun', detail: 'build NEW harness + fresh data + full sweep' },
    { title: 'AuditReport', detail: 'second agent audits the harness for cheats + writes honest REPORT.md' },
  ],
}

const A = (typeof args === 'string') ? JSON.parse(args) : args
const REPO = (A && A.repo) || '/Users/k/dev/headroom'

const BUILD = {
  type: 'object',
  properties: {
    harness_built: { type: 'boolean' },
    data_sources: {
      type: 'array',
      items: {
        type: 'object',
        properties: { type: { type: 'string' }, source: { type: 'string' }, real_external: { type: 'boolean' }, note: { type: 'string' } },
        required: ['type', 'source', 'real_external'],
      },
    },
    cases_run: { type: 'number' },
    default_params_confirmed: { type: 'boolean' },
    raw_results: { type: 'string' },       // key numbers + path to committed raw json
    degradations: { type: 'array', items: { type: 'string' } },  // where fresh data scored BELOW the dev claim
    hash_failures: { type: 'array', items: { type: 'string' } }, // any non-byte-exact reconstruction
    commit: { type: 'string' },
    summary: { type: 'string' },
  },
  required: ['harness_built', 'data_sources', 'cases_run', 'default_params_confirmed', 'raw_results', 'summary'],
}

const AUDIT = {
  type: 'object',
  properties: {
    harness_legit: { type: 'boolean' },
    cheats_found: {
      type: 'array',
      items: {
        type: 'object',
        properties: { issue: { type: 'string' }, severity: { type: 'string', enum: ['low', 'medium', 'high', 'critical'] } },
        required: ['issue', 'severity'],
      },
    },
    spot_recheck: { type: 'string' },
    numbers_that_replicate: { type: 'array', items: { type: 'string' } },
    numbers_that_degrade: {
      type: 'array',
      items: {
        type: 'object',
        properties: { case: { type: 'string' }, dev_claim: { type: 'string' }, fresh_result: { type: 'string' }, delta: { type: 'string' } },
        required: ['case', 'dev_claim', 'fresh_result', 'delta'],
      },
    },
    report_committed: { type: 'boolean' },
    honest_verdict: { type: 'string' },
    commit: { type: 'string' },
  },
  required: ['harness_legit', 'cheats_found', 'numbers_that_replicate', 'numbers_that_degrade', 'honest_verdict'],
}

const SPEC = `ROLE: INDEPENDENT VERIFIER. Assume the reported compression numbers are inflated and non-replicable until proven otherwise on data YOU generate fresh. Your job is to BREAK them, not confirm. A clean pass is suspicious — hunt for where they degrade.

CLAIMS UNDER TEST (measured during dev on FIXED fixtures — treat as suspect):
  logs@90 93.0% | search@90 92.7% | repeated_logs@90 97.1% | multiturn@135 70.8% | disk@9 50% lossless | code@7 0%. All claimed "100% recoverable".

HARD RULES:
- Do NOT modify the engine (crates/, headroom/). Do NOT tune anything to the new data. Create files ONLY under ${REPO}/verify/heldout/.
- Generate a DIFFERENT variation of test data. Do NOT re-run, reuse, or import the existing benchmarks/ fixtures or their generators. Out-of-sample only. Write NEW generators.
- Use the engine's OWN public surface to compress AND to reconstruct: \`from headroom import compress\`; reconstruct the original from the compressed output ALONE using the engine's documented decoder (headroom/transforms/csv_schema_decoder.py) + CCR retrieve (headroom.cache.compression_store / ccr_get) by parsing the <<ccr:HASH>> pointer out of the compressed output. Do NOT reimplement compression or hand-roll a decoder.
- Use committed DEFAULT params/thresholds (RoutingPolicy default = MinTokens, etc.). If any run uses a non-default, FAIL LOUD and list it.
- Cold CCR/cache state per case (fresh store; no warm cache carried between runs). Fixed seeds; re-runnable by a third party. Sweep >= 5 seeds per case; report mean ± min/max. NO best-of-N cherry-picking.

FRESH DATA (different in content, structure, seed):
- 3 entropy tiers per type: low (repetitive), medium, high (near-unique rows). 2+ sizes per type (e.g. 90 and 900 items). Realistic per-row unique fields (timestamps, ids, uuids) — no artificially clean rows.
- REAL external data, not project fixtures: clone a DIFFERENT public git repo for the code/search cases; use a real lockfile / real CI log / real JSON API dump for the logs case. CITE each source exactly. If network/clone is unavailable, use REAL local data that is NOT a project fixture, and say so.
- THIS IS A SECOND, HELD-OUT RUN. The FIRST verification (committed under verify/) used github.com/sindresorhus/slugify + is-plain-obj + the GitHub commits API + npm registry. You MUST use DIFFERENT public repos (NOT slugify, NOT is-plain-obj) and DIFFERENT seeds, so the engine improvers — who measured against verify/'s data during their round-3 work — provably never saw THIS data. Pick genuinely new sources (e.g. other well-known public repos with deep directory trees + many files for the path/search case).
- ROUND-3 IMPROVEMENT CLAIMS TO TEST (in ADDITION to the original dev claims): on high-entropy near-unique data the improvers claim — search@90 high-entropy 36.1% (erratic 24-94%) -> 93.6% RELIABLE (±0.1pp); logs@90 high 80.2% -> 82.4%; a lossless cross-row "affix fold" + "head-dictionary" giving large reductions on STRUCTURED near-unique string columns (shared path roots/url roots/module names/keys) while genuine-entropy columns (random uuid/sha) get NO encoding (no fake gain); a deterministic "entropy-floor crushability override" making the lossy drop reliably aggressive on near-unique rows. FOR EACH: does it REPLICATE on YOUR new held-out data? A gain that showed on verify/'s slugify data but does NOT replicate on different repos = OVERFITTING — flag it loudly as a FAIL of the anti-cheat mandate. Also confirm the affix/head encodings fire on STRUCTURED columns but correctly DECLINE on genuine-entropy columns (no fake gain on random data). Confirm the TTL/result-cache silent-loss bug stays fixed (re-run a divergence probe).

MEASURE per case (with the engine's own compress()):
- Raw token reduction (same tokenizer the dev numbers used: gpt-4o tiktoken via the engine).
- Lossless vs lossy: reconstruct from the compressed output + CCR, then assert the reconstruction equals the ORIGINAL by HASH equality (sha256 of canonicalized original vs reconstructed). "recoverable=100%" is TRUE ONLY if the hash matches — PROVE it per case, do not assert it. Any mismatch is a hash_failure — record the case + the diff.
- If lossy: EFFECTIVE savings under retrieval. Plant K unique needles; simulate the model retrieving the dropped items it would actually need. Report effective savings at retrieval-rate {0%, 25%, 50%} INCLUDING the round-trip token overhead (retrieve call + retrieved content tokens).
- Needle test (especially search/high-entropy): how many planted needles survive UNCOMPRESSED (visible)? For dropped needles, are they (a) recoverable AND (b) SIGNALLED in the compressed output (a <<ccr:HASH>> pointer the model would see)? An unsignalled drop = SILENT data loss — flag it loudly.
- Multiturn: assert the cached prefix (cache_control-bearing blocks / leading messages, esp. index 0) is NOT dropped or reordered (prompt-cache safety). Flag any reorder/drop of the cached prefix.`

// ---- Phase 1: build harness + fresh data + full sweep ----
phase('HarnessRun')
const build = await agent(
`${SPEC}

YOUR TASK (Phase 1 — build + run): create a self-contained, committed, re-runnable harness under ${REPO}/verify/heldout/:
- NEW data generators (verify/generators.py or similar) — independent of benchmarks/. Cover the 3 types (code, logs/search-shaped structured rows, multiturn conversations), 3 entropy tiers, 2+ sizes, >=5 seeds, realistic unique per-row fields.
- Fetch/assemble the REAL external data (cite sources in a verify/SOURCES.md; commit small snapshots under verify/data/).
- The measurement core (verify/measure.py): compress via the engine, reconstruct via the engine's decoder + CCR retrieve, sha256 hash-compare original vs reconstructed, token reduction, effective-savings-under-retrieval at {0,25,50}%, needle survival + signal detection, multiturn cache-prefix safety.
- A runner (verify/run.py) that executes the full sweep with fixed seeds and writes verify/raw_results.json (machine-readable, per case: mean ± min/max).
Run it. Commit the harness + generators + seeds + raw_results.json + SOURCES.md (descriptive message, no Co-Authored-By; read ./.claude/workflow/DEFAULT_WORKFLOW.md first if the routing hook blocks Write/Bash). Do NOT write REPORT.md — that is the auditor's job in Phase 2.

Return the structured build report: harness_built, data_sources (cited, real_external flag), cases_run, default_params_confirmed (true ONLY if every run used committed defaults), raw_results (the key fresh numbers + the committed json path), degradations (every case where fresh data scored BELOW the dev claim — this is the POINT, report it), hash_failures (any non-byte-exact reconstruction), commit, summary.`,
  { label: 'verify-harness', phase: 'HarnessRun', schema: BUILD }
)
log(`Harness: ${build?.cases_run || 0} cases, default_params=${build?.default_params_confirmed}, degradations=${build?.degradations?.length || 0}, hash_failures=${build?.hash_failures?.length || 0}`)

// ---- Phase 2: independent audit of the harness + honest REPORT.md ----
phase('AuditReport')
const audit = await agent(
`${SPEC}

YOUR TASK (Phase 2 — AUDIT the Phase-1 harness, then write the verdict). A first verifier built ${REPO}/verify/heldout/ and ran it. Be skeptical of IT too — a lenient harness produces fake clean passes. READ the held-out harness it just built (verify/heldout/*.py) and verify/heldout/raw_results.json and AUDIT it hard:
- Does it ACTUALLY reconstruct from the compressed output + CCR and sha256-compare to the ORIGINAL — or does it secretly assert/short-circuit/compare against itself? Trace the reconstruction path.
- Cold CCR/cache per case, or warm-cache leakage inflating recovery? Fixed seeds actually varied? >=5 seeds, mean±range not best-of-N?
- DEFAULT params everywhere (RoutingPolicy=MinTokens etc.)? Any special-casing/branching/hardcoding on the new data? Did it touch engine source (git diff crates/ headroom/ must be EMPTY)?
- Is the "real external data" actually external + real, or did it quietly fall back to project fixtures?
- Re-run a SPOT subset yourself (a high-entropy case + the search needle case) to confirm the numbers reproduce identically.
Then WRITE ${REPO}/verify/heldout/REPORT.md:
- Table: type | dev-claim | fresh mean ± range | lossless? | effective savings @25%/@50% retrieval | needle survival | roundtrip hash OK.
- Plain verdict: which numbers replicate, which DEGRADE on fresh/high-entropy/real data, and by how much. If ANY case is below the dev claim, STATE IT EXPLICITLY — that is the result, not a failure.
- Note any cheats/bugs you found in the harness itself, and whether you trust the result after your spot-recheck.
Commit REPORT.md (+ any harness fixes you had to make to de-cheat it — but if you change the harness, say so and re-run). Read ./.claude/workflow/DEFAULT_WORKFLOW.md first if blocked.

Return: harness_legit, cheats_found (with severity), spot_recheck (what you re-ran + did it match), numbers_that_replicate, numbers_that_degrade (case/dev_claim/fresh_result/delta), report_committed, honest_verdict (the plain-English bottom line), commit.`,
  { label: 'verify-audit', phase: 'AuditReport', schema: AUDIT }
)
log(`Audit: legit=${audit?.harness_legit}, cheats=${audit?.cheats_found?.length || 0}, degrade=${audit?.numbers_that_degrade?.length || 0}`)

return { build, audit }
