// headroom-parallel-eval — ONE reusable workflow, three modes (optimize | break | quality).
//
// Shape: loop-until-dry rounds of parallel sonnet agents, each running a MEASURED
// experiment in an isolated git worktree + its OWN throwaway venv (zero collision with
// the shared repo/.venv or with each other). A persistent LEDGER threads every prior
// attempt (approach_id + measured delta + verdict + why-it-failed) into the next round,
// so NO agent may repeat a tried approach: each must read the ledger, reason from prior
// failures, and justify why its NEW approach will measurably win. Rounds stop after
// DRY_LIMIT consecutive rounds with zero wins. An opus agent then reads the FULL ledger
// (wins AND failures) and emits a ranked action doc + dead-end list.
//
// Save once; run 3x with different args (mode/seeds). Cost is intentionally high — the
// brief is "optimize every part to the brim", proven by measurement, never guessed.

export const meta = {
  name: 'headroom-parallel-eval',
  description: 'Loop-until-dry fleet of isolated MEASURED experiment agents (optimize|break|quality) with an anti-repeat ledger; opus synthesizes a ranked action doc.',
  phases: [
    { title: 'Explore', detail: 'rounds of isolated measured experiments; ledger blocks repeats, forces novel + justified approaches' },
    { title: 'Synthesize', detail: 'opus reads the full ledger (wins + failures) -> ranked action doc + dead-ends' },
  ],
}

// ---- args (meta must stay a pure literal; all dynamic config read here) ----
const A = (typeof args === 'string') ? JSON.parse(args) : (args || {})
const MODE = A.mode || 'optimize'                       // optimize | break | quality
const BASELINE = A.baselineRef || 'HEAD'                // git ref the worktrees branch from
const ESCALATE_AT = A.escalateAt || A.dryLimit || 2     // N dry rounds -> fire ONE final widen-scope "last chance" round; stop only if THAT also dries up
const MAX_AGENTS = A.maxAgents || (MODE === 'break' ? 50 : 48)
const ROUND = A.roundSize || (MODE === 'break' ? 10 : 6) // <= concurrency cap (10 on 12-core); also the within-round distinct-focus count
const USE_WORKTREE = MODE !== 'break'                   // break = read-only engine runs, no worktree needed
const BASELINE_NUMBERS = A.baselineNumbers ||
  'current engine (commit under test): code@7 lossless 0.0% | logs@90 lossless 84.5% (LOSSY, drop 83.3%) | search@90 lossless 40.0% | needle-recall 100% (visible-only 72.2%). Token model gpt-4o tiktoken via the engine tokenizer.'
const CODEBASE_MAP = A.codebaseMap || ''               // precomputed navigation map (inline content) — used if no path given
const CODEBASE_MAP_PATH = A.codebaseMapPath || ''      // absolute path to CODEBASE-MAP.md; agents read it FIRST (keeps prompts small, works from worktrees via abs path)
const MAP_BLOCK = CODEBASE_MAP
  ? `PRECOMPUTED CODEBASE MAP (navigate by this — it is a guide, verify a file:line before relying on it):\n${CODEBASE_MAP}\n\n`
  : (CODEBASE_MAP_PATH
    ? `FIRST STEP — read the precomputed codebase navigation map at ${CODEBASE_MAP_PATH} before anything else. It documents where every subsystem lives, key file:line symbols, a change-index ("to change X -> file:line"), the contract-enforcement sites, and the build/bench commands. Use it to jump straight to the relevant code — do NOT re-explore what it already documents. It is a guide; verify a file:line before you rely on it.\n\n`
    : '')

const DEFAULT_SEEDS = {
  optimize: [
    'per-field / per-group / global quantization (uniform AND non-uniform) of numeric columns',
    'columnar transpose variants (row->col, struct-of-arrays, chunked/blocked transpose)',
    'entropy coding on residuals after a structural transform (rANS / zstd trained dictionary / range coding)',
    'semantic clustering of near-duplicate rows (cluster -> store centroid + per-row diff)',
    'delta-chains across messages (cross-message + intra-array reference / back-reference encoding)',
    'learned / heuristic field-splitters (auto-decompose a single varying column into stable + varying sub-fields)',
    'dictionary sharing across columns + a cross-row shared-substring dictionary',
    'numeric base/radix delta + decimal-scale fold + varint / bit-packing of integer columns',
    'template mining (extract one row template, store only the per-row fills)',
    'BWT + MTF + RLE pre-stage on sorted near-unique string columns',
    'schema inference -> typed columnar codec selected per inferred column type',
    'affix tries beyond the current affix-fold (prefix+suffix trie) for path/identifier columns',
  ],
  break: [
    'find a distinct row that does NOT round-trip byte-exactly through compress -> CCR retrieve (prove via sha256, not assert)',
    'bust prompt-cache ordering: force a drop or reorder at message index 0 or anywhere in the cached prefix',
    'make effective-savings-under-retrieval go NEGATIVE on a realistic (not contrived) input',
    'find an overfit-to-fixtures path: passes on verify/ data, fails on fresh out-of-sample data you generate',
    'unicode / encoding / control-char / emoji inputs that corrupt the <<ccr:HASH>> sentinel or the hashing',
    'a huge single field / pathological row that defeats the field-aware split and is dropped silently',
    'an adversarial near-duplicate set where dedup drops a needle WITHOUT CCR backing',
    'cache_control marker mishandling: a block that must be byte-faithful gets rewritten or moved',
    'canonical-hash instability / collision across the Python<->Rust parity boundary',
    'TTL / result-cache race that serves an UNBACKED sentinel (item neither in output nor retrievable)',
    'empty / single-item / degenerate arrays that bypass the recovery invariant',
    'mixed-type arrays where one type branch skips CCR persistence',
  ],
  quality: [
    'crusher.rs internals — cyclomatic complexity, dead branches, unsafe assumptions, hot-path allocations',
    'CCR store — correctness, unbounded memory growth, eviction policy, concurrency safety, error handling',
    'Python shims (transforms / ccr / cache) — boundary validation, error surfacing, type tightness, immutability',
    'tests — coverage gaps, missing property tests, over-mocked/flaky tests, untested invariants',
    'public API surface — ergonomics, leaky abstractions, immutability guarantees, footguns',
    'error handling — swallowed errors, panic-as-control-flow, missing domain error types',
    'performance hot paths — profile-driven, redundant work, repeated tokenizer calls, needless clones',
    'config / routing policy — clarity, undocumented behavior, default footguns',
  ],
}
const SEEDS = A.seeds || DEFAULT_SEEDS[MODE] || DEFAULT_SEEDS.optimize

// ---- schemas ----
const ATTEMPT = {
  type: 'object',
  properties: {
    approach_id: { type: 'string' },                  // short stable slug, e.g. "perfield-nonuniform-quant"
    focus: { type: 'string' },
    hypothesis: { type: 'string' },                   // why this wins, referencing prior ledger failures
    what_changed: { type: 'string' },                 // files + gist (optimize/quality) OR attack vector + input (break)
    measured_before: { type: 'string' },
    measured_after: { type: 'string' },
    delta: { type: 'number' },                        // gain in pct-points (opt/qual) or severity 0-10 (break)
    contracts_ok: { type: 'boolean' },                // recovery invariant + cache-prefix held (false = it broke them)
    verdict: { type: 'string', enum: ['improved', 'no_change', 'regressed', 'build_failed', 'defect_found', 'rejected'] },
    why: { type: 'string' },                          // root-cause reasoning: why it worked / why it failed
    novel: { type: 'boolean' },                       // agent asserts distinct from ledger
    repro: { type: 'string' },                         // minimal repro (esp. break mode)
  },
  required: ['approach_id', 'focus', 'verdict', 'why', 'delta', 'contracts_ok'],
}

const SYNTH = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    top_recommendation: { type: 'string' },
    ranked: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          rank: { type: 'number' }, title: { type: 'string' }, approach_id: { type: 'string' },
          evidence: { type: 'string' }, expected_gain: { type: 'string' }, risk: { type: 'string' },
          effort: { type: 'string' }, build_steps: { type: 'string' },
        },
        required: ['rank', 'title', 'evidence', 'expected_gain', 'risk'],
      },
    },
    dead_ends: {
      type: 'array',
      items: {
        type: 'object',
        properties: { approach_id: { type: 'string' }, why_avoid: { type: 'string' } },
        required: ['approach_id', 'why_avoid'],
      },
    },
    doc_markdown: { type: 'string' },                 // the full action doc, written to disk by the orchestrator
  },
  required: ['summary', 'top_recommendation', 'ranked', 'doc_markdown'],
}

// ---- shared prompt fragments ----
const CONTEXT = `${MAP_BLOCK}PROJECT: Headroom — a forked LLM-context compression engine. Rust core (crates/headroom-core, pyo3 crates/headroom-py) + thin Python (headroom/transforms, ccr, cache). Public API: \`from headroom import compress, CompressConfig\` -> \`result = compress(messages)\` -> result.messages / result.tokens_saved / result.compression_ratio. Lossy drops are made recoverable via a \`<<ccr:HASH>>\` sentinel surfaced in the output + the original held in the CCR store. Lossless encodings: constant-fold, ditto, affix-fold, head-dict, ISO-delta, arithmetic-progression, decimal-scale-fold, dictionary. RoutingPolicy=MinTokens default. CCR offload is now GRANULAR per-row.

BASELINE (the commit your worktree is on): ${BASELINE_NUMBERS}

HARD CONTRACTS — breaking ANY makes the attempt a FAILURE (report contracts_ok=false, do not hide it):
 1. CCR recovery invariant: every distinct dropped/substituted item on the public compress() path stays 100% recoverable (sentinel in output + original retrievable). 0 silent loss.
 2. Prompt-cache ordering: never drop message index 0, never reorder the cached prefix, never rewrite cache_control. Byte-faithful passthrough of anything not compressed.
 3. Python<->Rust parity: canonical item hash byte-stable across both sides.
 4. No overfitting to verify/ fixtures. Gains must hold on fresh out-of-sample data.`

const GUARD = `VERIFY (run in YOUR worktree — its files are yours; NEVER write to the shared /Users/k/dev/headroom/.venv). Setup (deps come from the shared pip cache — fast; do NOT upgrade pip, do NOT --system-site-packages [it does not inherit .venv deps here]):
 1. \`python3 -m venv .venv-eval && .venv-eval/bin/pip install -q maturin\`
 2. Build ONLY the Rust ext via the SHARED sccache so unchanged crates are CACHE HITS (only YOUR changed crate recompiles): \`export RUSTC_WRAPPER=sccache SCCACHE_DIR=/Users/k/dev/headroom/.sccache CARGO_INCREMENTAL=0 && .venv-eval/bin/maturin develop\` (must be GREEN). CARGO_INCREMENTAL=0 is REQUIRED or sccache will not cache.
 3. FAIL-LOUD precedence gate: \`.venv-eval/bin/python -c "import headroom,inspect,os;print(os.path.realpath(inspect.getfile(headroom)))"\` — if this path is NOT under YOUR worktree (e.g. it points at /Users/k/dev/headroom/headroom), STOP and return verdict="build_failed", why="import precedence wrong — measuring shared baseline, not my change". Do NOT measure; a number from the wrong code is worse than none.
Recovery invariant: \`.venv-eval/bin/python -m pytest tests/test_ccr_recovery_invariant.py -q\` (all pass). Measure: \`.venv-eval/bin/python -m benchmarks.run_bench\` (note numbers; needle-recall must stay 100%). If a change breaks the build or a contract, that attempt is a FAILURE — report it honestly with the root cause; do NOT weaken a test or a type to make it pass. Do NOT commit, push, merge, or rebase — the worktree is throwaway and is discarded.`

// Build a compact ledger digest the next round must respect.
const digestOf = (entries) => {
  if (!entries.length) return '(empty — you are the first round; pick the highest-leverage approach for your focus.)'
  return entries.map((e, i) =>
    `#${i + 1} [${e.approach_id}] focus="${e.focus}" verdict=${e.verdict} delta=${e.delta} contracts_ok=${e.contracts_ok} :: ${String(e.why || '').slice(0, 240)}`
  ).join('\n')
}

const ANTIREPEAT = (ledgerDigest) => `LEDGER OF ALL PRIOR ATTEMPTS (every agent before you, across every round — wins AND failures):
${ledgerDigest}

ABSOLUTE RULES (the user's #1 requirement):
 - Do NOT repeat any approach_id above, nor a trivial variant of one. "Same change, hoping for a different outcome" is forbidden.
 - READ the failures. For every related prior attempt, understand WHAT they changed, WHY it failed, and what they got wrong.
 - Your hypothesis MUST explain, with reasoning grounded in those prior measured results, why YOUR approach is genuinely different and why THIS time it will measurably win. No guesswork — triage, reason, then act.
 - If, after studying the ledger, you find no genuinely novel + promising approach for your focus, return verdict="rejected" with why (do NOT fabricate a marginal repeat).`

// Injected ONLY on the final widen-scope round after the track has stalled.
const ESCALATION = `### FINAL WIDEN-SCOPE ROUND — LAST CHANCE, MAKE IT COUNT
The last ${ESCALATE_AT} rounds on this track produced ZERO measurable wins: incremental attempts have stalled or regressed. This is the final round before this track stops. So:
 - Do NOT submit another small tweak. Step back and think DIFFERENTLY.
 - You MAY abandon your single seed focus: COMBINE techniques across the ledger, invent an approach NOT in the seed list, or attack the problem from first principles.
 - Mine the ledger's failures hard — the obvious paths are spent; find the non-obvious one and justify why it breaks the plateau where the others failed.
 - Bold + well-reasoned + MEASURED beats a safe repeat. But if even this finds nothing real, report verdict="rejected" honestly — do NOT fake a marginal gain to look busy.`

// ---- worktree experiment prompt (optimize / quality) ----
const experimentPrompt = (focus, idx, ledgerDigest, escalated) => `You are experiment agent #${idx} in a "${MODE}" fleet. You are in a FRESH, ISOLATED git worktree at the baseline commit. Work only here. Other agents work in their own worktrees in parallel — you cannot collide.

TWO COPIES EXIST — CRITICAL: \`/Users/k/dev/headroom\` is the SHARED READ-ONLY baseline every other agent depends on; NEVER Edit/Write/build/install there. Your worktree (your cwd — confirm with \`pwd\` first) is the ONLY place you may edit. The codebase map below uses repo-RELATIVE paths (e.g. \`crusher.rs:1160\`, \`headroom/transforms/...\`): resolve EVERY one inside YOUR worktree, never against /Users/k/dev/headroom. Before each Edit/Write, verify the target is under your worktree. Editing the shared baseline corrupts every agent's measurement — the worst possible failure.

${escalated ? ESCALATION + '\n\n' : ''}${CONTEXT}

YOUR FOCUS AREA: ${focus}

${ANTIREPEAT(ledgerDigest)}

YOUR JOB (MEASURED, not guessed):
 1. Set up your isolated venv and build the baseline (see VERIFY). Confirm the baseline benchmark numbers match the stated baseline (they are your "before").
 2. ${MODE === 'optimize'
    ? 'Design and IMPLEMENT one concrete, novel compression improvement for your focus area in the Rust core and/or Python transforms. Aim to raise the LOSSLESS token-reduction (or convert lossy drops into lossless gains) on the real benchmark datasets WITHOUT breaking any contract.'
    : 'Design and IMPLEMENT one concrete, novel code-quality/robustness/performance improvement for your focus area. Measure its impact (build still green, full suite still green, and a measured perf or complexity or coverage delta).'}
 3. Rebuild, re-run the recovery invariant + the benchmark (+ \`.venv-eval/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --continue-on-collection-errors --timeout=120\` for quality mode). Capture before/after numbers.
 4. Verify every HARD CONTRACT still holds. If your change breaks one, the attempt FAILED — report contracts_ok=false honestly.

${GUARD}

Return the structured attempt: approach_id (short slug), focus, hypothesis (why novel + why it should win, citing prior ledger failures), what_changed (files + gist), measured_before, measured_after, delta (pct-points gained; negative if it regressed), contracts_ok, verdict (improved only if a real measured gain with all contracts intact), why (root-cause: why it worked or exactly why it failed — this teaches the next round), novel (true + justified), repro (the key diff or command).`

// ---- adversarial prompt (break) — no worktree, read-only engine ----
const breakPrompt = (focus, idx, ledgerDigest, escalated) => `You are adversarial agent #${idx} in a "break" fleet. Your job is to BREAK the current engine, not confirm it. Assume the reported numbers are inflated and the invariants are violable. Use the already-built shared engine read-only: \`from headroom import compress, CompressConfig\`. Do NOT modify ANY repository file. Write any scratch / generated test data / probe scripts ONLY under /tmp (e.g. \`/tmp/break-${idx}-...\`), NEVER under any repo path — every break agent shares the one live repo, so a stray write corrupts siblings mid-run.

${escalated ? ESCALATION + '\n\n' : ''}${CONTEXT}

YOUR ATTACK VECTOR: ${focus}

${ANTIREPEAT(ledgerDigest)}

YOUR JOB:
 1. Generate FRESH adversarial input yourself (never reuse verify/ fixtures — that would be a false positive/negative). Make it realistic where the vector calls for realism.
 2. Drive the engine and test the specific failure: for round-trip, retrieve every dropped item from the CCR store and compare sha256 to the original (prove with hashes, never \`assert\`). For savings, compute effective tokens INCLUDING retrieval. For cache ordering, inspect message index 0 / the cached prefix / cache_control byte-for-byte.
 3. Reduce any confirmed defect to a MINIMAL repro (smallest input + exact commands).

Return the structured attempt: approach_id, focus, hypothesis, what_changed (the attack + input description), measured_before/after (the observed vs expected behavior), delta (severity 0-10), contracts_ok (false if you broke a contract), verdict ("defect_found" only with a reproducible defect; else "no_change"/"rejected" with why), why (root cause if found, or why the engine held), novel (true + justified), repro (minimal input + commands to reproduce).`

const promptFor = (focus, idx, ledgerDigest, escalated) =>
  USE_WORKTREE ? experimentPrompt(focus, idx, ledgerDigest, escalated) : breakPrompt(focus, idx, ledgerDigest, escalated)

const isWin = (e) => e && (e.verdict === 'improved' || e.verdict === 'defect_found')

// ---- Explore: loop-until-dry ----
phase('Explore')
const ledger = []
let spawned = 0, round = 0, dryRounds = 0, stop = false

// Stop is NEVER a silent give-up: after ESCALATE_AT dry rounds we fire ONE final
// widen-scope "last chance" round (agents told to think differently / combine /
// go off-seed). We stop only if THAT round also yields nothing. A win at any point
// (incl. the escalated round) resets the counter and normal exploration resumes.
while (!stop && spawned < MAX_AGENTS && (!budget.total || budget.remaining() > 80_000)) {
  round++
  const escalated = dryRounds >= ESCALATE_AT
  const digest = digestOf(ledger)
  const batch = []
  for (let i = 0; i < ROUND && spawned < MAX_AGENTS; i++) {
    const focus = SEEDS[spawned % SEEDS.length]          // distinct focus per agent within a round (ROUND <= SEEDS.length)
    batch.push({ focus, idx: spawned })
    spawned++
  }
  const results = await parallel(batch.map((b) => () =>
    agent(promptFor(b.focus, b.idx, digest, escalated), {
      label: `${MODE}:${b.idx}${escalated ? ':LAST' : ''}:${b.focus.slice(0, 24)}`,
      phase: 'Explore',
      model: 'sonnet',
      ...(USE_WORKTREE ? { isolation: 'worktree' } : {}),
      schema: ATTEMPT,
    })
  ))
  const fresh = results.filter(Boolean)
  ledger.push(...fresh)
  const wins = fresh.filter(isWin)
  if (wins.length === 0) {
    dryRounds++
    if (escalated) stop = true                           // even the widen-scope last-chance round found nothing -> done
  } else {
    dryRounds = 0                                        // progress (incl. an escalated breakthrough) -> keep going
  }
  log(`round ${round}${escalated ? ' [ESCALATED last-chance]' : ''}: ${fresh.length}/${batch.length} returned, ${wins.length} wins, dryRounds=${dryRounds}, spawned=${spawned}/${MAX_AGENTS}`)
}

log(`Explore done: ${ledger.length} attempts over ${round} rounds; ${ledger.filter(isWin).length} wins.`)

// ---- Synthesize: opus reads the FULL ledger ----
phase('Synthesize')
const synth = await agent(
`You are the synthesis lead (opus). A fleet of "${MODE}" agents ran ${ledger.length} MEASURED experiments over ${round} rounds against the Headroom compression engine. Below is the FULL ledger — every attempt, wins AND failures, each with measured deltas and root-cause reasoning.

${CONTEXT}

FULL LEDGER (JSON):
${JSON.stringify(ledger, null, 1)}

YOUR JOB: produce a ranked, evidence-backed ACTION DOC telling the implementer exactly what to build next. Rules:
 - Rank only approaches with REAL measured support (cite the numbers from the ledger). ${MODE === 'break' ? 'For break mode, rank confirmed defects by severity with repro + fix direction.' : 'Rank by (measured gain × confidence) ÷ effort.'}
 - Every item: title, the supporting approach_id(s), the measured evidence, expected gain, risk, effort, and concrete build_steps.
 - List DEAD ENDS: approaches that were tried and measurably failed, with why — so the implementer never wastes time re-trying them.
 - Be honest: if the fleet found little, say so. No inflation. Tier every compression claim (typical vs ceiling vs near-unique) and account for retrieval cost.
 - doc_markdown: the complete action doc as Markdown (title, summary, top recommendation, ranked table, dead-ends, methodology note citing it is measured + out-of-sample).

Return the structured synthesis.`,
  { label: `synth:${MODE}`, phase: 'Synthesize', model: 'opus', schema: SYNTH }
)
log(`Synthesize done: ${synth ? (synth.ranked || []).length : 0} ranked items.`)

return { mode: MODE, rounds: round, attempts: ledger.length, wins: ledger.filter(isWin).length, ledger, synthesis: synth }
