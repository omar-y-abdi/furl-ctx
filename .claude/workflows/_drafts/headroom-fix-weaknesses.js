// headroom-fix-weaknesses — two agents fix the two real weaknesses the held-out
// adversarial verification (verify/heldout/REPORT.md) exposed: (A) single-blob
// CCR retrieval economics, (B) harness leniencies + dishonest headline numbers.
// Sequential (both touch the engine/repo; shared build+git). Inherit model (Opus).

export const meta = {
  name: 'headroom-fix-weaknesses',
  description: 'Fix the held-out-verified weaknesses: granular per-chunk CCR retrieval, and strict/honest verification + benchmarks.',
  phases: [
    { title: 'GranularCCR', detail: 'per-chunk CCR offload so retrieval is proportional, not whole-blob' },
    { title: 'HonestVerify', detail: 'strict-by-default harness + honest tier-aware/retrieval-aware BENCHMARKS.md' },
  ],
}

const A = (typeof args === 'string') ? JSON.parse(args) : args
const REPO = (A && A.repo) || '/Users/k/dev/headroom'

const REPORT = {
  type: 'object',
  properties: {
    frontier: { type: 'string' },
    what_changed: { type: 'string' },
    files: { type: 'array', items: { type: 'string' } },
    before_after: { type: 'string' },
    contracts_intact: { type: 'boolean' },
    guardrails: { type: 'string' },
    honest_caveats: { type: 'string' },
    commit: { type: 'string' },
  },
  required: ['frontier', 'what_changed', 'contracts_intact', 'guardrails'],
}

const CONTEXT = `FULL CONTEXT (read these first): DESIGN.md, BENCHMARKS.md, verify/REPORT.md, verify/heldout/REPORT.md, recent git log. The project is a forked LLM-context compression engine (Rust core crates/headroom-core + pyo3 + thin Python in headroom/transforms,ccr,cache). It was amputated 384k->~91k LOC, then hardened: an adversarial loop proved + locked the CCR recovery invariant (every dropped/substituted distinct item on the public compress() path is recoverable via a <<ccr:HASH>> pointer surfaced in the output + the original in the CCR store; tests/test_ccr_recovery_invariant.py = 21 tests), a TTL/result-cache silent-loss bug was fixed (recompute-on-unbackable), and lossless encodings (affix-fold, head-dict, dict, delta) + route-by-min-tokens (RoutingPolicy=MinTokens default) were added. An INDEPENDENT held-out verification on fresh real repos (express/chalk/npm-cli) confirmed: recovery is real + byte-exact, round-3 gains generalize (not overfit), but two real weaknesses remain (your job).

HARD CONTRACTS — do NOT break: CCR recovery invariant (100% recoverable, 0 silent loss), prompt-cache ordering (never drop index 0 / reorder cached prefix / rewrite cache_control), Python<->Rust parity (canonical CCR hash compute_item_hash byte-stable), default-config behavior of the lossless decoder. Keep cargo/maturin/pytest green throughout. No synthetic benchmark data. No overfitting to verify/ fixtures.`

const GUARD = `GUARDRAIL (run all, paste verbatim; revert anything that breaks one): cargo test -p headroom-core (0 failed); .venv/bin/maturin develop (GREEN); .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --continue-on-collection-errors --timeout=120 (>=420 passed, 0 failed); .venv/bin/python -m pytest tests/test_ccr_recovery_invariant.py -q (all pass); .venv/bin/python -m benchmarks.run_bench (needle-recall 100%) then git checkout HEAD -- benchmarks/baseline_results.json benchmarks/BASELINE.md. Commit per change, descriptive, no Co-Authored-By. A routing-gate hook needs ./.claude/workflow/DEFAULT_WORKFLOW.md read first if it blocks Write/Bash.`

// ---- Frontier A: granular per-chunk CCR ----
phase('GranularCCR')
const ccr = await agent(
`You are fixing the BIGGEST real weakness the held-out verifier found.

${CONTEXT}

THE WEAKNESS (verify/heldout/REPORT.md, auditor finding): the engine offloads ALL dropped rows of an array into ONE CCR blob under a single <<ccr:HASH>>; a single retrieve returns the WHOLE blob. So the moment the model needs even ONE dropped row it pays for the entire offloaded payload — effective savings can go NEGATIVE under retrieval (logs@90 high: +55.7% @25% retrieval -> -10.3% worst-case, MORE tokens than uncompressed). The lossy "savings" are only real if the model retrieves nothing.

YOUR JOB: make CCR retrieval PROPORTIONAL to what is actually needed — offload dropped rows as GRANULAR, individually-addressable chunks (per-row or per-small-group), each with its own hash, so retrieving one needed item fetches only that item (or its small group), not the whole blob. Find where the lossy drop persists + emits the sentinel (crusher.rs persist_dropped / ccr_dropped_sentinel ~691-740; the CCR store ccr/mod.rs; the Python mirror + compression_store). Design so: (1) the output still signals what was dropped (count + addressable pointers) so the model knows it can retrieve; (2) retrieve(chunk_id) returns just that chunk; (3) the recovery invariant still holds 100% (every dropped item recoverable); (4) parity + canonical hash + default lossless behavior unchanged; (5) the existing single-blob retrieve path stays working or is cleanly superseded with tests updated. Add a benchmark/test measuring effective-savings-under-retrieval at {0,25,50}% with the NEW granular model and show it no longer goes negative.

${GUARD}

Return the structured report: frontier, what_changed (file:line), files, before_after (effective-savings under retrieval before vs after, real numbers), contracts_intact, guardrails (verbatim), honest_caveats, commit.`,
  { label: 'granular-ccr', phase: 'GranularCCR', schema: REPORT }
)
log(`GranularCCR: contracts=${ccr?.contracts_intact}, commit=${ccr?.commit || '?'}`)

// ---- Frontier B: honest verification + benchmarks (runs after A so it measures the fixed engine) ----
phase('HonestVerify')
const honest = await agent(
`You are making the verification + the published numbers HONEST, on the engine as just improved by the granular-CCR change.

${CONTEXT}

THE WEAKNESSES (verify/heldout/REPORT.md auditor cheats_found): (1) the lossless check has a lenient _present_in_text scalar-substring fallback that can mark an item "reconstructed" without the real decoder/CCR round-trip — make the STRICT reconstruction (decode_csv_schema_rows + CCR retrieve + sha256, no substring fallback; see verify/heldout/strict_recheck.py) the DEFAULT measurement in verify/measure.py (and verify/heldout/measure.py). (2) the effective-savings model charged a PROPORTIONAL slice of the offloaded blob, but real retrieval was whole-blob — update it to the REAL cost model now that Frontier A made retrieval granular (charge per actually-retrieved chunk). (3) the held-out numbers showed the dev headline percentages are CEILINGS not typical (logs 93%->82% high / 80% genuine; disk 50%->40-44%; multiturn 70.8%->28-39% at realistic entropy/size).

YOUR JOB: (a) make the verify harnesses strict-by-default (remove/neutralize the scalar fallback so a non-round-tripping item FAILS, re-run, confirm still 0 hash failures); (b) fix the effective-savings model to the realistic granular-retrieval cost; (c) REWRITE BENCHMARKS.md to be honest + tier-aware: report TYPICAL (medium/realistic-entropy) AND CEILING (low-entropy) AND the genuine-entropy/near-unique numbers AND effective-savings-under-retrieval, citing verify/ + verify/heldout/. No inflated single numbers — every claim tier-qualified + sourced. Do NOT touch the engine (Frontier A owns engine changes); this is verification + docs only, plus re-running the harnesses.

${GUARD}

Return: frontier, what_changed (file:line), files, before_after (what the strict/honest numbers are vs the old inflated ones), contracts_intact, guardrails (verbatim), honest_caveats, commit.`,
  { label: 'honest-verify', phase: 'HonestVerify', schema: REPORT }
)
log(`HonestVerify: contracts=${honest?.contracts_intact}, commit=${honest?.commit || '?'}`)

return { granularCCR: ccr, honestVerify: honest }
