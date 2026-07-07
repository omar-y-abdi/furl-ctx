# Harness Expansion Plan

Implement every **Topp-hävstång** (quick win) and **Big bet** from
`docs/HARNESS-USECASE-EXPANSION.md`. Each item wires or surfaces a capability that
already exists in the code but is gated off, unwired, or unexported.

## Execution model

- **PM/gate:** the orchestrator does **not** implement. Each item is delegated to a
  named subagent. The orchestrator reviews the diff as the harshest critic, and on
  any smell re-initiates the **same** subagent with a continuous-review prompt until
  clean. Only then: commit + push.
- **Quality bar — lazy senior-dev (user, 2026-07-07). "The best code is the code never
  written." The Ladder — stop at the first rung that holds, per item AND per sub-part:**
  1. Does this need to exist at all? Speculative → skip it, say so in one line (YAGNI).
  2. Stdlib does it? Use it. (CLI → `argparse`; HTML → `html.parser`; not a new dep.)
  3. Native/platform feature covers it? Use it.
  4. Already-installed dependency solves it? Use it. Never add a NEW dep for what a few
     lines do. (Reuse `benchmarks/`, `verify/`, `tiktoken` — don't reinvent.)
  5. One line? One line.
  6. Only then: the minimum code that works.
  Deletion over addition. Boring over clever. Fewest files. No unrequested abstractions/
  flags/config. Mark intentional shortcuts with a `lazy:` comment naming the ceiling +
  upgrade path. NOT lazy about: input validation at trust boundaries, data-loss-preventing
  error handling, security, anything explicitly requested.
- **Testing (lazy):** non-trivial logic leaves **ONE runnable check** — the smallest thing
  that fails if the logic breaks (an assert-based self-check or one small test file; no
  frameworks/fixtures). Trivial one-liners need no test. The full suite (`pytest tests/`)
  must stay green regardless.
- **Verify-first:** subagents must confirm each grounding claim against the real code
  before changing anything — the doc's `file:line` are a starting point, not gospel.
- **Green gate (every item):** `uv run ruff check .` + `uv run mypy furl_ctx
  --ignore-missing-imports` + `uv run pytest tests/ -q` (baseline **1583 passed**). Rust
  touch → `uv run maturin develop` first.
- **Compression-contract gate (routing/drop/offload items — Q3, Q5, B1, B2, B4, + any
  touching `content_router`/`content_detector`/`compress`):** ALSO `uv run python -m
  verify.run` → compare `verify/raw_results.json` aggregate ratios vs the committed floor
  (`benchmarks/baseline_results.json` / `BASELINE.md`) — **no regression**; + needle
  recall 100% (`benchmarks/needle_recall.py`). Unit-green is NOT compression-safe.
  `verify/raw_results.json` is generated — **never commit it.**
- **Branches (two PRs):** quick-wins land on `c7/harness-expansion` → green PR → merge →
  big-bets branch off the new main. Isolates CI, compounds on merged code.

## Critic checklist (reject + re-initiate the SAME subagent on ANY hit)

- Ladder skipped — reinvents stdlib / an already-installed dep / a few-lines job, or a
  sub-part that fails rung 1 (YAGNI) → **reject**.
- New dependency (esp. heavy) → **reject**, log as blocker-question instead.
- Speculative param/flag/abstraction/config not in the item spec → reject.
- Stub / TODO / placeholder → reject.
- Tests asserting structure not behavior (coverage theater) → reject (apply test-quality).
- `Any`-typed public signature (RULES no-lie) → reject.
- Mutation of shared/input objects (immutability rule) → reject.
- Ratio or recovery regression vs floor → reject.
- Non-minimal diff — any line not tracing to the item → reject.

## Order (dependency-sorted)

### Quick wins
- [x] **Q1 — Real Claude tokenizer** (#6, S) — `f393fe2a`. claude-* → TiktokenCounter
      o200k_base (was 3.5-cpt estimate); ImportError→estimator fallback. Mirrored in the
      Rust registry so FFI parity is byte-identical (claude asserted == gpt-4o/o200k both
      sides). No new dep; Anthropic-API exact-tokens deferred (blocker-question). Bench pins
      gpt-4o → neutral. 1585 pass, cargo/ruff/mypy green.
- [x] **Q2 — `compress_to(messages, max_tokens=N)`** (#8, M) — `furl_ctx/compress_to.py`.
      Thin bounded greedy orchestrator over compress(): fixed 5-rung kwargs ladder
      (protect_recent→0, compress_user_messages, min_tokens→50, protect_analysis→False),
      first rung that fits wins; unreachable budget → smallest result + warning (never
      raises/loops/over-budget). Measures the real tokenizer per rung, not the fail-open
      `tokens_after`. No engine change → bench-neutral. 1589 pass. *(PM-implemented: 2
      subagent stream-idle-timeouts on big-file reads; ~55-LOC item, sanctioned small edit.)*
- [ ] **Q3 — API-envelope unwrap** `{"data":[...],"meta":{}}` (#1, S). `content_detector`
      only detects `JSON_ARRAY` when the top level *is* a list. Add one unwrap pass over
      common keys (`data/results/items/hits/records/edges/rows/documents`) → SmartCrusher
      → re-wrap non-array fields. *(envelope keys speculative — verify.)*
- [ ] **Q4 — Retrieval exports** (#4, S). `retrieve`/`search`/`search_all` exist on
      `CompressionStore` but not in `furl_ctx.__all__`. Export `retrieve(hash, query=None)`,
      add `ccr_hashes: list[str]` to `CompressResult`, add `resolve_markers(messages, store)`.
- [ ] **Q5 — Wire CCR spill tier** (#5, S). `CompressionStore(spill=...)` implemented
      (`_spill_evicted`/`_recover_from_spill`) but the MCP server passes `spill=None`. Wire
      `FURL_CCR_SPILL_BACKEND`.
- [ ] **Q6 — Hook wires shipped config** (#2, S). Hook calls `compress()` with bare
      defaults, ignoring `DEFAULT_TOOL_PROFILES`, `is_tool_excluded()`, and `tool_name`.
      Map tool→profile bias, use the exclusion helper, accept a model; expose
      `FURL_HOOK_MODE` / `FURL_HOOK_EXCLUDE_TOOLS`.
- [ ] **Q7 — Observability bundle** (#3, S). Hook writes nothing on success;
      `mcp_server` hardcodes `$3.0/Mtok`; stats keep a 2h window; `furl_stats` is
      pull-only. Add opt-in stderr annotation (`FURL_HOOK_VERBOSE`), a real per-model cost
      model (`FURL_COST_RATE_USD_PER_MTOK` + tokenizer), append-only JSONL
      (`FURL_STATS_LOG_PATH`), `per_message_stats` on `CompressResult`, opt-in `timing`.
      *After Q6 (same hook file).*
- [ ] **Q8 — `furl` CLI** (#7, M). No `[project.scripts]`. Thin
      `furl_ctx.cli:main` over the library: `compress [file|-]` (`--json`,
      `--lossless-only`, `--model`), `retrieve <hash>`, `stats`, `doctor`. *Uses Q4, Q1.*

### Big bets
- [ ] **B1 — HTML main-content extractor** (#9, M). WebFetch is profiled *aggressive*
      but HTML routes to `noop` (0%). Add a readability-style main-content extractor
      (strip nav/script/ads, keep article + headings).
- [ ] **B2 — CCR durable-retention epic** (#10, L). Eviction *demotes* not deletes;
      session/conversation-scoped lifetime; TTL-extension-on-access; `session_id`/`agent_id`
      namespacing on `compress()`; `ccr_export`/`import`; pin-forever. See `CCR-RETENTION.md`.
      *Extends Q5.*
- [ ] **B3 — Redaction + purge + namespace + audit + encryption** (#11, L).
      `CompressConfig.redactor` (fail-**closed**, outside the fail-open boundary);
      `furl_purge(hash)` MCP tool + `furl purge` CLI; `FURL_CCR_NAMESPACE`; append-only
      `audit.jsonl`; optional at-rest encryption (`FURL_CCR_ENCRYPT_KEY`);
      `FURL_HOOK_SENSITIVE_TOOLS` → memory-only. *After B2.*
- [ ] **B4 — Cross-turn / whole-history wiring** (#12, M). Activate the idle
      `ReadLifecycleManager` (stale/superseded reads) via a conversation-aware path;
      `compress_chat_history()` preset; `compress_with_cache(freeze_up_to_n)` helper.
- [ ] **B5 — Eval / recall harness** (#13, M). `benchmarks/` + `verify/needle_recall.py`
      exist internally; expose `furl eval <corpus> --recall` (the trust gate). *Uses Q4, Q8.*

## Blocker questions (fill during the run; ask after everything is done)

- **B1 HTML extractor** re-introduces functionality the "Great Excision" deliberately
  deleted (`html_extractor.py` + trafilatura, user: "i want it GONE"). User re-authorized
  it here → proceeding with a **minimal stdlib-only** extractor (no trafilatura/readability
  dep). Flagging the re-introduction for confirmation.
- **B3 at-rest encryption** (`FURL_CCR_ENCRYPT_KEY` / SQLCipher) needs a heavy crypto dep →
  building the minimal redaction/purge/namespace/audit core; **encryption deferred as a
  question** (which dep, or skip?). Audit-format (fields/rotation) also a question.
