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

DEEP-ANALYZE the lossless paths and find ANY AND ALL high-value techniques, then implement the highest-ROI / lowest-parity-risk ones:
- CompactionStage columnar/table path (crates/headroom-core/src/transforms/smart_crusher/compaction/*) — stronger per-column encoding: RLE for constant columns, delta/varint for monotone numeric (timestamps, counters), dictionary for low-cardinality strings, transpose for wide tables.
- Template / pattern extraction (LogCompressor, log_compressor.rs/.py): templatize recurring structured lines (one template + the varying fields stored columnar) so N near-identical lines collapse losslessly.
- Cross-row + cross-message substring dedup / reference (back-reference) encoding for repeated payloads.
- Schema inference for heterogeneous dict-arrays; field-aware (Imp2) extensions.
- Tokenizer-aware minification: drop redundant whitespace / shorten repeated JSON keys in a way the model still reads, measured in TOKENS (not bytes) via the real tokenizer.
- Routing: send more content to the lossless path where it wins.

${CONTRACTS}

${HONESTY}

${GUARDRAIL}

Return the structured report: analysis_summary (where lossless bytes/tokens are left on the table, file:line), techniques_found (ranked, with est_gain + parity_risk + implemented), implemented (with real before/after benchmark numbers + commit), benchmark_delta (overall measured lossless gain), roadmap (bigger techniques deferred + why), contracts_intact, guardrails (verbatim).`,
  { model: 'fable', label: 'lossless-frontier', phase: 'Lossless', schema: REPORT }
)
log(`Lossless: ${lossless?.implemented?.length || 0} implemented, contracts_intact=${lossless?.contracts_intact}, delta=${lossless?.benchmark_delta || '?'}`)

// ---- Frontier B: lossy-but-recoverable (runs after A) ----
phase('LossyRecoverable')
const lossy = await agent(
`You are a compression researcher. Your ONLY goal: maximize how much the engine REMOVES from the visible output while keeping 100% recoverable (every removed item fetchable via CCR) AND zero needles lost. The lossless frontier was just improved + committed; build on it. Current: logs drop ratio 74%, info retention 100%, needle-recall (output OR CCR) 100%.

REPO: ${REPO} (x86_64 venv at .venv)

DEEP-ANALYZE the lossy paths (crates/headroom-core/src/transforms/smart_crusher/{planning,orchestration,analyzer}.rs, relevance/*, the Python shims) and find ANY AND ALL techniques to remove MORE from the visible output without losing information, then implement the highest-ROI ones:
- Semantic / near-duplicate collapse: group items by similarity (not just byte-identity), keep ONE representative carrying multiplicity (_dup_count) + the varying values, recover the rest via CCR. This is the biggest lever — most "distinct" rows are near-duplicates modulo a few fields.
- Smarter relevance / query-aware adaptive-k: keep only what is relevant to the query, recover the rest. Tune adaptive_k down where CCR backs the drop.
- Better representative selection + cluster sampling so fewer kept items cover more distinct content.
- Stronger CCR-backed aggressive sampling: lower the kept budget when recovery is guaranteed.

${CONTRACTS}
(For this frontier especially: the win is a HIGHER drop ratio / lower token count at CONSTANT 100% information-retention + needle-recall. A drop that loses recoverability is a FAILURE.)

${HONESTY}

${GUARDRAIL}
(Plus: needle-recall (output OR CCR) MUST stay 100% — if any technique drops it below 100%, revert.)

Return the structured report: analysis_summary, techniques_found (ranked), implemented (with before/after drop-ratio + token numbers + retention proof + commit), benchmark_delta, roadmap, contracts_intact, guardrails (verbatim — FULL pytest + bench since you run last).`,
  { model: 'fable', label: 'lossy-frontier', phase: 'LossyRecoverable', schema: REPORT }
)
log(`Lossy: ${lossy?.implemented?.length || 0} implemented, contracts_intact=${lossy?.contracts_intact}, delta=${lossy?.benchmark_delta || '?'}`)

return { lossless, lossy }
