// ---------------------------------------------------------------------------
// headroom-fullscale-review — two fable agents fully review + fix the two
// codebase sections (Rust engine, Python layer), keeping every guardrail green.
// Sequential by necessity: the two sections share one maturin build, one git
// index, and one installed .so — concurrent fix+commit would race. Rust first
// (Python tests depend on the freshly built ext), Python second.
// ---------------------------------------------------------------------------

export const meta = {
  name: 'headroom-fullscale-review',
  description: 'Two fable agents review + fix the Rust engine and the Python layer, optimizing without breaking the green build/tests/contracts.',
  phases: [
    { title: 'Rust', detail: 'fable: review + fix crates/ (engine)' },
    { title: 'Python', detail: 'fable: review + fix headroom/ + tests + benchmarks' },
  ],
}

const A = (typeof args === 'string') ? JSON.parse(args) : args
const REPO = (A && A.repo) || '/Users/k/dev/headroom'

const REPORT = {
  type: 'object',
  properties: {
    section: { type: 'string' },
    state_summary: { type: 'string' },                 // what's been done / current state of this section
    issues_found: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          location: { type: 'string' },                // file:line
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          kind: { type: 'string' },                    // bug / dead-code / inefficiency / inconsistency / error-handling / type / style
          description: { type: 'string' },
        },
        required: ['location', 'severity', 'kind', 'description'],
      },
    },
    fixes_applied: {
      type: 'array',
      items: {
        type: 'object',
        properties: { location: { type: 'string' }, what: { type: 'string' }, commit: { type: 'string' } },
        required: ['location', 'what'],
      },
    },
    deferred: {
      type: 'array',
      items: {
        type: 'object',
        properties: { location: { type: 'string' }, why: { type: 'string' } },
        required: ['location', 'why'],
      },
    },
    guardrails: { type: 'string' },                    // maturin / cargo / pytest / bench results, verbatim
    all_green: { type: 'boolean' },
  },
  required: ['section', 'state_summary', 'issues_found', 'fixes_applied', 'all_green'],
}

const CONTRACTS = `INVARIANTS YOU MUST NOT BREAK (these are intentional, verified, and recently established — do NOT revert them):
- The repo was deliberately amputated 384k->91k LOC (a forked compression platform stripped to its compression core). Do NOT re-add deleted modules or "restore" missing features. Missing proxy/memory/cli/etc. is intentional.
- CCR recovery invariant: every dropped/substituted distinct item on the public compress() path must stay recoverable via a <<ccr:HASH>> pointer surfaced in the output + the original in the CCR store. Locked by tests/test_ccr_recovery_invariant.py (17 tests) + Rust tests. Do NOT weaken it.
- compute_item_hash / the canonical CCR hash / the retrieve contract / the dict default-config output are byte-stable (Python<->Rust parity fixtures pin them). Do NOT change their output.
- ONNX (fastembed, magika) is intentionally optional/default-off (features: embeddings, magika, onnx). Default build is ML-free. Keep it that way.
- Read DESIGN.md + BENCHMARKS.md + recent git log first to understand what was done and why before changing anything.`

const GUARDRAIL = `GUARDRAIL — after your fixes, ALL must pass (paste real output in guardrails). Revert any fix that breaks one:
- cargo: \`cargo test -p headroom-core 2>&1 | grep "test result:"\` — 0 failed.
- build: \`.venv/bin/maturin develop\` — GREEN.
- python: \`.venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --continue-on-collection-errors --timeout=120\` — >=372 passed, 0 failed.
- bench: \`.venv/bin/python -m benchmarks.run_bench\` — needle-recall (output OR CCR) 100%, no lossless-ratio regression; then \`git checkout HEAD -- benchmarks/baseline_results.json benchmarks/BASELINE.md\` (run_bench overwrites the baseline file — restore it).
- Commit fixes in small descriptive commits (no Co-Authored-By). A routing-gate hook may require reading ./.claude/workflow/DEFAULT_WORKFLOW.md (cwd-relative, it exists) before Write/Bash — read it first if blocked.`

const POLICY = `FIX POLICY: fix HIGH-CONFIDENCE issues only — real bugs, dead code your section owns, clear inefficiencies, inconsistencies, missing/wrong error handling, type-safety gaps, and obvious correctness/optimization wins. For anything risky, uncertain, or that would change a public contract or behavior, DEFER it (report in deferred[], do not change). No speculative rewrites, no new abstractions, no scope creep. Match existing conventions. No stubs/TODOs/placeholders. Immutable patterns; small focused functions.`

// ---- Section A: Rust engine ----
phase('Rust')
const rust = await agent(
`You are a senior Rust reviewer doing a full-scale review + fix of the RUST ENGINE section of a lean LLM-context compression library, to optimize it to work better.

REPO: ${REPO}  (x86_64 venv at .venv; \`.venv/bin/maturin develop\` rebuilds the pyo3 ext; \`cargo\` for the workspace)
YOUR SECTION (review + fix ONLY these): \`crates/headroom-core/\`, \`crates/headroom-py/\`, root \`Cargo.toml\`, \`Cargo.lock\`. Do NOT touch \`headroom/\`, \`tests/\`, \`benchmarks/\` (the other agent owns those).

${CONTRACTS}

DO A FULL REVIEW: read the crate thoroughly (lib.rs, transforms/smart_crusher/*, relevance/*, ccr/*, tokenizer/*, signals/*, compaction/*). Find: correctness bugs, panics/unwraps on reachable paths, dead code, needless clones/allocations, inefficient hot paths, error-handling gaps, clippy issues, inconsistencies, and concrete optimization opportunities. Then FIX the high-confidence ones.

${POLICY}

${GUARDRAIL}

Return the structured report: state_summary (what the Rust engine currently is + what's been done to it), issues_found (prioritized, file:line), fixes_applied (+ commit hashes), deferred (risky/uncertain), guardrails (verbatim results), all_green.`,
  { model: 'fable', label: 'rust-review-fix', phase: 'Rust', schema: REPORT }
)
log(`Rust: ${rust?.fixes_applied?.length || 0} fixes, ${rust?.issues_found?.length || 0} issues, all_green=${rust?.all_green}`)

// ---- Section B: Python layer (runs AFTER Rust so it builds on the fixed ext) ----
phase('Python')
const python = await agent(
`You are a senior Python reviewer doing a full-scale review + fix of the PYTHON LAYER of a lean LLM-context compression library, to optimize it to work better. The Rust engine section was just reviewed + fixed and is committed; build on top of it.

REPO: ${REPO}  (x86_64 venv at .venv; the package is editable-installed; \`from headroom import compress\` works)
YOUR SECTION (review + fix ONLY these): \`headroom/\` (transforms, ccr, cache, compress.py, tokenizer.py, config.py, pipeline.py, proxy trim, relevance, etc.), \`tests/\`, \`benchmarks/\`, \`pyproject.toml\`, \`scripts/\`. Do NOT touch \`crates/\` (the Rust agent owns those).

${CONTRACTS}

DO A FULL REVIEW: read the Python layer thoroughly (compress.py flow, transforms/*, ccr/*, cache/compression_store.py, the proxy trim, the benchmark harness). Find: correctness bugs, silent exception-swallowing, dead code / unused imports left from the amputation, inefficiencies, inconsistencies, missing input validation at boundaries, type-annotation gaps, and concrete optimization opportunities. Then FIX the high-confidence ones. Pay special attention to leftovers/rough edges from the recent amputation (stale comments, half-wired imports, dead branches referencing deleted modules).

${POLICY}

${GUARDRAIL}

Return the structured report: state_summary (what the Python layer currently is + what's been done to it), issues_found (prioritized, file:line), fixes_applied (+ commit hashes), deferred, guardrails (verbatim — the FULL pytest + bench since you run last), all_green.`,
  { model: 'fable', label: 'py-review-fix', phase: 'Python', schema: REPORT }
)
log(`Python: ${python?.fixes_applied?.length || 0} fixes, ${python?.issues_found?.length || 0} issues, all_green=${python?.all_green}`)

return { rust, python }
