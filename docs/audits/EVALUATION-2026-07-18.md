# Independent Due-Diligence Evaluation — furl-ctx

- **Date:** 2026-07-18
- **Scope:** full-repo audit answering: *"Should I and my AI agents start using this project?"*
- **Method:** fresh clone into an isolated environment; documented install paths executed verbatim; ~40 functional probes against the released PyPI 1.2.0 wheel and a from-source HEAD build; three **live Claude Code 2.1.212 sessions** with the plugin installed; benchmark reproduction; source + security review; SEO/discoverability sweep (12 search queries); alternatives research.
- **Evidence legend:** ✅ directly observed/verified · ⚠ likely (strong evidence, not conclusive) · ❓ speculation/unverified.

---

## Executive Summary

furl-ctx is a very young (repo created 2026-07-01 — ~17 days old, renamed from `furl` 6 days ago), solo-maintained, but unusually well-engineered reversible context-compression layer for AI agents, and it broadly does what it says: the two-command Claude Code plugin install works, the MCP tools deliver real 90%+ token reductions on repetitive tool output with byte-exact (text) retrieval, the security posture is thoughtful (fail-open everywhere, default-on credential redaction, jailed file reads, parameterized SQL, zero `unsafe` Rust), and the documentation is more honest about its own limits than almost any project of its size. Adopting it is low-risk in the failure sense — worst case is uncompressed passthrough — but the audit found real caveats: the released 1.2.0 wheel has a **silent false-negative `pattern` retrieval bug** (fixed at HEAD, unreleased); the shipped hooks run under `sh -lc`, so **any noisy login-shell profile silently breaks both automatic compression paths** (verified live) while counters still report success; docs across PyPI/README/plugin/hook-note **contradict each other about whether auto-compression works today** (live testing shows it *does* work on a clean machine — the docs under-claim); savings honestly approach **0% on high-entropy content** and file reads are excluded by design; the project currently has effectively **zero adoption and zero search visibility**; and its comparison table omits the elephant in the room — **Headroom, the ~59.7K-star, company-backed upstream it forked, which is also local and reversible with a superset of features**. Recommendation: **Yes, with caveats** — adopt the MCP tools/library for log- and JSON-heavy agent workflows if you value furl's specific niche (deterministic, ML-free, proxy-free, natively plugin-installed, auditably minimal); verify hook delivery once with its own counters; pin the version; evaluate Headroom alongside it before committing; do not yet depend on it for anything security- or compliance-critical.

---

## First Impression

*(Answered from the GitHub landing page only, before deep reading.)*

**Immediate understanding** ✅ — The repo communicates its purpose within a minute: "Context compression for AI agents — cut token usage costs with retrievable CCR compression. Rust core + Python API, Claude Code & Codex plugin, MCP server." Tool outputs get compressed before entering the context window; originals stay retrievable. Target audience: Claude Code / agent developers. An engineer could re-explain it after 60 seconds.

**Immediate confusion** ✅
- "**CCR compression**" is used in the tagline and README without expansion. The expansion (Compress-Cache-Retrieve) appears only in `llms.txt`/`LIBRARY.md` — a first-time visitor sees an unexplained acronym.
- The headline "**0–54% token savings** on real high-entropy content · reaching 95% on repetitive logs/fixtures" is honest but initially confusing — a headline that includes 0% as an outcome makes a visitor stop and re-read (that turns out to be deliberate honesty, but it costs clarity).
- "**Claude Code & Codex plugin**" — the Codex half of that claim has no corresponding artifact anywhere in the repo (see Bugs).

**Suspicious at first glance** ✅ — 2 stars, 0 forks, contributor graph failing to load; 9 releases within days of each other; a known-issue banner admitting the flagship auto-compression is "pending an upstream Claude Code fix." Also one tonal wobble: "By using Furl you'll never need to touch grass again" sits oddly against otherwise rigorous engineering prose, and several sentences read as non-native constructions ("Resulting in decreased input token usage while the answer always staying the same.").

**Confidence-inspiring** ✅ — Apache-2.0, SECURITY.md, CONTRIBUTING.md, BENCHMARKS.md with committed input data, a "best-case ceilings vs honest read" framing right in the README, a link to the upstream bug rather than hiding it, website + topics + llms.txt.

**Marketing clarity / professionalism** — High for a solo project; the honesty-first framing is genuinely distinctive. Visual presentation is plain ASCII-art README, no demo gif (one was deliberately removed in PR #89).

---

## Discoverability & SEO

*(12-query web sweep + site/PyPI inspection, run 2026-07-18.)*

**Search visibility: effectively zero.** ✅
- 0/12 relevant queries surfaced furl-ctx anywhere in results: `"furl-ctx"` (exact), `furl context compression`, `context compression AI agents`, `context compression tool LLM`, `context management Claude Code`, `token compression tool`, `prompt compression tool`, `token optimization LLM agents`, `AI context window management tool`, `LLM context memory tool`, `context pruning LLM`, `Claude Code plugin compress context`.
- `site:furl-ctx.vercel.app` → **0 pages indexed**. Exactly **one** indexed page on the web mentions furl-ctx (`Chat2AnyLLM/awesome-claude-skills`).
- This is an **indexing problem, not just ranking**: the project is ~11 days old under this name; crawlers have essentially not arrived. A Google Search Console verification file was added (PRs #91/#92) ✅, so the author is already working on it.

**AI discoverability** — A root `llms.txt` and `site/llms.txt` exist with accurate, quotable summaries ✅ (genuinely forward-looking). A `glama.json` (MCP directory metadata) is present ✅. But with one indexed mention on the web, ChatGPT Search / Gemini / AI Overviews have almost nothing to retrieve from ⚠.

**Name collision: the single biggest SEO liability.** ✅ The word "furl" is fully owned by others: `gruns/furl` (the established Python URL library — the README itself must warn "Do not run `pip install furl`"), furl.ai (funded security startup), Perl's Furl, and even crochet hooks. Searching the exact string `"furl-ctx"` today returns only those. The hyphenated name is winnable (no competition) but underused — the project brands itself as bare "Furl" in most surfaces.

**Concrete defects found** ✅
- **PyPI project links (Homepage/Repository/Issues) still point to the old repo name** `omar-y-abdi/furl` (works via redirect, but non-canonical, splits link equity; the released wheel's metadata predates the rename).
- The PyPI 1.2.0 README opens with "Furl compresses everything your Claude Code agent reads" — a claim the current repo README explicitly retracted (Read/Grep/Glob are *not* touched, by design). Stale metadata actively contradicts current positioning.
- The website hero claims "**91.5% fewer tokens** across six real captures" while the README leads with "**0–54%**". BENCHMARKS.md §"The three public headline numbers" does reconcile them carefully ✅, but a visitor comparing site and README sees contradictory headlines before finding the reconciliation.
- The site is server-rendered with clean headings (crawler-friendly ✅) but has no PyPI link, and meta/OG/JSON-LD could not be confirmed ❓.

**Suggested SEO improvements** (details in Improvements): fix PyPI URLs on next release; submit site to GSC/Bing and request indexing; standardize on the "furl-ctx" string; close the GitHub↔site↔PyPI link triangle; pursue listings where category peers already rank (mcpmarket, awesome-mcp-servers, awesome-claude-code); one substantive technical writeup for backlinks; make sure the site body uses the phrases people actually search ("context pruning", "prompt compression", "token optimization").

---

## Trust Assessment

Signals inspected, and the direction each moves trust:

| Signal | Observation | Effect |
|---|---|---|
| License | Apache-2.0, proper NOTICE with per-dependency licenses, cargo-deny allow-list ✅ | ↑ |
| Provenance | Openly declared **hard fork of Headroom**'s engine ("about a third of the engine still has traces"), correct Apache NOTICE attribution, provenance corrected in a dedicated PR (#109) the day before this audit ✅ | ↑ (disclosure), ⚠ (engine partly inherited, not fully home-grown) |
| Releases | 9 releases; v1.0.0→v1.2.0 within 3 days (Jul 10–12); release-please automation; detailed, self-critical release notes ✅ | ↑ quality, ↓ stability (API churn; README itself says "pin a minor version") |
| Release lag | Plugin 1.3.1 ships from HEAD while the pinned engine is 1.2.0; an open release PR (#87) has been pending since Jul 13 with real fixes (pattern-filter fix, type-error fail-fast) stuck behind it ✅ | ↓ (users get known-fixed bugs) |
| Issues | **Zero issues ever filed** ✅; README says "open a GitHub issue (the surest way to reach the maintainer)"; SECURITY.md points at "the repository issues" for hash-pinning progress — a pointer into an empty tracker | ↓ (no community signal; nobody but the author has exercised the support path) |
| PRs | 109+ PRs; ~30 in the last 6 days; mix of author, dependabot, release-please, and AI agents (⚡Bolt, Jules, "lazy-dev" sweeps — mostly left unmerged); previous external audit PR #105 **closed without merge**, but a follow-up (#107) merged its findings ✅ | ↑ activity; ⚠ heavy AI-generated inflow is filtered but the backlog of 9 open PRs is bot noise |
| CI | 1,279 workflow runs in ~1 month; 8 workflows (ci, rust, release, publish, pr-health, devcontainers, stale, release-please); recent runs green; 4-shard pytest matrix, ruff, mypy, commitlint, `claude plugin validate --strict` ✅ | ↑↑ |
| Tests | Claim (v1.2.0 notes): 2150 passed. Verified at HEAD: **2,409 passed / 16 skipped / 1 failed in 60s** — the single failure being a real environment-sensitive defect (login-shell noise; see Bugs), not a broken suite ✅ | ↑ (suite is real and current), ⚠ (the failure is a genuine live defect their CI can't see) |
| Commits | 505 commits, 481 by the author; 53/60 recent commits conventional-format; Keep-a-Changelog maintained by automation ✅ | ↑ |
| Maintainer | Solo (self-disclosed in README ✅ — "one person handles issues, PRs, and security reports") | ↓ bus-factor, ↑ honesty |
| Security practices | SECURITY.md with private-advisory reporting, supported-versions table, explicit at-rest threat model, supply-chain honesty ("version-pinned, NOT hash-pinned", floating transitives), GitGuardian config, dependabot, `deny.toml` ✅ | ↑↑ |
| Dependencies | Runtime: only `tiktoken` (+optional mcp/re2/tree-sitter). Rust: mainstream crates (serde, regex, pyo3, flate2…) ✅ | ↑ |
| Docs quality | Extensive (README/LIBRARY/BENCHMARKS/SECURITY/RUST_DEV/CODEBASE-MAP/CCR-RETENTION ≈ 180KB), but **drifting across surfaces** (see Bugs #6) | ↑ depth, ↓ consistency |
| Adoption | 2 stars, 0 forks, 0 dependents found ✅ | ↓ (unproven in the wild; you will be an early adopter) |

**Confidence rating: moderate-high on engineering integrity, low on ecosystem maturity.** The codebase gives every signal of a builder who takes correctness and honesty seriously (self-labeled "honest-docs corrections" in release notes; committed adversarial benchmark audits against the project's own claims). The risks are youth, churn, single maintainer, and zero external validation — not sloppiness.

---

## Installation Experience

All installs performed in an isolated sandbox (`HOME` redirected), following documentation exactly.

**Path 1 — PyPI wheel (`pip install furl-ctx`)** ✅
- Fresh venv → install completed in **3.8 s**, zero warnings (besides pip's own upgrade notice), `import furl_ctx` OK, version 1.2.0. `pip install "furl-ctx[all]"` (the LIBRARY.md-recommended form) equally clean.
- No Rust toolchain needed (prebuilt manylinux wheel) ✅ as documented. **Windows has no wheel** — documented honestly with a rustup fallback ⚠ (not tested here).
- Time to first successful `compress()`: ~2 minutes including reading the snippet. **Zero manual interventions, zero guesses.**

**Path 2 — Claude Code plugin (the headline "two commands")** ✅
- `claude plugin marketplace add omar-y-abdi/furl-ctx` → clone + validation succeeded.
- `claude plugin install furl@furl` → installed (user scope). `claude plugin validate --strict` on the marketplace manifest: passes.
- The plugin self-bootstraps the engine via `uv run --with 'furl-ctx[mcp]==1.2.0'`: the **first** piped/hooked call pays a visible multi-second download (uv resolving pydantic-core/tiktoken/furl-ctx to stderr) ✅ — documented ("seconds to tens of seconds"), warm calls ~0.13 s measured.
- Prerequisite `uv` was present; on a machine without `uv` the plugin would fail-open to raw output ⚠ (not tested).

**Path 3 — From source (`pip install -e ".[dev,mcp]"`)** ✅
- One command; maturin compiled the Rust core; **81 s** total; import OK. `pytest` runs against it directly per CONTRIBUTING.

**Friction found** (all minor): the docs' `uv run --no-project --with 'furl-ctx[mcp]' furl ...` CLI invocation is verbose for exploratory use; the plugin/engine **version split** (plugin 1.3.1 vs engine 1.2.0) is explained only in a README footnote; `pip install furl` (the natural guess) installs an unrelated URL library — the README warns about this, but the trap remains one typo away.

---

## Functional Testing

~40 probes across library, CLI, MCP server, hook scripts, and live Claude Code sessions. Payloads: repetitive JSON arrays (with a planted anomaly "needle"), templated logs, high-entropy random text, unicode (emoji + zero-width), binary, empty, 1.2 MB JSON, 5 MB single line.

**Verified working as documented** ✅
- `compress()` API: `CompressResult` with `messages/tokens_before/tokens_after/tokens_saved/compression_ratio/ccr_hashes/transforms_applied/warnings/error`; first call 1.9 s (docs say ~6 s — better), subsequent 0.01–0.03 s; deterministic across repeated runs; caller's input never mutated.
- Compression quality on the happy path: 300-row repetitive JSON → 28,695 chars → **252 chars**; templated logs → 91.3% char reduction with **byte-exact** text retrieval; 1.2 MB JSON compressed in 0.65 s.
- The planted anomaly (1 FATAL row in 300 ok rows) was **kept visible in the compressed view** and independently recoverable via `select_field/select_equals` — better than the docs promise (they only guarantee pull-based recovery).
- JSON-array retrieval returns a semantically-complete re-serialization (documented; parsed-equal to the original, not byte-equal) ✅; raw-text retrieval is byte-exact ✅; emoji and zero-width characters survive ✅.
- `retrieve` filter contracts: incompatible mixes, invalid regex, reversed/zero line ranges, negative limits → loud `ValueError`s with excellent messages; store miss → `None` (library) / rich explanatory error text (MCP/CLI) ✅.
- `purge()` → `True` then `False`; retrieval after purge → `None` ✅. `resolve_markers()` bulk-expands ✅. TTL expiry works (2 s TTL honored, loud miss after) ✅.
- Redaction: raising redactor **aborts compress() (fail-closed)** ✅; scrubbing redactor removes secrets from both view and store ✅.
- CLI: stdin/file compress, `--json` stats, cross-process retrieve via durable sqlite store, `doctor` diagnostics, helpful miss messages naming store path and reasons ✅. Bogus `FURL_CCR_BACKEND` → loud startup error, no silent downgrade, original content still emitted ✅.
- MCP server: clean JSON-RPC handshake, 6 tools (+`furl_read` correctly hidden until `FURL_MCP_READ=1`), structured results with token stats and cost estimate, `furl_search` finds content across entries, `furl_list`/`furl_stats` work; 818-char agent-facing instructions block ✅.
- `furl_read` jail: absolute-outside, `../` traversal, relative traversal, and **symlink escape** all rejected ("path outside workspace") ✅.
- Hook scripts (13/13 probes): valid rewrite envelope; end-to-end execution of the rewritten command compresses stdout and **preserves exit codes exactly** (tested exit 7); small outputs pass through raw; permission-rule guard blocks rewriting when any Bash rule exists (allow/deny/ask, malformed settings → doubt → passthrough); unrelated-tool rules don't block; loop guard prevents double-wrapping; `FURL_PRETOOL_PIPE=0` disables cleanly ✅.
- **Live session (the decisive test):** in a real Claude Code 2.1.212 run with plugin 1.3.1 and a clean (non-noisy) shell, `cat` of a 19,200-char file reached the model as a **143-char compressed view** via the PostToolUse path, and as a **136-char view** via the default PreToolUse pipe (rewritten command visible in transcript) ✅. **Auto-compression demonstrably works today on a clean machine — contradicting the project's own docs, which still say it's pending an upstream fix.**
- Benchmark reproduction: 5/10 datasets byte-identical to the committed baseline; 4 drift <1.5%; needle-recall 100% (output-or-CCR) reproduced ✅.

**Failures / surprises** (full detail in Bugs Found)
- `pattern=` retrieval silently returns empty on single-line JSON offloads in released 1.2.0 (CLI, library, MCP all affected).
- Both automatic paths silently break under a noisy login-shell profile (verified live: model received all 19,200 raw chars while furl's counters recorded `hook_compressions_applied`).
- `furl compress <nonexistent>` and binary files → raw Python tracebacks.
- Released 1.2.0 `compress()` accepts strings/None/ints with only a stderr traceback + fail-open (HEAD now raises TypeError).
- Unknown `model=` values are accepted silently (tokenizer fallback, no warning in `result.warnings`).
- 5 MB single-line input → loud tokenizer-guard decline + fail-open passthrough (reasonable, but means giant minified payloads may not compress at all) ⚠.
- `benchmarks` re-run on HEAD produced 41,025→1,678 tokens on `code@7` vs the README table's 471 (99%→~96%) — within their own "counts can drift" disclaimer, but the flashiest row is stale ✅.

---

## Source Code Review

**Scale** ✅: Python 72 files / 29,123 LOC; Rust 69 files / 40,691 LOC; tests 184 files / 42,149 LOC (test:source ratio ≈ 0.6:1 across both languages). A `CODEBASE-MAP.md` (24 KB) documents layout.

**Architecture** ✅ (verified against behavior): `compress()` → redaction (fail-closed, composed: built-ins → env patterns → callback) → namespace store binding (ContextVars, reset-not-clear) → `TransformPipeline` (CrossMessageDeduper → ContentRouter; CacheAligner opt-in) → per-content-type compressors (SmartCrusher for JSON, log/diff/search compressors — implemented in the Rust core `furl_ctx._core`) → CCR store (memory/sqlite backends; entry-point-extensible) with `<<ccr:HASH>>` marker grammar owned by a single module pinned against the Rust producers. MCP server and CLI are thin surfaces over the same engine. The layering is genuinely clean and the frozen-prefix (`cache_control`) contract shows unusual care for prompt-caching interactions.

**Readability/maintainability** — Double-edged. The code is exhaustively commented with invariant reasoning and audit IDs ("COR-43", "review F3", "audit Crit-4"), which makes intent traceable, but the density is extreme (comments narrate development history at length; `pyproject.toml` alone reads like a changelog). A newcomer can follow any single file; the whole is a lot of surface for one maintainer.

**Error handling** ✅ — A consistent, explicit philosophy: *compression is best-effort and fail-open; security (redaction) is fail-closed; store selection fails loud.* Verified in behavior on every path tested. The circuit breaker (3 failures → passthrough cooldown) exists for pipeline errors.

**Testing strategy** ✅ — 2,400+ tests including adversarial/property-style suites (gate-parity between shell and Python implementations of the same predicate; plugin-manifest pins asserted against hooks.json; ReDoS budgets; proportional-retrieval cost models). One caveat observed: a full local `pytest` run **writes hook counters into the real `~/.furl`** (observed counter rows created during the suite run) ⚠ — imperfect test isolation.

**Dead weight & promises-vs-reality in the pipeline** ✅ — Two advertised components do not run by default: `CodeAwareCompressor` (1,592 LOC) is permanently off (`enable_code_aware=False` + requires the `[code]` extra — documented as opt-in, but it is a lot of shipped-dead code), and **`CacheAligner` — listed in the "How it works" architecture as "stabilizes prefixes so KV caches actually hit" — is disabled by default and, even when enabled, is detector-only (it warns, it never rewrites)**. A Rust module (`smart_crusher/anchors.rs`) is marked DEPRECATED in its own header yet still called on the active path. Docstrings carry heavy internal-ticket archaeology ("Engine P2-11", "Great-Excision Chunk 7", "COR-43"). Positive: zero TODO/FIXME/HACK markers, no orphan modules, no permanent test skips.

**Duplication — the structural debt** ✅ — The Python and Rust halves each implement: a tokenizer/estimator, a BM25 scorer, a CCR store (plus a dedicated `router_ccr_mirror` module bridging Rust-side drops into the durable Python store), a marker grammar, and even Python's `json.dumps` byte format (Rust `pyjson.rs`). All are hand-synced, guarded only by parity tests; the 1800 s TTL constant is literally hardcoded in both languages with a "matching Python's" comment. This is the codebase's biggest long-term maintenance risk for one maintainer.

**Other findings** ✅ — God-modules concentrate risk (`mcp_server.py` 2,746 LOC; `compression_store.py` 2,472; Rust `route.rs` 3,259). Control-flow invariants use runtime `assert` in several modules (`router_dispatch.py`, `mcp_server.py`, `marker_grammar.py`) — stripped under `python -O`. Durable-store TTL uses wall-clock `time.time()` while the in-memory router cache deliberately uses `time.monotonic()` (defensible asymmetry; an NTP step can mis-expire durable entries). Env-var audit: **20 `FURL_*` vars are read in library code; the LIBRARY.md table documents 15** — missing: `FURL_REDACT_BUILTINS` (security-relevant: opts out built-in credential redaction; documented only in SECURITY.md), `FURL_MAX_COMPRESS_BYTES`, `FURL_MCP_LEGEND`, `FURL_PROFILE_BANNER` (+ `FURL_CCR_NAMESPACE` prose-only). Error handling is disciplined (broad excepts log with tracebacks or carry justifications; two minor silent swallows in `cli.py:262` and the off-by-default code-aware module); Rust panics are bridged to catchable `PyRuntimeError` via `catch_unwind` and the fail-open boundary catches `BaseException` specifically to trap PyO3 panics. No `print()` reaches stdout in library code — the MCP stdio channel is safe from stray output.

**Performance observations** ✅ — Warm compress calls 10–30 ms on ~29 KB payloads; 1.2 MB in 0.65 s; warm hook overhead ~0.13 s per call (uv resolution); first-use cold starts are the main tax (documented).

**Security observations** ✅ — No `eval`/`exec`/`pickle`/`os.system`/`shell=True` anywhere in the package; no runtime subprocess use (removed deliberately, PERF-13); all SQL parameterized or DDL; **zero `unsafe` blocks in 40K lines of Rust**; 11 `unwrap()/expect()` in non-test Rust paths (modest ⚠); store files 0600/dir 0700; per-project store isolation by default; agent-supplied regex matching runs under RE2 or a SIGALRM budget (documented main-thread limitation is why the MCP extra pulls in RE2). Residual notes: the PreToolUse tempfile fallback path (`/tmp/furl-pipe.$$` when both `mktemp`s fail) is predictable-named and created with default umask, so the "0600 tempfile" claim in the plugin README doesn't hold on that rare path ⚠; raw stdout transits a tempfile before redaction (disclosed for the normal path).

**Operational safety rating: high.** The dangerous-by-design surfaces (executing rewritten shell, reading files) are gated conservatively (total permission-rule passthrough; jailed + opt-in `furl_read`), and every failure mode I could induce degraded to "original content, uncompressed" rather than data loss or a broken tool call.

---

## Comparison With Alternatives

*(Web research, GitHub metrics cross-verified 2026-07-18. Fuller sourcing in the research notes.)*

**The field** ✅

| Project | Scale/maturity | Approach | Reversible? | Relation to furl |
|---|---|---|---|---|
| **Headroom** (upstream) | **~59.7K stars**, company-backed, pushed yesterday, 162 releases | Deterministic (SmartCrusher/CCR — the code furl inherited) **+ ML** (Kompress prose model, AST code compression, images) + proxy + multi-provider + cross-agent memory | **Yes** (same CCR lineage) | furl = Headroom's deterministic reversible core minus proxy/ML/dashboard/telemetry, plus native Claude Code plugin, sliceable retrieval, per-project isolation, redaction/purge, honest benchmarks |
| **RTK** | ~71.6K stars, Rust single binary | Lossy per-command CLI output rewriting (100+ commands), PreToolUse rewrite | No (failure-tee only) | Complementary first layer; furl's own docs say so, correctly |
| **lean-ctx** | ~3.3K stars, daily releases | Context intelligence: 10 file-read modes, 95+ shell compressors, memory, budgets, 76+ MCP tools, 30+ editors | **Claims yes** (content-addressed store + `ctx_expand`/`ctx_retrieve`) — contradicting furl's table ⚠ | Owns the file-read surface furl deliberately avoids |
| **LLMLingua** (Microsoft) | 6.4K stars, research-frozen (last release 2024) | Model-based lossy token pruning, up to 20x on prose | No | Opposite philosophy; strong exactly where furl is honestly ~0% |
| **claude-mem** | ~87.6K stars | LLM-summarized cross-session memory (SQLite+FTS) | No (lossy) | Different problem (persistence); proves the plugin category can win big |
| **magic-compact** | 114 stars (same age as furl) | LLM history compaction; pruned tool I/O retrievable by ID | Partially | Covers conversation history, which furl's plugin does not |
| **Built-in Claude Code** | — | Microcompaction (clears old tool results, cache-aware, free), `/compact`, huge-output file-offloading, subagents | No (cleared = gone; re-run the tool) | The 80% solution with zero install; erodes furl's automatic-path pitch for median users |
| **Compresr / The Token Company** | Real, YC-flavored hosted APIs ✅ | Lossy semantic compression as a service | No | furl's table rows for them are fair |

**Where furl is genuinely better** ✅ — It is the only *auditably minimal* implementation of the reversible approach: purely deterministic (no ML weights, no LLM calls, no telemetry), no proxy/`ANTHROPIC_BASE_URL` interception, byte-exact **sliceable** retrieval (row-select by field/range — not just whole-blob), search over stored originals, per-project isolation, pre-store redaction + purge, a provably conservative permission-rule guard, and a two-command *native* marketplace install. Its epistemic practice (publishing its own 0–54% failure band with committed corpora and adversarial audits) is unique in this field. For security-review/air-gap/compliance-minded users who must be able to read every line of what touches their data, that minimalism is the product.

**Where competitors are stronger** ✅ — Headroom: everything except minimalism (breadth, maturity, team, TS+proxy+multi-provider, ML tier for prose, images, memory). RTK: stops noise before it exists, zero latency, works unconditionally today. lean-ctx: the file-read surface (often a coding agent's largest cost — exactly what furl excludes), budgets, memory, 30+ surfaces. LLMLingua/hosted: the high-entropy prose furl honestly can't compress. Built-ins: free, supported, cache-aware, already good enough for most.

**Missing features relative to the field** ✅ — No lossy/semantic tier (removed with the fork; nothing replaced it); no conversation-history compaction in practice; no file-read coverage by design; no cross-session memory; thin non-Claude story (no proxy, no TS lib; "Codex plugin" = generic MCP compatibility); no dashboard; no image/output-token features (all excised upstream features).

**Fairness of furl's own comparison table** — This is the weakest honesty artifact in an otherwise honest repo ✅: it **omits Headroom entirely** while the framing line ("Furl runs locally, covers every content type, and is reversible") implies uniqueness its own upstream negates; the **lean-ctx "Reversible: No" row is contradicted** by lean-ctx's current README (timing uncertain ⚠); furl's own scope cell ("All context — tools, RAG, logs, files, history") is overbroad for the shipped plugin (files excluded by design, history untouched); and the built-in microcompaction/claude-mem/magic-compact comparisons a Claude Code user actually faces are absent. The RTK, OpenAI-compaction, and hosted rows are fair.

**Rational adopter profile** — A Claude Code / Python-agent power user pulling large *machine-generated structured* payloads (API dumps, CI logs, traces, fixtures) who needs every byte recoverable and nothing non-deterministic touching their data — and who accepts early-adopter risk. Most others should evaluate Headroom (same architecture, mature), RTK (first layer), or just the built-ins first.

---

## User Experience

**Documentation** — Comprehensive to a fault. README + LIBRARY.md answer nearly every question I generated during testing, *including* the embarrassing ones (TTL matrix, fail-open redaction gap under hook timeout, stderr interleaving loss, permission-guard blindness to CLI flags). The cost: the TTL story takes four surfaces to explain (library 30 min / CLI 24 h / plugin 24 h / bare MCP 1 h-session + 30 min dropped-rows), and a first-time reader must hold plugin-vs-engine versioning, three headline numbers, and two hash widths in mind. This is documentation written by (and arguably for) people who read specs.

**Onboarding** — Plugin path: genuinely two commands ✅. Library path: one pip install + 4-line snippet ✅. The first compressed output an agent sees is self-describing (`_dup_count`, `_ccr_dropped` marker, MCP instructions legend) ✅.

**Learning curve** — Low for "just install the plugin"; moderate for the library (filter DSL, TTL/backend/namespace matrix); the failure modes that matter (counters vs delivery) require reading the harness-status section carefully.

**Where users will get stuck** (observed, not speculative): (1) noisy shell profile → silent no-compression with green-looking counters — nothing in the docs mentions this failure mode ✅; (2) released-version `pattern` searches returning empty and looking like "no matches" ✅; (3) expecting file-read compression (excluded by design; the single biggest context cost in coding agents) ⚠; (4) Bash permission rules silently disabling the pipe — documented, but users with allow-lists won't connect their rule to missing savings ⚠.

**Would AI agents use it reliably?** The MCP tool surface is excellent for agents: structured results, loud errors, explanatory misses, search across stores. Two agent-trust hazards: the silent regex-cap false negative (an agent told "0 matches" believes it), and `isError: false` on error payloads (agents must parse the `{"error": ...}` convention).

---

## Strengths

1. **Honesty as an engineering practice** ✅ — committed adversarial audits of its own headline numbers ("degrade by 6–43pp", "effective savings could go NEGATIVE"); release notes with "Honest-docs corrections" sections; disclosed fork provenance; disclosed store-at-rest risks; disclosed supply-chain posture.
2. **The install really is two commands** ✅ (verified against real Claude Code 2.1.212), and pip install is 3.8 s with zero friction.
3. **Reversibility holds** ✅ — byte-exact text retrieval, semantic JSON re-serialization, 100% needle recall reproduced, purge/TTL loud-miss semantics all verified.
4. **Fail-open discipline** ✅ — every induced failure (bad backend, missing engine, tokenizer guard, tempfile failure, hook crash) degraded to original content; exit codes preserved exactly through the pipe rewrite.
5. **Security engineering above its weight class** ✅ — fail-closed redaction ordering, default-on credential scrubbing, jailed opt-in file reads (symlink-safe), total permission-rule passthrough, RE2/ReDoS budgets, parameterized SQL, no unsafe Rust, honest 0600-not-encryption framing.
6. **Observability built in** ✅ — durable cross-process counters with a documented invariant (`invocations == compressions + Σ noops`, which held exactly in my data), session stats, `furl_stats` cost estimates, `doctor`.
7. **Real, verified savings on the target workload** ✅ — 99%+ view reduction on repetitive JSON/logs reaching the model end-to-end in live sessions.
8. **Engineering hygiene rare at this scale-and-age** ✅ — 2,400+ tests, 4-shard CI, mypy/ruff/clippy/deny/commitlint, release automation, devcontainer, `act` configs, llms.txt.
9. **Agent-first ergonomics** ✅ — MCP results carry token accounting; error prose explains *why* and *what to do*; compressed views embed retrieval affordances.

## Weaknesses

1. **Zero adoption, zero visibility** ✅ — 2 stars, no dependents, 0/12 search queries, one indexed mention on the web. You would be the early adopter.
2. **Solo maintainer + extreme velocity** ✅ — 481/505 commits by one person; 9 releases in days; the README itself advises pinning. API stability is a stated aspiration, not a track record.
3. **Docs drift across surfaces** ✅ — four different stories about auto-compression status (PyPI blurb vs README vs plugin README/SKILL vs hook first-run note); stale PyPI links/summary; site headline vs README headline.
4. **The released artifact lags known fixes** ✅ — pattern-filter fix and fail-fast typing sit merged-but-unreleased behind an open release PR while PyPI and the plugin pin serve 1.2.0.
5. **Value is workload-dependent and honestly bounded** ✅ — ~0% on high-entropy/code content; file reads excluded by design; pipe is Bash-only; any Bash permission rule disables the pipe entirely.
6. **Env-var config surface is large** (20 `FURL_*` knobs read in code, 15 documented in the reference table) with per-surface default differences (TTL/backends) — a footgun matrix ✅.
6b. **Dual-language duplication as standing debt** ✅ — tokenizer, BM25, CCR store, and marker grammar each exist twice (Python + Rust), hand-synced by parity tests; heavy for a bus-factor-of-one project.
7. **Tonal/grammar rough edges in the README** ✅ — undermines the otherwise professional presentation.
8. **Multi-agent development inflow** (Bolt/Jules/lazy-dev PRs) sits unreviewed in the open-PR list, which reads as noise to a visitor ✅.
9. **It lives in its upstream's shadow** ✅ — Headroom offers the same local+reversible architecture with a company, 59.7K stars, and a superset of features; furl's differentiation (minimalism, native plugin, no ML/proxy/telemetry, sliceable retrieval, honest benchmarks) is real but narrow, and the docs never position against Headroom directly.

---

## Bugs Found

Ordered by severity. All independently reproduced; "released" = PyPI/plugin-pinned engine 1.2.0.

1. **P1 — Login-shell profile output silently breaks both automatic compression paths** ✅. `hooks.json` runs every hook via `sh -lc`. On any machine whose profile prints to stdout (nvm init here), hook stdout becomes `nvm\n{json}`, Claude Code discards the malformed envelope, and the model receives the raw output — while furl's counters record `hook_compressions_applied` (observed live: 19,200 raw chars delivered; counters +1 "applied"). The project's own test (`test_pretool_explicit_disable_is_cheap_no_uv_no_output`) fails on such machines — 2,409 pass otherwise — but CI images are quiet, so this ships. Fix: `sh -c` with explicit PATH handling (or strip non-JSON prefix before emitting).
2. **P1 — `pattern` retrieval returns silently empty on single-line JSON offloads (released 1.2.0)** ✅. Any `retrieve(hash, pattern=...)`/`furl_retrieve pattern`/CLI `--pattern` against a SmartCrusher offload stored as one long line returns `''`/`matched_count: 0` — even for plain literals that are provably present (`pattern="FATAL"` finds nothing while `select_equals="FATAL"` returns the row). Root cause: the ReDoS line-length cap (10,000 chars) skipped *all* patterns on long lines. Fixed at HEAD (PR #107: literals now substring-match), **unreleased**. Agent impact: confident false "no matches."
3. **P2 — Even at HEAD, non-literal regex patterns silently no-match on >10K-char lines** ✅. The cap is a deliberate ReDoS defense but is disclosed only in code comments; the MCP/library result gives no hint ("matched_count: 0", no note). An agent querying `pattern="Dropped.*Frame"` on a minified blob gets a wrong answer with no way to know. Fix: add a `note`/`lines_skipped_over_cap` field to the filter result.
4. **P2 — Docs contradict live behavior on the flagship feature (both directions)** ✅. README/LIBRARY/plugin README/SKILL say PostToolUse replacements are dropped pending upstream #68951; the hook's own first-run note says mirroring makes ≥2.1.163 honor them. Live test on 2.1.212: **the mirrored replacement IS applied** (with clean hook stdout). The docs under-claim; the surfaces disagree with each other; and the counters' name (`hook_compressions_applied`) counts *produced*, not *delivered*, replacements.
5. **P3 — "Claude Code & Codex plugin" tagline, but no Codex plugin exists** ✅. No Codex artifact, install path, or doc anywhere in the repo (the MCP server is generically usable, but that is not a "Codex plugin").
6. **P3 — Unpolished CLI error paths** ✅: `furl compress /nonexistent/file.json` and `furl compress <binary file>` print raw Python tracebacks (FileNotFoundError / UnicodeDecodeError) — the only two ugly error paths in an otherwise excellent CLI.
7. **P3 — Released `compress()` accepts wrong top-level types with only stderr noise** ✅ (string/None/int → fail-open passthrough, `error` field set inconsistently — `None` input yields `error=None`). Fixed at HEAD (TypeError), unreleased.
8. **P3 — Unknown `model=` silently accepted** ✅ — no warning in `result.warnings`; token counts computed against a fallback tokenizer without telling the caller.
9. **P4 — `fields=[...]` + `limit=` without a row-select raises a misleading error** ✅ ("select_field is required...") though docs present `limit` as the general row bound; `fields` projection alone is unbounded.
10. **P4 — PyPI wheel metadata stale** ✅ — project URLs point at the pre-rename repo; summary contradicts the current README's scope statement.
11. **P4 — Test suite writes to the real `~/.furl`** ✅ — hook-counter rows appeared in `$HOME/.furl` during a plain `pytest` run (imperfect isolation for a store the product treats as user data).
12. **P3 — The "Compared to" table (LIBRARY.md) is materially incomplete/inaccurate** ✅ — it omits Headroom (furl's own ~59.7K-star upstream, which is also local + reversible), its lean-ctx "Reversible: No" cell is contradicted by lean-ctx's current README (⚠ timing uncertain), and furl's own scope cell ("All context — tools, RAG, logs, files, history") overstates the shipped plugin (files excluded by design; history untouched).
13. **P4 — Config docs incomplete on a security-relevant knob** ✅ — 20 `FURL_*` env vars are read in code; LIBRARY.md's "every live FURL_* knob" table lists 15. `FURL_REDACT_BUILTINS` (disables default credential redaction) appears only in SECURITY.md; `FURL_MAX_COMPRESS_BYTES`, `FURL_MCP_LEGEND`, `FURL_PROFILE_BANNER` appear nowhere.
14. **P4 — "How it works" lists CacheAligner as an active pipeline stage** ✅ — it is off by default and detector-only even when enabled (it never modifies messages); the architecture diagram promises more than the default pipeline runs.

*(Also observed, my own harness artifacts — not furl bugs: two apparent failures during probing turned out to be my display-filter and pipe-exit-code mistakes; noted in the diary for honesty.)*

---

## Improvement Opportunities

**Critical**
- **C1. Replace `sh -lc` in `hooks.json`** (Bug 1). Why: silently disables the product's headline feature on common real-world machines while observability reports success; who benefits: every plugin user with a non-trivial dotfile setup; effort: small (one-line change × 3 hooks + a PATH-resolution strategy for `uv` + regression test already exists and currently fails in noisy envs).
- **C2. Cut the pending release** (Bugs 2, 7; open release PR #87). Why: PyPI and the plugin pin serve known-fixed silent-false-negative behavior; effort: minimal (merge + let automation publish; bump plugin pin).

**High Priority**
- **H1. Reconcile the auto-compression story across all surfaces** (Bug 4): one canonical harness-status paragraph (README anchor) that the plugin README, SKILL, hook note, and site all link; update it to reflect verified 2.1.212 behavior; rename or annotate `hook_compressions_applied` (produced vs delivered), or better, detect delivery (e.g., compare next-turn transcript) if feasible.
- **H2. Surface the regex line-cap in filter results** (Bug 3): add `note`/`skipped_long_lines` to `FilteredContent` and the MCP payload. Agents need machine-readable honesty, not code comments.
- **H3. Fix PyPI metadata on next release** (Bug 10) and the CHANGELOG's old-repo URLs. Cheap, high-trust/SEO payoff.
- **H4. Remove or implement the Codex claim** (Bug 5): either ship a documented Codex/MCP setup guide (the server is host-agnostic — a `codex mcp add` snippet may be all it takes) or drop the word from tagline/topics.
- **H5. Basic SEO execution** (from Phase 2): GSC/Bing submission + sitemap/meta verification, PyPI↔site↔GitHub link triangle, standardize the "furl-ctx" brand string, pursue the directory listings where category peers already rank.
- **H6. Fix the comparison table** (Bug 12): add a Headroom row (the honest move, and it sharpens furl's actual pitch — "Headroom minus ML/proxy/telemetry, as a native plugin"), re-verify the lean-ctx reversibility cell, narrow furl's own scope cell to what the plugin ships, and add the built-in microcompaction row a Claude Code user actually compares against.

**Medium Priority**
- **M1. Friendly CLI errors for missing/binary files** (Bug 6) — try/except with the same loving error prose the rest of the CLI has; effort: trivial.
- **M2. Warn on unknown `model=`** (Bug 8) via `result.warnings`.
- **M3. Simplify/table-ize the TTL-per-surface matrix** and collapse defaults where possible (why does a bare MCP server use a 1 h session TTL while its dropped-row originals use 30 min?).
- **M4. Test isolation for `~/.furl`** (Bug 11): point `FURL_WORKSPACE_DIR` at `tmp_path` in a session-scoped autouse fixture.
- **M5. Triage/close the AI-bot PR backlog** (9 open) or label them `agent-generated` so the PR list reads as maintained.
- **M6. README copyedit** — remove the "touch grass" line, fix the non-native constructions, expand "CCR" at first use.
- **M7. Publish one technical deep-dive post** (the honest-benchmarks story is genuinely novel content) for backlinks + credibility.
- **M8. Complete the env-var reference** (Bug 13): add `FURL_REDACT_BUILTINS` (security-relevant), `FURL_MAX_COMPRESS_BYTES`, `FURL_MCP_LEGEND`, `FURL_PROFILE_BANNER`, and a table row for `FURL_CCR_NAMESPACE`.
- **M9. Truthful architecture diagram** (Bug 14): mark CacheAligner "opt-in, detector-only" and CodeAware "opt-in `[code]` extra" in the "How it works" lists.

**Nice to Have**
- **N1. `fields`+`limit` composition** (Bug 9) or a clearer error.
- **N2. Encryption-at-rest option** for the CCR store (already honestly scoped out; even an opt-in SQLCipher backend via the existing entry-point mechanism would close a compliance gap).
- **N3. Hash-pinned engine bootstrap** for the plugin (tracked in SECURITY.md; an actual issue should exist so the tracker isn't empty).
- **N4. Windows wheel** (cibuildwheel) — removes the only platform with install friction.
- **N5. A 30-second demo GIF** on the README (one was removed; first-time visitors currently see only ASCII art).
- **N6. Predictable-tempfile fallback hardening** (`umask 077` before the probe, or accept and document).

---

## Final Verdict

**Yes, with caveats.**

For the question asked — *"Should I and my AI agents start using this project?"* — the evidence supports adoption in the narrow-but-real sweet spot, with eyes open:

- **Adopt now, low risk:** the MCP tools (`furl_compress`/`furl_retrieve`/`furl_search`) and the Python library for agent pipelines that push large repetitive tool outputs, logs, JSON dumps, or fetched pages through context. Verified: 90–99% reductions on that workload, byte-exact recovery, fail-open worst case, careful security posture, 2-command install. Pin the version.
- **Adopt with verification:** the automatic plugin paths. They demonstrably work end-to-end on a clean 2.1.212 machine (this audit proved delivery live), but a noisy shell profile silently neutralizes them while counters look healthy — run one `cat big-file` test and check what the model saw before trusting the savings, until C1 ships.
- **Don't adopt for:** high-entropy/code-heavy contexts (honestly ~0% savings), file-read-dominated agents (excluded by design), Windows-native without Rust, compliance environments needing encrypted-at-rest storage or hash-pinned supply chains, or anywhere a single-maintainer abandonment would hurt (fork risk is real; the engine is itself a fork).
- **Evaluate the field first:** if you don't specifically need furl's minimalism (no ML, no proxy, no telemetry, auditable size), its upstream Headroom offers the same reversible architecture with far more maturity and breadth; RTK is the better first layer for shell-heavy agents; and Claude Code's built-in microcompaction already covers the median user's pain for free.

What tips this to "Yes" rather than "Neutral" is integrity density: this project's failure modes are disclosed, tested, fail-open, and observable to a degree that most mature projects never reach — and its two worst current bugs were both *already found and fixed by its own audit process*, just not yet released. What keeps it from "Strong Yes" is that you would be roughly the first external user, on a 6-day-old name, with one maintainer, a drifting doc surface, and a release pipeline that is currently the bottleneck between users and known fixes.

---

## Research Diary

Chronological log of what I thought, found, questioned, and revised.

1. **Landing page.** Read the repo page cold. Understood the pitch in under a minute (good sign). Flagged: "CCR" unexplained, "0–54%" headline both honest and confusing, "Codex plugin" claim to verify, 2 stars, auto-compression admitted broken pending an upstream fix. Initial stance: skeptical — "honest-looking README" can itself be marketing.
2. **Trust sweep.** Releases showed v1.0.0→v1.2.0 in 3 days with startlingly self-critical notes ("was silent no-op live", "Honest-docs corrections"). Zero issues ever filed — the support path is unexercised. PR list revealed the repo was renamed from `furl` 6 days ago, that AI bots (Bolt/Jules/lazy-dev) file PRs that mostly sit unmerged, and that a previous external audit PR was closed unmerged while a follow-up merged its findings. Revised stance: this is a one-person, AI-agent-amplified, high-discipline operation.
3. **Upstream bug check.** Fetched anthropics/claude-code#68951 — real, open, repeatedly reported. The flagship feature's brokenness is genuinely upstream. (Their specific "2.1.207 repro" comment link I could not verify — API 403 — left ❓.)
4. **Install.** pip 3.8 s clean; plugin's two commands worked verbatim against real Claude Code 2.1.212 including strict validation. Source build 81 s. No guesses needed anywhere. This phase found essentially nothing to criticize — rare.
5. **First functional battery.** 14/15 probes matched docs. Warmup faster than documented. The planted-anomaly test *exceeded* the docs' promise (needle stayed visible). Two oddities queued: `fields+limit` error, and weird passthrough acceptance of non-list inputs.
6. **False alarm (mine #1).** A probe seemed to show row-selects returning empty `{}` rows — alarming. Re-testing showed my own grep filter was stripping indented JSON lines from the display. Lesson re-learned: verify the harness before the subject. Logged here for honesty.
7. **Real bug.** The `pattern` filter returned empty on JSON offloads even for `pattern="."`. Scoped it: text entries fine; single-line JSON entries always empty. Found the 10K-char ReDoS line cap in source; found the literal-substring fix at HEAD (PR #107); confirmed released 1.2.0 lacks it. Realized every current user has this bug because the release PR is unmerged. This became a theme: *the project's own audits find its bugs; the release pipeline delays the fixes.*
8. **Their failing test = my second lead.** HEAD's suite failed one test in my container: hook stdout expected empty, got `nvm`. Traced to `sh -lc` login shells sourcing `/etc/profile.d/nvm.sh`. Hypothesis formed: on noisy-profile machines the hook JSON gets corrupted live.
9. **CLI battery.** Excellent error prose, TTL honored, loud backend failures — plus two raw tracebacks (missing file, binary input) and the pattern bug reproduced on the user-facing surface. (Mine #2: two of my "exit code" readings were my own pipeline's exits — corrected.)
10. **MCP battery.** Six tools clean; jail rejected all four escape attempts including a symlink. The pattern bug via MCP returns `matched_count: 0` with no hint — the worst kind of answer for an agent. Noted `isError: false` on error payloads.
11. **Hook battery.** All 13 design properties held, including exact exit-code preservation and the total permission-rule guard. Discovered the PostToolUse envelope now *mirrors the tool's output shape* and its stderr note claims this defeats #68951 — directly contradicting the README on the same commit. Which is true?
12. **Binary spelunking.** Grepped the installed Claude Code 2.1.212 bundle: replacements are applied iff they pass the tool's `outputSchema.safeParse`. So string replacements (the upstream repro) fail; shape-mirrored dicts plausibly succeed. The hook note became credible; the docs became stale-in-the-under-claiming-direction. Only a live session could settle it.
13. **Live sessions (the pivot of the audit).** Session 1 (pipe off): model received all 19,200 raw chars — flagship failed. But the transcript showed why: `stdout: "nvm\n{...}"` — *my environment's login-shell noise*, not the upstream drop. furl's counters said "applied" anyway. Session 2 (patched `sh -lc`→`sh -c`): model received the **143-char compressed view** — auto-compression works on 2.1.212. Session 3 (defaults, clean shell): pipe path also delivered (136 chars, rewrite visible in transcript). Conclusion inverted twice in one hour: the feature is neither "broken upstream" (docs) nor "working everywhere" (hook note) — it works on clean machines and silently dies on noisy ones, with observability that can't tell the difference.
14. **Benchmarks.** Reproduced their runner: half the datasets byte-identical, drift small except the flashiest row (code@7, 471→1,678 tokens) — inside their own disclaimer, still worth reporting. Needle recall 100% held. The BENCHMARKS.md "three headline numbers" section resolved the site-vs-README contradiction I'd flagged in Phase 2 — partially downgrading that finding from "inconsistency" to "poor cross-linking."
15. **Security review.** Greps came back cleaner than expected (no eval/exec/pickle/shell=True/subprocess at runtime; zero unsafe Rust; parameterized SQL). SECURITY.md pre-disclosed almost everything I was preparing to "catch" (unencrypted store, hash-unpinned bootstrap, ReDoS-timeout redaction gap). Residual finds: predictable fallback tempfile, fallback umask vs the "0600" claim, suite writing counters into real `~/.furl`.
16. **Edge inputs.** Unicode roundtrip clean; 5 MB single line loudly declined (tokenizer guard) with fail-open; binary → traceback. HEAD now fail-fasts on type errors the release fails-open on — one more "fixed but unreleased."
17. **SEO agent returned.** 0/12 queries, zero indexed pages, one mention on the entire web, PyPI links pointing at the dead name, "furl" unwinnable vs a URL library/a startup/crochet hooks. The project is, for practical purposes, undiscoverable today.
18. **Deep sweeps returned.** The alternatives research delivered the audit's biggest reframe: Headroom — the upstream — has ~59.7K stars, a company, and the same local+reversible architecture, yet is absent from furl's comparison table; lean-ctx's "Reversible: No" cell is contradicted by its current README; RTK/claude-mem own adjacent layers at 70–90K-star scale; built-in microcompaction already solves the median case. Simultaneously the source sweep quantified the debt my spot-reads had suggested: dual-language duplication everywhere, 1.6K LOC of default-dead code-aware compression, CacheAligner advertised-but-inert, four undocumented env vars (one security-relevant), god-modules — alongside genuinely clean hygiene (no stray stdout, no TODOs, panics bridged, no permanent skips).
19. **Synthesis.** The pattern that organizes every finding: *exceptional engineering integrity, pre-adoption reality.* Nearly every defect I found was either already found by the project's own audit loop (and stuck in the release queue) or lives exactly where CI can't see (real machines' shell profiles, stale PyPI metadata, search indexes). Verdict settled on "Yes, with caveats" — the caveats being release lag, doc drift, the login-shell defect, workload fit, and the fact that an adopter today is the first one.
