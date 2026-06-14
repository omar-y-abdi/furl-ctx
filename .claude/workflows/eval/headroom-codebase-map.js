// headroom-codebase-map — Phase-0 recon. ~12 parallel READ-ONLY Explore agents, one per
// subsystem, each returns a structured section (files, their role, key symbols + file:line,
// gotchas). A synthesis agent merges them into ONE tight navigation map optimized for an
// agent that will MODIFY or ATTACK the engine: "where to change X", build/bench cheatsheet,
// contract-enforcement sites. Run ONCE; the map_markdown is fed (inline) into every
// headroom-parallel-eval agent so the 140+ experiment agents navigate precisely instead of
// each re-exploring the same files.
//
// RUN: Workflow({ name: 'headroom-codebase-map', args: { repo, areas? } })

export const meta = {
  name: 'headroom-codebase-map',
  description: 'Read-only recon: parallel subsystem mappers -> one tight navigation map (where everything is + what it does + key file:line + build/bench) to prime downstream eval agents.',
  whenToUse: 'Before a large multi-agent eval/refactor of the Headroom engine, so experiment agents navigate by a precomputed map instead of each re-exploring.',
  phases: [
    { title: 'Map', detail: 'parallel read-only Explore agents, one per subsystem' },
    { title: 'Synthesize', detail: 'merge into one tight navigation map + change index + build/bench cheatsheet' },
  ],
}

const A = (typeof args === 'string') ? JSON.parse(args) : (args || {})
const REPO = A.repo || '/Users/k/dev/headroom'

// 12 subsystems (fits the user's "10-16 agents"). Each agent owns one, follows imports outward.
const DEFAULT_AREAS = [
  { name: 'rust-crusher-core', focus: 'the keep/drop decision, CCR sentinel emission, granular per-row offload, RoutingPolicy=MinTokens render selection', paths: 'crates/headroom-core/src/transforms/smart_crusher/crusher.rs, config.rs, orchestration.rs' },
  { name: 'rust-compaction', focus: 'lossless columnar encodings (constant-fold, ditto, affix-fold, head-dict, ISO-delta, arithmetic-progression, decimal-scale-fold, dictionary) + CSV-schema formatter', paths: 'crates/headroom-core/src/transforms/smart_crusher/compaction/compactor.rs, encodings.rs, formatter.rs' },
  { name: 'rust-ccr', focus: 'CCR store (InMemory DashMap + sqlite), TTL, canonical hashing, per-row chunk index ({hash}#rows), ccr_get surface', paths: 'crates/headroom-core/src/transforms/.../ccr, ccr/mod.rs (rg for ccr)' },
  { name: 'rust-other-transforms', focus: 'code/log/search/diff/kompress compressors + compression_units/summary/policy on the rust side', paths: 'crates/headroom-core/src/transforms/ (everything except smart_crusher)' },
  { name: 'rust-routing-tokenizer', focus: 'content_router (rust), tokenizer registry, relevance scoring, cache-prefix / cache_control handling', paths: 'crates/headroom-core/src/ (router, tokenizer, cache, scoring modules)' },
  { name: 'rust-libtree-api', focus: 'lib.rs, module tree, the public Rust API surface + error types, anything not yet covered', paths: 'crates/headroom-core/src/lib.rs + remaining top-level modules' },
  { name: 'pyo3-bindings', focus: 'how Rust is exposed to Python: which functions/classes are bound, type conversions, the Python<->Rust parity boundary (canonical hash)', paths: 'crates/headroom-py/' },
  { name: 'py-transforms', focus: 'content_router.py, smart_crusher.py, csv_schema_decoder.py and the python compressor shims + their mirror of the rust CCR store', paths: 'headroom/transforms/' },
  { name: 'py-ccr-cache', focus: 'headroom/ccr, headroom/cache/compression_store.py (the python CCR mirror), config.py, tokenizer.py', paths: 'headroom/ccr/, headroom/cache/, headroom/config.py, headroom/tokenizer.py' },
  { name: 'py-public-api', focus: 'the public surface: compress(), CompressConfig, result object (tokens_saved/compression_ratio/messages), error types', paths: 'headroom/__init__.py, headroom/compress.py' },
  { name: 'tests', focus: 'what is covered and HOW to run it: rust tests, python tests, the recovery-invariant + proportional-retrieval + parity tests, fixtures', paths: 'crates/headroom-core/tests/, tests/' },
  { name: 'benchmarks-verify', focus: 'how compression is MEASURED: datasets, run_bench, metrics, needle_recall, the verify/ honest harnesses, DESIGN.md + BENCHMARKS.md', paths: 'benchmarks/, verify/, DESIGN.md, BENCHMARKS.md' },
]
const AREAS = A.areas || DEFAULT_AREAS

const SECTION = {
  type: 'object',
  properties: {
    area: { type: 'string' },
    summary: { type: 'string' },                       // 1-3 sentences: what this subsystem does + how it fits
    files: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          path: { type: 'string' },
          role: { type: 'string' },                    // one line: what this file is for
          key_symbols: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                name: { type: 'string' },
                location: { type: 'string' },           // file:line
                what: { type: 'string' },
              },
              required: ['name', 'location', 'what'],
            },
          },
          gotchas: { type: 'string' },                 // traps / invariants / "do not touch X without Y"
        },
        required: ['path', 'role'],
      },
    },
    change_sites: { type: 'array', items: { type: 'string' } },   // "to change <behavior> -> <file:line>"
    build_bench_notes: { type: 'string' },             // any build/run command specific to this area
  },
  required: ['area', 'summary', 'files'],
}

const MAP = {
  type: 'object',
  properties: {
    map_markdown: { type: 'string' },                  // the tight navigation map (fed inline to eval agents)
    area_count: { type: 'number' },
    file_count: { type: 'number' },
  },
  required: ['map_markdown'],
}

// ---- Phase 1: parallel read-only subsystem mapping ----
phase('Map')
const sections = (await parallel(
  AREAS.map((ar) => () => agent(
`You are mapping ONE subsystem of the Headroom compression engine so that later agents (which will MODIFY or ATTACK the engine) can navigate precisely without re-exploring. READ-ONLY — edit nothing. Scope all reads to ${REPO}.

SUBSYSTEM: "${ar.name}"
WHAT TO COVER: ${ar.focus}
START PATHS (follow imports/calls/macros outward as needed; discover siblings via rg/glob): ${ar.paths}

Read the ACTUAL code (do not infer from names). For your subsystem produce:
- summary: what it does and how it connects to the rest of the pipeline (compress() -> ... -> output + CCR store).
- files[]: each meaningful file — its role, the KEY symbols (fn/struct/class) with file:line locations and a one-line "what", and gotchas (invariants, traps, "don't change X without Y", parity/cache/CCR constraints).
- change_sites[]: concrete "to change <behavior> edit <file:line>" pointers — the entry points a modifying agent will need.
- build_bench_notes: any build/test/bench command relevant to this area.

Be DENSE and high-signal: precise file:line, no filler, no narration. This is a map, not an essay.`,
    { label: `map:${ar.name}`, phase: 'Map', schema: SECTION, agentType: 'Explore' }
  ))
)).filter(Boolean)

const fileCount = sections.reduce((n, s) => n + ((s.files && s.files.length) || 0), 0)
log(`mapped ${sections.length}/${AREAS.length} subsystems, ${fileCount} files`)

// ---- Phase 2: synthesize one tight navigation map ----
phase('Synthesize')
const synth = await agent(
`You are assembling ONE tight navigation map of the Headroom compression engine from ${sections.length} per-subsystem readings. This map will be injected INLINE into the prompt of 140+ downstream experiment/adversarial agents, so it must be DENSE, accurate, and high-signal — every token earns its place. Aim for a map a modifying agent can navigate by without opening unrelated files.

PER-SUBSYSTEM READINGS (JSON, with file:line evidence):
${JSON.stringify(sections, null, 1)}

Produce map_markdown with these sections, all grounded in the readings (do NOT invent symbols/lines not present):
1. PIPELINE — one short paragraph: end-to-end flow compress(messages) -> routing -> smart_crusher (lossless compaction + lossy drop) -> CCR offload -> output, naming the key files in order.
2. SUBSYSTEM MAP — for each subsystem: 2-6 bullet lines of "file:line — symbol — what", covering only the load-bearing symbols (the keep/drop site, CCR sentinel + granular per-row chunk store, each lossless encoding, the parity hash, routing policy, the public API).
3. CHANGE INDEX — a flat lookup: "to change/attack <behavior> -> <file:line>" for the things an optimize/break/quality agent will most need (add an encoding, change drop policy, touch CCR offload, alter routing, add a test, run a benchmark).
4. CONTRACT-ENFORCEMENT SITES — where the hard contracts live and are checked: recovery invariant, prompt-cache ordering (index0/prefix/cache_control), Python<->Rust canonical hash parity.
5. BUILD / BENCH CHEATSHEET — exact commands: build the ext, run rust tests, run pytest, run the benchmark + the restore-baseline step.

Keep it tight. Prefer a precise file:line list over prose. Return area_count, file_count, and the full map_markdown.`,
  { label: 'synthesize-map', phase: 'Synthesize', schema: MAP }
)
log(`map synthesized: ${synth ? (synth.map_markdown || '').length : 0} chars`)

return { areaCount: sections.length, fileCount, map: synth, sections }
