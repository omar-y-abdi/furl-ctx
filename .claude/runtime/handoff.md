# 🔶 CYCLE-6 + NEW PERMANENT PROCESS — recon→fix→DEEP-recon-VERIFY loop (user mandate 2026-06-27)

## LATEST — 2026-07-01 (cycle-6 tail; HEAD `f56f5927`, all gated-green)
Site-3 lookup-half extraction DONE (`92088258` + format guards `0844692f`) — `_lookup_cached_disposition` ADT (`ServeOriginal|ServeCached|Recompute`), both cache-lookup sites unified via `match`, data-loss guard centralized in one place; characterization-first route_counts pins + direct unit test (all 5 outcomes). ML→Kompress rename DONE (`1d8a69e9`, ~20 sites: method+dispatcher-param+type-alias). Round-3 re-recon (3 opus agents): **CODE zero-material**; found 8 doc-integrity MATERIAL → batch-fixed `e64602e2` + reconcile `7fc45aaa` (README fabricated benchmarks → honest BASELINE per user Option-A; accuracy-table deleted; RTK "ships with"→peer; phantom `[progress]`/ENTERPRISE-link/pre-commit-hook removed; telemetry→local-only; CODEBASE-MAP re-anchored exact; "60–95%" headline verified BACKED by BENCHMARKS.md 6-seed sweep + bridged to table). Gate G1-G5 green, 750 passed, recovery 23, needle 100%. Full ledger + round-4 scope = `PLAN.md`. **NEXT = round-4 re-recon (HOLISTIC doc sweep incl `.txt`, fresh agents) → confirmation workflow.**

**LESSON — PM DISCIPLINE (user correction 2026-07-01):** In cycle-6 I burned a full context window doing the round-3 batch-fix MYSELF (read ~15 files + 30 edits) after ONE edit-agent timed out — that is IC behavior, not PM. FIX going forward: **DELEGATE + VERIFY, never self-execute.** Recon = fresh subagents. Batch-fixes = edit-only subagents (I only spec → review → gate → commit). Agent times out → re-dispatch TIGHTER-scoped, do NOT drop into IC mode. PM writes to files sparingly + surgically. My context = orchestration, never execution.


Rerun #6 (wf_01820141-b7e, 199 agents) done → NOT beyond-perfect (as predicted). Deduped real material:
**A** blake3 doc-lie (ccr/mod.rs:70 says BLAKE3, code uses sha256[:6]) · **B** dead SQLite/Redis CCR backend
(sqlite.rs 205 LOC + from_config, test-only; "production default" docs lie) · **C** CCR mirror 6-file
replication → one `CcrMirror` owner · **D** ContentRouter god-object (1855 LOC, high) · **E** RelevanceConfig/
fastembed (the critique MISSED it — my known finding) · recovery-honesty docs ("within request window").
By-design re-surfaces: DC1 lossy/CCR scope, DC4 two-engine detection (no action). Deflated: MinTokens re-clone (WRONG).

**USER DECISIONS (AskUserQuestion 2026-06-27):** D god-object = **REFAKTORERA FULLT UT, korrekt, before next workflow**.
B SQLite backend = **RADERA** (lazy-dev, like A2 redis).

**USER MANDATE — NEW PERMANENT PROCESS** (replaces "surface-fix → run 200-agent workflow to DISCOVER"):
1. **RECON FIRST** — 2-3 opus DEEP-recon subagents, same lens-scope as the workflow but smaller/cheaper. Find EVERYTHING incl. small details. I (PM) am the toughest critic.
2. **CONSOLIDATE** — merge recon findings + known into ONE master defect list.
3. **BATCH-FIX EVERYTHING** in one informed pass (god-object full refactor + SQLite delete + RelevanceConfig + C mirror owner + A/docs + every recon finding).
4. **VERIFY = DEEP RECON AGAIN** (fresh subagents, NOT a surface gate). Anything found → FIX → repeat 3–4 until ABSOLUTE CERTAINTY beyond-perfect.
5. **CONFIRM** — run the 200-agent workflow ONLY as the final stamp, when already certain. Workflow = CONFIRMATION, never DISCOVERY.
PM owns cleanliness: a flaw the workflow can still find = PM's fault. NO slop, no missed small details.

**RECON ROUND 1 — DONE** (3 opus agents + 2 sub-sweeps). All findings consolidated → `.claude/runtime/recon-findings.md` (the MASTER list + P0-P8 plan). recon-arch produced the full god-object extraction blueprint (in recon-findings.md ARCHITECTURE section).

**BATCH-FIX PROGRESS (gate G1-G5 PASS each, recovery 23, needle 100%, bench 0%):**
- ✅ **P0** `a341bf4f` — CCR silent-loss HIGH fixed (bool-veto + passthrough in diff/log/search; RED→GREEN bite-verified). test_ccr_persist_failure_vetoes.py added.
- ✅ **P3+P4** `6aba33b3` — deleted ~1400 LOC dead ML/embedding subsystem (dynamic_detector.py + models/ + RelevanceScorerConfig embedding fields + create_scorer + default_batch_score). User-confirmed delete. Public-API change.
- ✅ **P1py+P5** `30c8742d` — Python doc-lies (tool_injection/marker_grammar blake3/compute_key, hooks.py ProxyConfig, kompress [proxy]→[ml]) + pyproject ([all]/[code], dropped [relevance], numpy→[ml], "proxy" keyword) + retrieval_endpoint dead param + verify F401.

- ✅ **Rust dead-deps + core doc-lies** `df173f5a` — dropped unused rayon/toml/http deps + config/pipeline.toml; fixed blake3→sha256[:6] (ccr/mod.rs:70) + sha2 [:16]→[:8] comment. maturin clean, gate PASS.
- ✅ **B SQLite/Redis backend deletion** `4c0b1b6d` — deleted sqlite.rs (205 LOC) + the whole from_config/CcrBackendConfig/CcrBackendInitError factory (test-only, verified zero prod callers — prod builds InMemoryCcrStore directly via SmartCrusherBuilder::with_default_ccr_store) + tests/ccr_backends.rs (in_memory coverage stays in in_memory.rs) + rusqlite dep (Cargo.lock regen, libsqlite3-sys gone). Reconciled EVERY "production default" doc-lie: ccr/mod.rs+in_memory.rs+backends/mod.rs docstrings, workspace Cargo.toml panic=abort (proxy→PyO3 host), crates Cargo.toml dashmap "proxy" comment, RUST_DEV.md 73-line uvicorn/sqlite/redis multi-worker section → process-local request-window note, CODEBASE-MAP.md from_config/backend lines (+ trait :45→:38 drift), compression_store.py format_retrieval_miss_detail model-facing "(Sqlite/Redis)". −590 net LOC. Gate G1-G5 PASS, recovery 23, needle 100%, bench 0%. KEPT `dyn CcrStore` trait (DI seam via with_ccr_store ×6 modules — NOT abstraction-for-one). HEADROOM_CCR_BACKEND Python entry-point hook LEFT (honest extension seam, registered-nowhere by design). DEFERRED: llms.txt SQLite-memory + proxy refs (separate dead subsystem — memory/ deleted 74a6f348; fold into doc-lie cleanup pass).

**REMAINING (in recon-findings.md detail):**
- **P6 god-object FULL refactor** — recon-arch 6-step blueprint. PROGRESS:
  - ✅ step 1 `68da6f03` — deleted dead eager_load_compressors + route_and_compress (router 2399→2313 LOC).
  - ✅ perf double-detection `47dce416` — compress() now computes detection once (moved the mixed/detection locals into `if debug_enabled:`; _determine_strategy seam untouched — test patches it with `lambda content:`, so do NOT change its call signature).
  - ✅ step 2 (block de-dup) `924ce8cc` — extracted `_compress_content_block(block, text, *, block_key, label, detail_prefix, ...)`; the tool_result + text branches of `_process_content_blocks` now both call it (was 2× near-identical). Behavior-identical (cache/CCR-divergence + content-block + persist-failure tests pass). Router 2399→2284 LOC. The method is currently a ContentRouter METHOD (de-dups but stays coupled) — a follow-up could move it to `router_compress_unit.py` as a free fn w/ injected deps (cache, ensure_ccr_backed, compress_fn) for full decoupling.
  - ⏭ **NEXT (remaining de-trip / extraction):**
    - apply() STRING path (~1685-1738) is the THIRD near-identical site but DEFERS compression to the parallel pending_tasks pass (Pass 2/3) — structurally distinct (no inline compress; result→result_slots not new_blocks). Its cache-LOOKUP decision (Tier1/Tier2/CCR/ratio) still duplicates `_compress_content_block`'s lookup half — could extract a `_lookup_cached_unit(content_key,context,min_ratio) -> decision` that BOTH the block method and the apply path use, leaving recompute-dispatch (defer vs inline) per-site. Higher risk (touches the parallel path). the cache+`_ensure_ccr_backed`+pin+ratio-gate logic is 3× near-identical — `apply()` string path (now ~1685-1738), `_process_content_blocks` tool_result branch (~2004-2145), text-block branch (~2147-2243). Extract `router_compress_unit.py::compress_cacheable_unit(text, *, cache, context, min_ratio, runtime, compress_fn, ensure_ccr_backed) -> UnitOutcome`, rewire ONE site per gated commit (apply string → tool_result → text). MUST-PRESERVE seams: `_ensure_ccr_backed` (test_result_cache_ccr_divergence.py calls it directly + asserts hasattr), `self._cache.{is_skipped,get,put,mark_skip,move_to_skip,invalidate,stats}`, `self.compress`, `self._timed_compress`. The 3 sites have SUBTLE diffs (tool_result uses block.content, text uses block.text, apply uses message slots + parallel pending_tasks) — read all 3 carefully, the shared core takes a string + returns (new_content|None, route_key, transform_label, detail). Gate (recovery 23 + needle + bench + test_result_cache_ccr_divergence) after EACH rewire. RISK: this is the recovery/cache hot path — a subtle behavior change = silent loss (the exact P0 class). Do it with fresh focus.
  - then step 5 ContentBlockWalker (extract `_process_content_blocks` body, keep delegator seam), step 6 ToinRecorder (optional; re-export `_create_content_signature` as module global — test patches it).
  - ✅ CONTAINED P6/P7-adjacent `091b319f` — ContentType total-fn (unknown Rust tag→PLAIN_TEXT+warn, +test) + `_get_kompress`/`_create_content_signature` Optional sigs (TYPE_CHECKING block) + RouterCompressionResult.cache_hit dead-flag drop. REJECTED prefer_code_aware_for_code "dead" finding (live: gates SOURCE_CODE→KOMPRESS at router_policy.py:65, tested). STILL PENDING: TokenCounter Protocol dedup (providers/base.py vs tokenizers/base.py) + providers/ fold.
- ✅ **P7 correctness/API — ALL DONE:** csv phantom-row guard `4551c0a1` (ordinal-bound, RED→GREEN) · compress() reject-unknown-kwargs `4551c0a1` (warn→raise TypeError, match apply()) · cache_aligner `34cf1bbc` (stateless: dropped instance latch, thread previous_prefix_hash kwarg — singleton no longer leaks cross-request; +no-cross-leak test) · mcp_server `163b131c` (TEAMMATE: headroom_read jail to HEADROOM_WORKSPACE_DIR/cwd before exists-probe + 10MiB caps + 200-char log truncation + generic model errors; I built the jail/cap tests).
- ✅ **P8 Python test hardening `b745e9b0`** (TEAMMATE, diff-reviewed): weak-assert→content-equality across 5 files (compression_store_guards, ccr_proportional, tool_injection_smartcrusher, compress_frozen_prefix, adaptive_min_ratio off-boundary). No strict assert weakened; G4 file untouched (23). csv adversarial fuzz + cache_aligner singleton test already landed in 4551c0a1/34cf1bbc.
- ⏭ **MY REMAINING LANE:** (1) **god-object STRUCTURAL** = apply-string-path de-trip (site 3, hot-path, P0-class risk — see NEXT above) + ContentBlockWalker. (2) **P8 RUST boundary tests** — crusher.rs SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES(256: 255/256/257), LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES(64: 63/64/65), diff/search(0.8)/log(0.5) min_compression_ratio_for_ccr inline boundaries (cargo-slow → I do, not teammate).
- **THEN:** deep RE-RECON (fresh agents, NOT surface gate) → fix anything → loop until zero MATERIAL (advisor stop-criterion: by-design/taste/nitpick don't count) → run confirmation workflow.

## CYCLE-6 BATCH-2 (this session, post-compact) — 6 commits, Hybrid orchestration
SQLite-del `4c0b1b6d` · P7-csv+compress `4551c0a1` · cache_aligner `34cf1bbc` · router-contained `091b319f` · mcp-sec `163b131c`(teammate) · p8-tests `b745e9b0`(teammate). User mandated PM-orchestration (not solo IC): I own god-object hot-path + review/gate/commit all; teammates fan-out isolated lanes. All gated G1-G4+ PASS, recovery 23.
Snapshots: codebase-CRITIQUE.md (cycle-6, root, untracked) + .claude/runtime/critique-cycle5-prev.md (cycle-5).

## CYCLE-6 BATCH-3 (this session, post-compact #2) — 4 commits, master-list MATERIAL items exhausted
- ✅ **god-object site-3 RESOLVED (banked + documented + pinned)** `591a897f` — apply-string-path is the 3rd cache-lookup copy but is DELIBERATELY NOT merged with `_compress_content_block`: it DEFERS recompute to the batched ThreadPoolExecutor pass (pending_tasks→Pass 2/3) while the block path compresses INLINE, and their transform-string formats differ. Advisor reframed the merge as MARGINAL tidiness on a hot path (the P0-class `_ensure_ccr_backed` is already shared at both sites — I'd overstated the safety case). Banking is only drift-safe if BOTH copies are pinned → added `tests/test_content_router_cache_lookup_paths.py` pinning the 2 previously-UNCOVERED lookup outcomes (Tier-1 skip-hit, Tier-2 tightened→move_to_skip) on BOTH the string path AND the content-block path; the other 3 (miss→serve, serve-cached, stale-recompute) are pinned in test_result_cache_ccr_divergence.py. Pins drive public apply() with the cache pre-seeded via mark_skip/put; cache-stat deltas double as proof the lookup was reached. Divergence documented at the lookup site so re-recon won't re-flag the untaken dedup. RED-bite verified (move_to_skip-noop + Tier-1-disabled mutations caught). **=> The high-value de-triplication (the recon-arch "REAL lever") is DONE: 2 truly-identical block branches merged (924ce8cc), 3rd site banked w/ evidence.**
- ✅ **SQLite residue** `1a78e6b4` — advisor pre-recon grep (from_config/CcrBackendConfig/SqliteCcrStore/rusqlite/sqlite across .rs/.py/.md/.toml) found ONE surviving doc-lie: in_memory.rs module docstring still said "production path must use sqlite or redis" / "switch backends" (no other backend exists post-4c0b1b6d). Fixed to accurate guidance (larger capacity; CCR-RETENTION.md). CODEBASE-MAP.md refs correctly DESCRIBE the deletion (left). Excision now verifiably clean.
- ✅ **TokenCounter dedup + providers/ fold** `9eb82351` — two identical-purpose TokenCounter Protocols (providers/base.py had count_text/count_message/count_messages; tokenizers/base.py had only the first+last, though its BaseTokenizer already implemented count_message). Added count_message to the tokenizers Protocol, repointed the SOLE importer (tokenizer.py), deleted the providers/ package (abstraction-for-one, not in top-level public API, zero tests/external imports). Gate PASS (G2 confirms no broken import).
- ✅ **P8 crusher boundary tests [High]** `ee847a30` — extracted the two absolute-saved floor gates (SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES=256, LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES=64) into `#[inline]` pure predicates (clears_small_array_lossless_floor / clears_lossy_survivor_floor) so the inclusive `>=` boundary (255/256/257, 63/64/65) is unit-testable WITHOUT a renderer-byte-exact fixture (saved = estimate_array_bytes − rendered.len() is renderer-coupled → brittle). Kills a `>=`→`>` mutation the directional tests survived. Behavior-identical (all 721 crusher tests green).

**CONSCIOUSLY BANKED (low-value/cosmetic — re-recon arbitrates):**
- **Ratio-gate exact boundaries (diff/search/log `min_compression_ratio_for_ccr`) [Med]** — DOCUMENTED RESIDUAL, not churned. diff has strong directional coverage already (min_compression_ratio_for_ccr_is_configurable: 0.8 emits / 0.5 suppresses + 0.1 no-op test, via the clean config knob). search/log gates are trivial `ratio >= min` comparisons (extraction = over-engineering). The exact-float boundary is renderer-coupled (the recon "0.79/0.80/0.81" conflated the default 0.8 with the fixture's actual 0.729 ratio) → brittle, consequence-low (CCR is a recovery aid, not correctness). Honest test-quality residual.
- **god-object ContentBlockWalker (step 5) + ToinRecorder (step 6, optional)** — NOT a bug/behavior fix; pure COHESION extraction (move `_process_content_blocks` body ~300 LOC to its own module, keep delegator seam). The MATERIAL god-object work (de-trip, dead-code, perf double-detection, Optional sigs, ContentType total-fn) is ALL done. This is cosmetic LOC-split on a P0-class recovery hot path; the handoff itself says "do it with fresh focus." Advisor was DOWN this turn → not started blind. Recommend: do it next with advisor-backed focus IF the user wants the full cosmetic split, else proceed to re-recon (the arbiter of whether the god-object is still "material").

**RE-RECON PRE-NOTES (new findings discovered this turn — belong to the re-recon, NOT reactive fixes):**
- `headroom/exceptions.py:85` `StorageError` is exported in __all__ but NEVER raised anywhere; no telemetry uses sqlite (its docstring `sqlite:///foo.db` example is a harmless generic URL, unrelated to the deleted CCR backend). Dead-but-public exception — re-recon to decide remove vs wire.
- `RUST_DEV.md:265-274` phantom-recorder bullet: references `recorder.py::_build_cache_aligner_tokenizer`, `NoopTokenCounter`/`TiktokenTokenCounter`, and "headroom.providers.* imports opentelemetry" — ALL absent from the tree (opentelemetry fully gone; providers/ now deleted). Pre-existing dead-doc cluster for the doc-rot re-recon pass.

**NEXT = the VERIFY phase: deep RE-RECON** (fresh opus deep-recon agents, diff-weighted on this multi-session effort's commits — newest/least-audited surface: SQLite excision, ML-chain deletion, P0 CCR-veto, god-object de-trip + banking, compress-kwargs raise, csv/cache_aligner/mcp P7, the new test pins, crusher predicate extraction). Find anything material → fix → loop until zero MATERIAL (by-design/taste/nitpick don't count) → THEN the 200-agent confirmation workflow.

---

# 🔻 CYCLE-5 "FIX EVERYTHING" — ALL 5 MATERIAL FINDINGS FIXED → rerun #6 launched

Cycle-5 critique (wf_d705ee60-a48): **NOT felfri** — 5 distinct material findings (ceiling Medium, 0 crit/high).
User mandate: **FIX EVERYTHING** + **A2 = delete gated Rust**. North star: rerun says "beyond perfect, nothing
needs to change". All 5 fixed, gate G1-G5 PASS per part (0% bench, needle 100%, recovery 23):

- [x] **A1** `756ba6f1` — `_detect_content` docstring truth + 4 Rust↔Python parity tests (bite-verified RED).
  MY cleanup defect: docstring claimed "no parallel paths" + cited phantom doc, but the Python regex backstop
  overrides routing on Rust=PLAIN_TEXT. KEPT the path.
- [x] **#5** `aecf3972` — explicit_hash floor: DOCUMENT not tighten (marker_grammar.py:27-35 = two-contracts design,
  loose store floor vs strict {12,24} ingress is BY DESIGN). Fixed false comments.
- [x] **T1** `8b37efe2` — `PipelineEvent` → `@dataclass(frozen=True)` + honest docstring. Only handler returns None.
- [x] **A2** `3b1d04b4` — DELETE gated Rust (magika/embeddings/redis + onnx feature): 45 cfg sites + 3 src files +
  deps (ort/ort-sys transitive) + redis tests. HybridScorer→BM25-only clean rewrite; redis→UnsupportedBackend
  loud-fail. −2373 LOC. (Builder agent timed out 3× on silent cargo → finished by hand.)
- [x] **A3** `b4940ca0` — EXCISE the whole `compression_policy` proxy vestige (advisor: full excision, not gate-trim).
  Read ONLY by 3 dead gates (`if policy is not None and …`, policy ALWAYS None — proxy gone, no Python constructs
  CompressionPolicy, public compress() ignores the kwarg). Removed: RouterRuntime field + from_kwargs + allowlist;
  SmartCrusher's ENTIRE threading.local (carried only this); cache_aligner gate; the now-unused `runtime` param on
  ContentRouter._record_to_toin. Behavior-neutral: gates controlled TOIN telemetry / cache-aligner warnings, NEVER
  compressed bytes (bench provably 0% — skipped redundant A/B per advisor). Reversed the #7 determination-lock
  (mandate shifted churn→dead-code); worker-isolation/thread-safety tests rewritten for the remaining 3 fields.
- [x] **map refresh** `d13adc30` — A2/A3-induced staleness in CODEBASE-MAP (MY mess): removed the broken
  `--features redis,magika,embeddings` cheatsheet command, the compression_policy allowlist entry, the feature-flag
  note; refreshed drifted line numbers in the touched lines.

**RERUN #6 LAUNCHED** (this turn): adversarial-critique.js UNCHANGED, args.map=CODEBASE-MAP.md, background.
Snapshot of cycle-5 critique → `.claude/runtime/critique-cycle5-prev.md` (/tmp was CLEARED — no longer durable).
**Advisor expectation: rerun #6 will NOT be "beyond perfect"** — RelevanceConfig (below) is a KNOWN live defect.
Rerun #6 hands the COMPLETE remaining list → fix in ONE scoped pass → rerun #7 = beyond-perfect candidate.

## ⏭ NEXT-PASS FINDING (known, pre-existing, DO NOT front-run pre-rerun) — RelevanceConfig / fastembed drift
Genuine material defect in the tree, separate from cycle-5 (deferred during A2). Confirmed this session:
- `config.py:49` `RelevanceConfig.tier: Literal["bm25","embedding","hybrid"] = "hybrid"` DEFAULTS to "hybrid",
  but `relevance/__init__.py:69-73` `create_scorer` accepts ONLY "bm25" and RAISES ValueError on hybrid/embedding.
- `relevance/` is BM25-only (base.py + bm25.py); "semantic/embedding scorers were retired" (__init__.py:15).
- `config.py:34-46` docstring + `embedding_model` field (config.py:56) advertise sentence-transformers/hybrid.
- `pyproject.toml` `[relevance]` extra installs `fastembed>=0.4.0` — NO Python code imports fastembed (DEAD extra).
- `tier` is DEAD config: smart_crusher IGNORES relevance_config (smart_crusher.py:310-312, warns if non-None).
- **CORRECTION (recon-arch verified 2026-06-27): sentence-transformers/spacy are DEAD, my earlier "LIVE" warning was WRONG.**
  `dynamic_detector.py` (1034 LOC) has ZERO live callers (only cache/__init__.py lazy-export + a config.py docstring;
  no transform/compress/pipeline/test imports it). `ml_models.py` (338 LOC, MLModelRegistry) is consumed ONLY by the
  dead dynamic_detector. sentence_transformers/spacy are imported ONLY in those two dead modules AND are in NO pyproject
  extra (not installable via the package) → vestigial. The WHOLE chain deletes: dynamic_detector.py + ml_models.py +
  their cache/__init__.py + models/__init__.py lazy-exports + ML_MODEL_DEFAULTS.sentence_transformer/.spacy +
  config.py embedding_model field + the RelevanceConfig embedding/hybrid advertising (the known E finding). ~1400 LOC.
  CAVEAT: these are PUBLIC lazy-exports (headroom.cache / headroom.models) — deletion is a public-API change; given size
  + the user's MCP-tools direction, likely confirm with user before nuking (analogous to the SQLite-backend ask).
  `onnxruntime` (ml extra) LIVE (kompress_compressor.py:28 onnx_runtime.py) — keep. numpy: verify live usage before touch.
- **Next-pass gate (advisor):** first test if `RelevanceConfig` is reachable via public `compress()`/`CompressConfig`
  (same test as compression_policy: not a CompressConfig field → ignored-with-warning = internal-only → tier-Literal
  narrowing is FREE; if public → narrow as a deliberate API change, not silent).

After rerun #6: delta vs critique-cycle5-prev.md → fix RelevanceConfig + whatever else surfaces in ONE pass → rerun #7.
PM-loop commit discipline: `git checkout HEAD -- uv.lock` first, `git add <specific>` (NEVER -A), guard
`grep -iE 'uv.lock|critique'`. Critique doc NEVER committed. branch verify/phase2-audit-report.
NOTE: /tmp tooling (bench_ref.json/regcheck.sh/critique snapshots) was CLEARED — reconstruct if needed.

---

# ⭐⭐⭐⭐⭐⭐ CYCLE-4 CRITIQUE FULLY RESOLVED → delete + rerun workflow (5th pass)

Every material cycle-4 critique item now resolved (verified this session):
item1 replay-guard `dd07cd40` (→ replaced by RouterRuntime field-guard in G) · item2 dead getattr `b10dcb71` ·
item3 MCP hash guard `02dd91d0` · item4 11-stage lifecycle `5bf0a303` + orphaned compute_frozen_count `95fd63a6` ·
item5 god-object split E `f0b7db1d`/F `7b47568d`/D(no-op) **+ typo-rejection `9107e749`** (the last open piece —
apply() now rejects unknown kwargs via `_APPLY_ALLOWED_KWARGS`, 0% bench/recovery 23, delivered WITHOUT the
net-negative full typed-request) · item6 TLS→RouterRuntime `142cde0c` · proxy-debt (A+B) done · map refreshed
`580a0bd0`. Deliberate-choice-1 (lossy-by-deletion reachability) = by-design open Q, documented map §6, not a defect.
**Nothing material left.** → advisor sanity → rm codebase-CRITIQUE.md → rerun adversarial-critique.js UNCHANGED
(args.map=CODEBASE-MAP.md). Snapshot prior at /tmp/critique-cycle4-prev.md.

---

# ⭐⭐⭐⭐⭐ GOD-OBJECT SPLIT + LIFECYCLE EXCISE — DONE (user chose "take the large refactors")

User decision: take the large refactors now, subagent per PART (sequential, not all at once), verify
regression per part, >3% → revert + discuss, <3% → keep + discuss, goal 0%. + "Radera 11-stage lifecycle".
Protocol: characterization-test-first for the bench-blind landmine; bench measured vs a FIXED reference
(/tmp/bench_ref.json, determinism confirmed 2× identical); recovery 23 + needle 100% = hard gate.

**All 7 parts done. ZERO regression — every shipped change 0.000% bench, needle 100%, recovery 23.**
| Part | Commit | Result |
|------|--------|--------|
| A lifecycle deletion | `5bf0a303` | 8 dead PipelineStage + discover path + CANONICAL tuple + README overstatement gone. 0%. |
| B Rust cache_control | `95fd63a6` | whole orphaned module (compute_frozen_count + helpers + tests + re-export) deleted; Python mirror is live. Removed a critique-praised-but-DEAD proptest. 0%. |
| D StrategySelector | (no-op) | already extracted to router_policy.py; ContentRouter methods are thin delegators doubling as test seams — removing = break seams for zero gain. |
| E StrategyDispatcher | `f0b7db1d` | 260-line dispatch+fallback → router_dispatch.py leaf (no router ref, deps injected per-call). 0%. |
| F CcrMirror | `7b47568d` | _ensure_ccr_backed cache-hit path + _extract_ccr_hashes → router_ccr_mirror.py leaf. extract byte-identical. recovery 23. 0%. |
| C typed CompressRequest | NOT SHIPPED | NOT A CLEAN WIN — net-negative (layer trilemma + name mismatch + tri-state). Characterization test `f577d47f` KEPT (pins the bench-blind 50-vs-250 floor). |
| G TLS→RouterRuntime | `142cde0c` | hidden mutable thread-local + replay (51 grep→0) → frozen value threaded by arg. 3 mechanism tests rewritten, BITE INDEPENDENTLY RE-VERIFIED (injected leak → isolation RED). 0%. |
| fmt | `23084658` | cargo fmt the cycle-3 A-1/A-5 test drift; ci-precheck-rust clean. |

Net: ContentRouter **2566→2344 LOC** (−222); 2 clean leaf seams (router_dispatch 343, router_ccr_mirror 154);
orphaned Rust module + dead lifecycle excised; TLS hidden-state eliminated (type-driven RULES alignment).
Final sanity: gate G1-G5 PASS, floor byte-exact, needle 100%, fmt-check + clippy clean.
**Tradeoff discussion: no regressions to weigh (all 0%). Only residual = C's typo-rejection not delivered
(low-severity internal API) + B dropped a praised-but-dead test. Both evidence-backed, surfaced to user.**

---

# ⭐⭐⭐⭐ CYCLE-3 RERUN (cycle-4 critique) — DONE, awaiting user macro-decision

Rerun `wf_e8857221-553` (EXACT same adversarial-critique.js UNCHANGED, args.map=CODEBASE-MAP.md):
**12 lenses · 2 rounds · 217 findings → 142 material / 21 by-design / 45 deflated / 9 nitpicks.**
Critique re-extracted to codebase-CRITIQUE.md (untracked; agent returned text, repo read-only).

**DELTA vs cycle-3 critique (snapshot /tmp/critique-cycle3-prev.md) — convergence-shaped:**
- cycle-3 top-6 RESOLVED/GONE: item1 scrape (→ now only the by-design lossy-reachability open Q +
  the documented 1b mirror), item3 FFI hand-sync (→ DEFLATED: critic ruled the duplication thesis
  "factually wrong" — compute_frozen_count orphaned, CCR_HASH_WIDTHS a re-export, validator is the
  authoritative binding), item4 memory/providers GONE, item5 diff-comment GONE, item6 unwrap GONE,
  docs a/b GONE (content_detector + map-thinning honest). My cycle-3 work landed and left the report.
- Test suite is now the critic's #1 EVIDENCE the code is well-written — explicitly credits the hardening
  I wrote: test_ccr_hash_parity_vectors (A-1/A-2), ccr_roundtrip dropped_count_ties (A-5), Python-store-only
  recovery (TE1), CompressorRegistry extraction (item2). Severity ceiling fell critical→med ⇒ med/trivial.
- NEW thin layer (3 TRIVIAL — all FIXED this turn + 1 medium + the declined large):

**Cycle-4 trivial fixes — DONE, gated (G1-G5 incl. bench floor byte-exact), committed:**
- `02dd91d0` item3 (security): MCP `headroom_retrieve` now applies the width+charset spoofing guard;
  extracted `marker_grammar.is_valid_ccr_hash` as single source, routed BOTH ingress points (parse +
  retrieve) through it (dedup). +5 parametrized tests. (Critic: "not a vuln" — surface consistency.)
- `b10dcb71` item2 (refactor): dropped 3 dead `getattr(self,"_runtime_*",default)` probes on
  always-present properties → read directly; behaviour byte-identical (bench floor held). Comment fixed.
- `dd07cd40` item1 (test): structural guard — parses live ContentRouter source, asserts
  `_runtime_*` properties == snapshot keys == replay keys, so a future option added without wiring the
  worker-TLS replay fails HERE not in prod. (Hardens the #10 surface I'd kept byte-frozen in cycle-3.)

**OPEN — the two macro-decisions surfaced to user (NOT silently defaulted):**
- **item 4 (11-stage pipeline lifecycle, only 3 stages live):** proxy-removal debt — 8 unreachable
  `PipelineStage` values + `discover_pipeline_extensions` + README "stable 11-stage contract"
  overstatement + orphaned Rust `compute_frozen_count` (no PyO3 binding). DELETE per standalone-excise
  + lazy-dev, OR KEEP+document as the extension surface the upcoming MCP/hook work will consume? →
  roadmap-dependent, user's call.
- **god-object (items 5-6, med·large):** ContentRouter 2566 LOC + per-request state on a shared
  singleton via TLS. Critic itself ranks it "lowest leverage, sequence last" and CREDITS the partial
  extraction. DECLINED twice on never-regress (large hot-path refactor). Accept as honest documented
  debt, OR take the StrategySelector/Dispatcher/CcrMirror split (effort + regression risk)? → user's
  risk-tolerance call.

Loop status: converged to "by-design/declined + a thin nitpick layer." A further rerun (~10M tokens,
~2h) would surface the next thin layer + the same declined god-object. Whether to spend it = user macro
decision (below). HEAD = dd07cd40.

---

# ⭐⭐⭐ CYCLE 3 — COMPLETE (full arch ✅ + test-hardening ✅ + close-out ✅ + rerun ✅)

User chose FULL cycle 3 (items 1+2+3) + test-hardening loop + fix-bugs + validate every
testimonial + verify critique dealt-with → delete → rerun EXACT workflow. "EVERY line to
perfection, nothing less." PM-loop throughout; never regress.

**Phase A — cycle-3 architecture: COMPLETE.**
- item 3 `790cb21a` — asdict SmartCrusherConfig single-source + named CSV-codec constants.
- item 1a `8253fd00` — crush() row-drop recovery TYPED (plural ccr_hashes/row_index_markers via
  additive DroppedRef sink; scrape scoped to opaque-only). New test_crush_typed_hash_parity.py (41,
  bites RED→GREEN 2 ways; caught a real missed drop-arm during dev). A1 fail-safe intact.
- item 1b — BLOCKED → accepted as DOCUMENTED RESIDUAL. `_ensure_ccr_backed` (content_router.py:1354)
  re-mirrors on every result-cache HIT from the cached COMPRESSED STRING only (cache stores
  (string,ratio,strategy), hit short-circuits crush → no typed result), so the scrape is load-bearing
  there. Full deletion needs a result-cache schema change (cache typed refs) = separate hot-path
  refactor w/ regression risk → DECLINED on never-regress + lazy-dev (scrape is necessary, fail-safe
  via A1, grammar-drift-guarded). FLAGGED to user as optional separate task. For Phase D: item 1's
  "delete the scrape (~300 LOC)" premise was factually wrong — the cache-hit path genuinely needs it.
- item 2 `4c6c493f` — extracted CompressorRegistry (compressor_registry.py, 5 self-contained factories;
  _get_kompress STAYS, reads thread-local). Factories-only seam (dispatch coupled to fallback+TOIN+#10
  → left, same ceiling as R6). content_router 2597→2566. #10 worker-options surface byte-unchanged;
  the 2 thread-safety tests green AND biting.

**Gate at Phase-A end (HEAD `4c6c493f`):** cargo 729 · pytest 639 · recovery 23 · worker-options 8 ·
floor held + needle 100% · clippy/fmt clean · maturin builds. Env: venv now py3.14 (abi3 .so OK),
maturin reinstalled. `uv` ops truncate uv.lock 6942→2837 → I restore before every commit.

**Phase B — 2nd test-hardening loop: IN PROGRESS.** Audit (general-purpose agent) → worklist; baseline
60% line / 86% branch, 639 tests. Done + committed:
- `1b4cd7c0` — Python: C-1..C-4 fragility repair (dropped match= prose-couplings, pinned hash-floor
  literals, parametrized readback-tautologies, fail-loud the vacuous TOIN skip) + B-1 mcp_server handlers
  28→76% (importorskip mcp; CI must install the extra) + B-3 parser 53→86% + A-4 ISO-boundary 88%.
- `a26fe310` — A-1/A-2: pinned `hash_canonical` CCR recovery key with FIXED literals BOTH sides (was
  recompute-tested both sides = the audit's biggest mutation-survivor; the OTHER 2 hashes were already
  pinned). Rust crusher.rs test + new tests/test_ccr_hash_parity_vectors.py (cross-impl hashlib≠sha2 +
  a live-crush() cross-language lock). PM wrote these directly — the builder agent stream-idle-timed 3×
  (slow silent cargo compile); pre-warmed the build, pre-computed+verified the literals, wrote them myself.
- `cb308cdb` — A-5: pinned CCR drop-count arithmetic (kept + advertised == N) in ccr_roundtrip.rs.
- `55917ada` — A-3 (evidence-based via cargo-mutants): analyzer.rs had 95 surviving mutants/231; the
  largest + highest-value cluster = every `&&` junction in is_iso_datetime/is_iso_date (`&&`→`||`) + is_digit.
  Two positional-corruption tests killed it — confirmed `cargo mutants --re is_iso` = 44/44 caught. D-2 =
  accepted residual (lowest audit value; grammar pinned via A-1 + markers.rs).
cargo-mutants now INSTALLED (the only objective Rust mutation number; score.py has no Rust profile).

**Phase B DONE (high-value subset).** Documented residual: ~71 analyzer mutants survive in lower-value
helpers (estimate_reduction telemetry ratio, python_repr Number-arm = likely EQUIVALENT mutation, internal
stat counters) — integration-tested via the crusher path + floor bench, not correctness defects; a full
analyzer mutation-kill is a scoped follow-up. Lower-priority module coverage left: kompress 47% (mostly
ONNX/torch env-gated), toin 49%, compression_feedback 39%.
**Phase C: EMPTY — no production bug surfaced.** Every hardening step closed a TEST gap; the code was
verified correct (mutants survived for want of tests, not wrong logic; all gates green throughout).

**Recovery WRITE-path mutation audit (post-Phase-B, advisor-prompted).** `cargo mutants --file crusher.rs
--re 'persist_dropped|hash_canonical|canonical_array_json|ccr_dropped_sentinel'` → **0 missed / 8 caught /
1 unviable**. The load-bearing recovery functions (the hash key + the drop sentinel + the canonical
encoder) are mutation-clean, not just covered. Objective number behind "recovery is safe."

**Phase D — close-out finding→status map (HONEST, not over-claimed):**
| critique # | finding | status | evidence / residual |
|---|---|---|---|
| 1 | crush() text-scrape recovery → typed return | **SUBSTANTIAL** | `8253fd00` typed `ccr_hashes`/`row_index_markers`; `f23e64e4` fail-loud; 41-test parity. **Residual 1b:** `_ensure_ccr_backed` cache-HIT re-mirrors from cached string (no typed refs on hit) — DECLINED (result-cache schema change = hot-path regression); the critique's "delete ~300 LOC" premise is factually wrong for the cache-hit arm. |
| 2 | ContentRouter god-object split | **PARTIAL** | `4c6c493f` CompressorRegistry extracted (the critique's own "do first" lowest-risk seam); 2597→2566. Full split DECLINED (dispatch coupled to fallback+TOIN+#10, same ceiling as R6). Map made honest. |
| 3 | FFI hand-synced contracts | **DONE(config)/PARTIAL(CSV)** | `790cb21a` `**asdict` config forwarding (kills 3-site edit); CSV grammar constants NAMED both sides but not generated-from-one-spec (2 files, pinned by 200-case fuzz). |
| 4 | empty memory/ + providers/ husk | **DONE** | `74a6f348` deleted memory/; `3c422b98` de-ceremonied providers/ (dead `_LAZY_EXPORTS` for 1 zero-dep Protocol; only consumer hits providers.base directly). |
| 5 | diff CCR knob missing comment | **DONE** | diff_compressor.py:93-95 explanatory comment. |
| 6 | two `.last().unwrap()` guard-distance | **DONE** | grep confirms zero `.last().unwrap()` in log_compressor.rs/analyzer.rs. |
| docs a | content_detector stale parity-harness docstring | **DONE** | content_detector.rs:24 now states the harness was removed. |
| docs b | map "2926→2578 thinning" overstated | **DONE** | CODEBASE-MAP.md:11/65 honest ("grown back to ~2597, core stayed coupled"). |
| tests | 59% cov, ~41% unpinned | **HARDENED** | Phase B: MCP 28→76%, parser 53→86%, csv 88%, CCR hash literals pinned both sides, is_iso cluster killed, recovery write-path 0/8 survivors. Residuals: kompress 47%/toin 49%/compression_feedback 39% (env-gated/low-value). |
| deliberate 1-4 | lossy-core / two-store / archive-not-refactor / Rust-only CCR knobs | **BY-DESIGN, aired** | The critique itself rules these "by-design, open questions, not settled" — architectural choices for re-examination, NOT defects. Acknowledged, not "fixed". |
| lens gaps | security / perf / eviction-fuzz | **DISCLAIMED** | The critique flags these "coverage gaps, not clean bills" (lenses not run), not findings. |

**Disposition:** every material defect (1-6, docs a/b) is DONE or addressed-with-documented-residual; the
3 residuals (1b cache-hit scrape, full router split, CSV codegen) are each DECLINED on the never-regress
constraint with a written reason, not skipped. Deliberate choices are by-design. → critique "dealt with"
in the honest sense (explicit disposition per finding). Cleared to snapshot → rm → rerun.

**NEXT — Phase D execute:** snapshot critique → /tmp → `rm codebase-CRITIQUE.md` → rerun
adversarial-critique.js UNCHANGED (args.map=CODEBASE-MAP.md) → delta report vs this map.

---

# ⭐⭐ CYCLE 2 — COMPLETE (finish sequence: sanity ✅ → rerun NEXT)

All 4 cycle-2 findings fixed + gated + committed (PM-loop: agent edit-only → I gate independently → commit on PASS, `git add -u`, critique never staged):
- [x] **TE1** `97ad8a4f` — row-drop Python-store-ONLY recovery assertion (store.retrieve, NOT ccr_get) → production path load-bearing in CI. Recovery 22→23; gate.sh G4 grep updated to `23 passed`.
- [x] **C1** `9ea2b34c` — `compress()` swallow made fail-open LOUD + honest: `logger.error(exc_info=True)`, real `tokens_before`, added `error: str|None` field to CompressResult (5 construction sites back-compat). Still fail-open (returns original), no re-raise.
- [x] **T1** `be16a798` — frozen `CompressRequest` built ONCE at the pipeline boundary (config.py), kills the 250-vs-50 `min_tokens` divergence (one default, 250). SAFE subset: A3 thread-local LEFT (the thread-safety test pins the mechanism, not just behavior → genuinely-coupled core, documented).
- [x] **A1** `f23e64e4` — CCR mirror made fail-LOUD + fail-SAFE (the user-chosen route, given the verified fact that BOTH stores are in-memory — R7 holds, Python store NOT durable, so durability-build (a)/(b) was out of scope; the real defect was the mirror's SILENT branches). smart_crusher 3 loss-branches → `raise CcrMirrorError` → propagates to `compress()` fail-open (compress.py:386) → original returned (nothing lost); diff/log/search sidecars → `logger.error` (loud floor, no fail-open caller). New test `tests/test_ccr_mirror_no_silent_loss.py` (5, separate file → recovery stays 23) pins it RED→GREEN.
- [x] **Sanity sweep** `b18b10c1` — `cargo fmt --all` (pre-existing drift across 12 core files; gate never ran fmt-check → `make ci-precheck-rust` would have gone red). `CODEBASE-MAP.md` parity-harness stale clause excised (removed `make test-parity`/`headroom-parity` refs).

**FULL SANITY CHECK ✅** (HEAD `b18b10c1`): cargo · pytest **598** · surface **40** · recovery **23** · bench floor held + needle **100%** · clippy `-D warnings` clean · `cargo fmt --check` clean · `maturin develop` builds clean. Codebase passes its own ci-precheck.

**★ ENV DRIFT (flag to user):** `.venv` was silently recreated today 14:31 by an agent's `uv pip install`/`uvx` → now **Python 3.14.2** (handoff baseline was 3.13) and **maturin was gone**. The `.so` is `abi3` so it loads fine under 3.14 and all gates are green; I reinstalled maturin (1.14.1, `uv pip install`, uv.lock untouched) so `maturin develop` works again. Interpreter left at 3.14 (functional; pinning back to 3.13 is risky env-surgery with no functional benefit). NOTE: agents' `uv` ops truncate `uv.lock` 6942→2837 each time — I restore it (`git checkout HEAD -- uv.lock`) before every commit; never let it into a commit.

**NEXT (finish sequence):** `rm codebase-CRITIQUE.md` (untracked throughout — confirmed NEVER in git history, so plain rm, no history rewrite) → re-run EXACT same `.claude/workflows/cleanup/adversarial-critique.js` UNCHANGED (`args.map=/Users/k/dev/headroom/CODEBASE-MAP.md`) → DELTA REPORT vs prior runs. Then (per roadmap) test-hardening is the promoted next step; MCP build deferred.

---

# ⭐ HANDOFF — MASS-REPAIR + STANDALONE-EXCISE (cycle 1 — COMPLETE)

**Where we are (2026-06-24, post-compact, IN PROGRESS):** MASS-REPAIR executing. PM-loop = dispatch opus agent (one concern, EDIT-ONLY no git) → I verify independently → run gate → commit on PASS. Agents must NOT commit (keeps me in verify-loop). `builder` agent type needs /tmp/subagent-instructions-<session>-builder.md filled (overwrite per task).
**DONE so far (baseline was 885d9b61):**
- R0 ✅ `9a2dbf24` — 10 scratch docs → docs/audits/. (⚠️ LESSON: `git add -A` re-staged untracked codebase-CRITIQUE.md → had to amend it out. ALWAYS `git add -u` (tracked only), NEVER -A — critique must stay untracked, deleted at finish.)
- R-EXCISE-A ✅ `6ba131de` — removed ALL upstream URLs (pyproject [project.urls], Cargo workspace repository +2 inheritors, marketplace.json ×2, README badges+vercel links+clone+ghcr, CONTRIBUTING, llms.txt), CHANGELOG reset 661→14 standalone, DELETED wiki/ + mkdocs.yml + .github/workflows/docs.yml. KEPT chopratejas HF model IDs (live weights — owner decided keep) + README model-card links.
- R-EXCISE-B ✅ `91bf9ae6` — scrubbed ~90 proxy comments→hook/MCP reality + ~130 fork-era tracker tags across 54 py/rs files (comment-only, gate+bench floor held), removed 3 dead paths.py funcs (savings_path/proxy_log_path/proxy_clients_dir) + 4 orphan constants. LEFT load-bearing strings (telemetry _TABLE="proxy_telemetry_v2"/sdk="proxy"/context return "proxy", HEADROOM_PROXY_CACHE_CONTROL_AUTO_FROZEN env, headroom-ai[proxy] extra, on-disk filenames), symbols (ProxyConfig), C-legit.
**OWNER DECISIONS locked (AskUserQuestion):** keep chopratejas HF models · remove upstream URLs entirely (no new repo) · reset CHANGELOG · delete whole wiki.
- R-EXCISE-C ✅ `86733705` — deleted 8 dead-feature workflows (proxy/wrap/docker/eval/e2e/openclaw) + e2e-setup action + 2 dangling marketplace.json + copilot template + 2 act fixtures; FIXED keepers (ci/rust/publish/release/dependabot/codecov/release-please-config) to drop deleted-path refs; cut dead TelemetryBeacon + telemetry/context.py (beacon.py 345→56), kept live is_telemetry_enabled. −1070 LOC.
- R-EXCISE residual ✅ `795e44a5` — upstream URLs in ccr/mod.rs rustdoc + build.rs comment → repo-relative/stripped. **R-EXCISE NOW COMPLETE: zero upstream URLs in live src, zero proxy narration.** KEPT chopratejas model IDs (kompress-v2-base, technique-router, kompress-finance docstring example) per owner decision. archive/ chopratejas refs = OUT OF SCOPE. devcontainers.yml + .devcontainer/ KEPT (live dev infra, flagged to user). release.yml fallback `--notes-file`→`--generate-notes` (flagged).
**DONE (critique findings):**
- R1 ✅ `c3ee5dc5` — clippy too_many_arguments → PrioritizeParams struct (+2nd all-targets warning), -D warnings clean.
- R3 ✅ `9298cc83` (core) + `43ded592` (cascade) — deleted ALL dead Python algorithm twins + the legacy-helper ring (adaptive_sizer.py, log/search _select_*/_score_*/_parse_*/_detect_format/_format_output/_store_in_ccr + orphan regexes). Both compressors hard-import Rust (no fallback) → twins proven dead. Behaviors Rust-pinned. Suite 626→567. Kept _persist_to_python_ccr/_format_from_str (live). FileMatches/SearchMatch now public-but-internally-dead → TIER-3 API-deprecation (left).
- R4 ✅ `64abbbc2` — collapsed dead OTel ceremony in pipeline.apply(), removed _telemetry_noop.py, kept record_metrics (kwargs-pop + simulate dry-run) + contract test. Suite 571.
- R5 ✅ COMPLETE — 3 gated steps: `2ae0fada` (step1: 9-shape characterization test through production consumer = closes gate blind spot), `88f68dfe` (step2: Rust marker_for_* family in ccr/markers.rs, all 7 producers routed through, opaque dup collapsed, dead compute_key/marker_for + blake3 retired), `ff2b4ff2` (step3: Python marker_grammar.py single spec, consumers repointed, producer-driven equiv tests that BITE, dead regex #2 retired). Marker grammar now single-owned + cross-checked. Recovery 21→22. ★ KEY: gate.sh G4 updated 21→22.  ★ KEY LEARNING: gate.sh does NOT run maturin — after ANY Rust change, run `.venv/bin/maturin develop` BEFORE gate (else pytest/bench test stale .so). needle-recall (G5) + recovery tests use their OWN parsers → the characterization test is the real consumer-regression guard.
- R6 ✅ `af2ab84e` — extracted the 4 CLEAN seams (router_cache.py/router_split.py/router_policy.py: CompressionCache, split fns, strategy-mappings+adaptive_min_ratio+CompressionStrategy enum); class methods delegate (test assertions byte-identical). LEFT the coupled core (two-pass apply, _runtime_* _tls, ThreadPoolExecutor snapshot/replay, CCR-verify, Kompress/TOIN) per no-regression — extracting it re-opens the silent #10 worker-options regression. content_router.py 2926→2578. Advisor-blessed scope. (verify/raw_results.json scratch left untracked.)
- R7 ✅ `2a0cb825` — retention OPEN QUESTION resolved by HONEST DOCS (user chose doc over feature-build; recon proved "durable already built" FALSE — no Python durable backend, single-tier store, durability≠retention; it's a net-new epic). Fixed CCR-RETENTION.md + CompressionStore docstrings: byte-exact recovery scoped to 1000-entry/300s window, loud miss after. Doc-only, no regression.
- D3 ✅ MEASURED (no commit — report only). strategy_info (crusher.rs:86). VERDICT: critique conflates two things — the lossless-first routing BRANCH is speculative under min-tokens default (wins 3/10 engaged), BUT the compaction-IR SUBSYSTEM is LOAD-BEARING (CSV-schema renderer on 10/10 engaged, ROWDROP_pure=0, sole compressor for small clean tables, removal inflates engaged output 34.2%). Measured benefit, NOT the parity/maintenance cost (that's the unquantified part). Severe corpus caveat (11 hand-built arrays). Cheap instrumentation: label the existing CrushEvent with strategy_info prefix. DO NOT delete compaction.
**★ ALL BACKLOG COMPLETE + FINISH SEQUENCE IN PROGRESS:**
- Full sanity gate+bench PASS on final tree (`aa782c3d`): G1-G5, recovery 22, floor held, needle 100%, maturin-rebuilt.
- `rm codebase-CRITIQUE.md` DONE (confirmed NEVER in git history — untracked throughout via git-add-u discipline → plain rm, no history rewrite).
- CODEBASE-MAP refreshed to post-repair tree + residual fork/proxy scars swept (BLAKE3 comments, Makefile dead proxy/parity targets) — commit `59cd773b`.
- ⏳ **adversarial-critique.js RE-RUN LAUNCHED UNCHANGED: `wf_f60fec75-4fe`** (args map=CODEBASE-MAP.md). Running in background → writes fresh codebase-CRITIQUE.md → notifies on completion.
**ON WORKFLOW COMPLETION → DELIVER DELTA REPORT.** Compare new critique vs ORIGINAL (wf_8f16e11f: 203 findings → 148 material/14 by-design/30 deflated/11 nitpick; top material = #1 clippy [FIXED R1], #2 root .md sprawl [FIXED R0], #3 telemetry ceremony [FIXED R4], #4 dead py twins [FIXED R3], #5 CCR marker grammar [FIXED R5], #6 ContentRouter god-object [FIXED R6 safe seams]; by-design retention [R7 honest docs], SmartCrusher [D3 measured]; standalone-excise [R-EXCISE A/B/C done]). EXPECTATION SET: all-opus adversarial critic structurally finds findings on ANY codebase → deliver measurable DELTA (every top-6 material finding resolved + scars excised), NOT literal "zero findings."
**Gate baseline: suite 592/14, recovery 22, floor held, needle 100%. HEAD `59cd773b`. PM-loop: agents edit-only, I gate (+maturin first if Rust changed), commit on PASS, git add -u (never -A → critique untracked).**

**TWO MANDATES (user):**
1. Fix every actionable critique finding. Scope LOCKED via AskUserQuestion: both large refactors fully (#5 marker-grammar, #6 ContentRouter split); change by-design items too (telemetry, retention, SmartCrusher). User words: "Båda fullt, without any regression!!!" + "Ändra dem också".
2. ⭐ **STANDALONE REPO, not a fork.** Every loose thread to the old repo MUST be excised, not documented. "If something is pointing to the old repo, that has to go." Hard no-go.

**HARD CONSTRAINT: no regression.** Gate `.claude/runtime/gate.sh [bench]` = G1 cargo · G2 pytest · G3 surface · G4 recovery-21 · G5 run_bench==floor + needle 100%. (`floor_check.py` compares fresh run_bench vs committed baseline json.) Validated PASS on baseline. run_bench overwrites baseline_results.json+BASELINE.md → gate.sh auto-restores.

**FLOOR (locked):** code@7 0.0% · logs@90 92.8% · search@90 92.2% · repeated_logs@90 96.5% · disk@9 50.0% · multiturn@135 70.6% · needle 100%.
**BASELINE HEAD = `4a412c43`.** cargo 765 green, pytest 626/14, surface 40, recovery 21.

**PM loop per task:** spawn opus agent (ONE selective concern, never the whole list) → testimonial → I independently run gate → PASS+good ⇒ commit; FAIL/bad ⇒ restart agent w/ feedback. "Don't let anything slip past."

**RECON DONE for R-EXCISE — old-repo surface mapped:**
- ⭐ Upstream repo = **`chopratejas/headroom`**. Hard threads: `pyproject.toml:101-103` (Repository/Issues/Changelog URLs), `Cargo.toml:20` (repository), `CONTRIBUTING.md:90` (git clone URL), `CHANGELOG.md` (ENTIRE file = upstream issue/commit links #761..#859), `CODE_OF_CONDUCT.md:131` (mozilla, OK keep).
- ~110 "proxy" mentions in live code (headroom/ + crates/src): paths.py (proxy_log/savings/clients — dead proxy artifacts), smart_crusher.py (8 proxy comments), content_router.py:825/1476/1831, telemetry/beacon+context+toin (proxy framing), kompress (proxy startup), ccr/mod.rs:39/67/80 ("the proxy will hold"), crusher.rs:223/429/1234, lib.rs proxy comments, ccr/backends/{sqlite,redis,mod}.rs proxy comments.
- Fork-era tracker tags: `#21`/`#816`/`#847`/`#802`/`PR-B1`/`F2.2`/`Stage-3c.2`/`PR4` across config.py, compress.py:408, compression_store.py:62/351, diff/search/log_compressor (issue #816), smart_crusher.py (many F2.2/Stage-3c.2/PR4), pipeline.py:58/86/221 (PR-B1, issue #847), cache_aligner.py:243 (PR-F2.1).
- wiki/: `wiki/proxy.md` (whole file = proxy) + proxy mentions in shared-context/typescript-sdk/mcp/troubleshooting/ccr/learn/cli/ARCHITECTURE/getting-started/docker-install.
- NOTE: some "proxy" comments are legit-but-stale framing (engine is now hook/MCP). Excise = remove proxy-era language/dead proxy artifacts, reframe to current standalone reality. NOT mechanical s/proxy//g — judgment per site. `paths.py` proxy_savings/proxy_log/proxy_clients may be genuinely-dead artifacts (verify callers).

**BACKLOG (risk ascending) — see PLAN.md for full detail. NOTHING DONE YET:**
R0 root .md cleanup (PM direct) · R-EXCISE ⭐ (old-repo threads) · R1 clippy args-struct · R3 dead Python twins · R4 telemetry collapse (keep record_metrics) · R5 CCR marker-grammar ownership (keep explicit_hash) · R6 ContentRouter god-object split · R7 retention durable spill (Sqlite default, no-regression-default-off). SmartCrusher D3 = MEASURE not delete (deleting compaction regresses lossless floor).

**FINISH:** full sanity gate → `rm codebase-CRITIQUE.md` (untracked, no history rewrite) → re-run EXACT same `adversarial-critique.js` unchanged → delta report. User expects "written perfectly" — I SET EXPECTATION: adversarial critic finds findings on any codebase; deliver measurable delta (fewer/lower material), not "zero".

**UNCOMMITTED right now (commit before/at compact):** `.claude/runtime/gate.sh`, `.claude/runtime/floor_check.py`, `PLAN.md` (new active section prepended). `codebase-CRITIQUE.md` stays untracked (deleted at finish).

**WORKFLOW re-run cmd (finish step):** `Workflow({ scriptPath:'.claude/workflows/cleanup/adversarial-critique.js', args:{ map:'/Users/k/dev/headroom/CODEBASE-MAP.md'} })` — DO NOT edit the workflow. Prior run wf_8f16e11f-22e: 203 findings → 148 material/14 by-design/30 deflated/11 nitpick.

---

# HANDOFF — Headroom compression engine (rebuild + max-compression + verification)

cwd: /Users/k/dev/headroom · venv: .venv (x86_64, python3.13 restored) · branch: verify/phase2-audit-report
Build: `.venv/bin/maturin develop` (rebuilds pyo3 ext). Public API: `from headroom import compress`.

## ⭐ ARCHITECTURE DECISION — LOCKED 2026-06-20 (the north star)
DELIVERY = **hook (data-plane) + 2-tool fastmcp server (control-plane)**, NOT the proxy. CUT ALL PROXY.
- Scope: ONLY agent harnesses with same architecture as Claude Code (Claude Code + Codex). NOT general/any-app.
  This is the exact condition that makes the proxy's universality worthless here → proxy = ~3.2k LOC of pure cost.
- Engine is transport-agnostic: proxy, hook, MCP all call the SAME `ContentRouter`/`SmartCrusher`. Only transport differs.
- WHY hook wins (verified from source): (1) hook only sees tool OUTPUTS = headroom's actual hit-zone; proxy's "extra"
  reach is cached prose (untouchable) or non-JSON (engine passes through anyway). (2) hook compresses BEFORE content
  enters context → cache forms around compressed bytes → the whole "did we bust the cached prefix" risk DISAPPEARS
  (proxy does delicate live_zone surgery to avoid it). (3) ~3.2k LOC of proxy is streaming/SSE/auth the hook never needs.
  (4) MCP makes retrieve an explicit model-visible tool → better fit for sampling-blindness than proxy's silent inject.
- THE 2 MCP TOOLS: `set_compression(on/off[/mode])` + `retrieve(hash)`. retrieve MUST point at CCR store
  (`headroom/cache/compression_store.py`) DIRECTLY — current `mcp_server.py:472 _retrieve_via_proxy` is proxy-coupled,
  that coupling is the one non-trivial bit of the rebuild (rest is straightforward).
- SEQUENCING: Tier-1 cleanup NOW (teammate, excludes proxy) → Tier-2 (incl. proxy→hook+MCP rebuild, user gives instructions).

## WHAT THIS IS
Forked LLM-context compression engine. Rust core `crates/headroom-core` (+ pyo3 `crates/headroom-py`) + thin Python `headroom/{transforms,ccr,cache}`. Amputated 384k→~91k LOC, hardened, max-compressed, independently verified.

## JOURNEY (done, all committed)
- Phase 0: origin detached. Phase 1: amputate 384k→91k (−76%), build green, suite green (~352).
- Phase 2: DESIGN.md → Imp1 (1A unconditional CCR persist, 1B novelty fill+singleton pin) + Imp2 (field-aware stable hash) + Imp3 honest bench. Adversarial loop proved+locked the CCR recovery invariant; fixed non-dict silent loss + marker-off/blob holes.
- Max-compression: round-2 (delta/dict/cross-message), round-3 (affix-fold, head-dict + entropy-floor crushability override). route-by-min-tokens (RoutingPolicy=MinTokens default).
- Fixed TTL/result-cache silent-loss bug (recompute-on-unbackable, commits abfb19b3 + 8cae60a6).
- Independent held-out verification (verify/ slugify+is-plain-obj; verify/heldout/ express+chalk+npm-cli) — engine untouched. VERDICT: recovery REAL + byte-exact + generalizes (not overfit); 0 silent loss; cache-prefix safe.

## CURRENT NUMBERS (held-out, honest)
~93-97% on redundant/medium data; DEGRADES on near-unique: logs high 82%/genuine 80%, disk 40-44%, multiturn 28-39% (only 70.8% at 900-items/low-entropy). Headline = CEILINGS, not typical. Recovery 100% byte-exact everywhere.

## DONE — wyg1sl7ew (`headroom-fix-weaknesses`): granular-CCR `cbf16a85` + strict/honest-verify `d3fdc3f5`. Both landed, tree clean.

## NOW — parallel-eval phase, PREPPED + WAITING FOR USER "go"
User wants 3 eval workflows × ~50 sonnet agents (isolated worktrees, MEASURED before/after), loop-until-dry,
ANTI-REPEAT ledger (no agent redoes a tried approach; reason from prior failures + justify novelty), → opus
synth → 3 ranked action-docs. Cost irrelevant ("brim-optimize everything").
- Built ONE reusable parameterized workflow `headroom-parallel-eval` (modes optimize|break|quality).
  Saved: `.claude/workflows/eval/headroom-parallel-eval.js`. Validated 0 warnings.
- EXACT launch steps + args: `.claude/runtime/eval-launch.md` (Phase 0 rebuild+baseline, then 3 sequential runs).
- BLOCKED: user's own workflow (opus agent) still running → repo LOCKED, no build/launch until user says "go".
- On "go": follow eval-launch.md (re-read HEAD, rebuild shared .venv, fresh baseline, run optimize→break→quality,
  write EVAL-{optimize,break,quality}.md from synth.doc_markdown, present to user).

## HOOK REAL-WORLD TEST — Biljakten data-plane (2026-06-14)
First real-world test of the integration-layer DATA PLANE: a PostToolUse hook that pipes tool output through the engine.
- BUILT: `/Users/k/dev/Biljakten/.claude/hooks/headroom-compress.py` (+ 2nd PostToolUse entry in that project's settings.json, matcher `Bash|Read|Grep|Glob`, runs `/Users/k/dev/headroom/.venv/bin/python3`). Fail-open (any error/parse-fail/no-shrink → ORIGINAL kept, exit 0). Compresses via `ContentRouter`; replaces output with `hookSpecificOutput.updatedToolOutput`.
- BUG FOUND+FIXED (Biljakten transcript = ground truth): `updatedToolOutput` MUST match the tool's native OUTPUT SCHEMA. Read = object `{type,file:{content,...}}`; Bash = `{stdout,stderr,interrupted,isImage,noOutputExpected,persistedOutputPath,persistedOutputSize}`. v1 returned a bare STRING → Claude rejected (`invalid_type: expected object, received string`) → fell back to ORIGINAL (uncompressed); that was the "hook warning" the user saw. FIX: rebuild the RECEIVED tool_response with only its text slot (`file.content`/`stdout`/`text`) swapped → schema-valid for any tool. Also fixed: v1 compressed the `json.dumps` WRAPPER (degenerate — whole file behind 1 CCR sentinel); now compresses CLEAN content.
- COMPRESSION SHAPE RULES (empirical — the engine's real hit-zone):
  - FLAT homogeneous JSON array → SMART_CRUSHER 93–96% ✓ (149 brand records 6797→324 = 95%).
  - NESTED / pretty-printed JSON (`filter_items` in `filter_items`) → TEXT fallback, 0% → passthrough.
  - Source code · git-log · free text → passthrough.
  - i.e. headroom ONLY compresses arrays-of-flat-records.
- BIG FINDING — MODEL MISREADS THE DENSE FORMAT: on a real 95% compression (10 visible rows + 139 offloaded to CCR), the model decoded the columnar `[10]{hits,make,models}` rows BY EYE and got 2/10 hits WRONG (BMW 11450→read 11530; Mercedes 11530→read 5248). Errors clustered at `=` ditto-marker rows — model resolved ditto SEMANTICS right (BMW models=28, Renault=41) but scrambled positional column-alignment for neighbors. PLUS 139/149 rows offloaded to `<<ccr:...>>` sentinels = UNRETRIEVABLE (no MCP retrieve tool wired). → "fully compressed, decode-by-eye" is LOSSY in practice: model errors + unrecoverable offload.
- BIGGER FINDING — SAMPLING BLINDNESS (effort-PROOF): the model (even after correcting the decode error) answered the actual question ("top 10 brands by MODELS") from the 10 KEPT rows — but those 10 are what the compressor sampled, NOT the answer. Real top-10-by-models includes Opel(40)/Peugeot(40)/Toyota(39) which were OFFLOADED → invisible; model instead listed Audi(28)/BMW(28)/Kia(26) which aren't top-10. 3/10 wrong, and NO amount of effort/decoding fixes it because the needed data is in the 139 offloaded rows. The model gave a confident, well-formatted, WRONG table; nothing in the compressed view forced it to realize "I can't rank 149 from 10 rows." → row-drop + ranking/aggregate queries ("top-N", "most", "sum", "count") = confidently wrong answers, model unaware. This is headroom's classic "lossy by deletion" weakness surfacing in the agent loop.
- IMPLICATION: validates the integration-layer design — DATA PLANE (hook) works, but the engine needs (a) the MCP CONTROL/RETRIEVE plane so offloaded rows are recoverable, (b) the compressed view must make the model AWARE the table is a SAMPLE (N shown, M offloaded) so it retrieves before aggregating/ranking, and (c) a model-legible format OR not asking the model to decode columnar+ditto by eye. Next concrete step toward the user's integration layer.
- CANDIDATE ENGINE FEATURE (user asked for layman explanation): "nested-aware path" — recursively find flat sub-tables INSIDE nested JSON and crush each, keeping the tree. Engine work (Rust core `crates/headroom-core`), NOT the hook. Would widen hit-rate on real (nested) API/tool output. Moderate effort: flat-table crusher already exists; new part = recursive walk to FIND inner tables + re-assemble + keep round-trip/recovery invariant.

## HARD CONTRACTS (never break)
CCR recovery invariant (100% recoverable, 0 silent loss; tests/test_ccr_recovery_invariant.py). Prompt-cache ordering (never drop msg index 0 / reorder cached prefix / rewrite cache_control). Python↔Rust parity (compute_item_hash / canonical CCR hash byte-stable). Default lossless decoder behavior. No synthetic benchmarks. No overfit to verify/ fixtures.

## GUARDRAIL CMDS
`cargo test -p headroom-core 2>&1 | grep 'test result:'` (0 failed) · `.venv/bin/maturin develop` · `.venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --continue-on-collection-errors --timeout=120` (≥420/0) · `.venv/bin/python -m pytest tests/test_ccr_recovery_invariant.py -q` (21) · `.venv/bin/python -m benchmarks.run_bench` THEN `git checkout HEAD -- benchmarks/baseline_results.json benchmarks/BASELINE.md` (run_bench overwrites baseline — always restore).

## OPEN / NEXT (user steers each step)
- Integration layer (user's design, awaiting final confirm): PostToolUse hook (compress, 2 flags: `fully on` + `gömda`) + MCP server (`set_compression(mode)` / `retrieve(hash)` / `expand`). Control-plane (MCP, model-driven) + data-plane (hook). RoutingPolicy config = the knob MCP flips. Model-controlled adaptive compression. NOT built yet.
- After weaknesses fix: maybe honest README, hybrid-routing default.

## GOTCHAS
- fable5 (`model:'fable'`) INACCESSIBLE this session → use inherited Opus (workflows already switched).
- claude-mem plugin DISABLED (settings.json enabledPlugins→false) — was looping (orphaned 13.5.7 + wedged worker); ignore its hook errors.
- zsh does NOT word-split unquoted vars → list files explicitly in pytest cmds.
- Routing-gate hook needs `./.claude/workflow/DEFAULT_WORKFLOW.md` read (cwd-relative) before Write/Bash; headroom/.claude/workflow/ docs created to unblock subagents.
- Agent tool `builder` subagents need a filled instruction-doc at /tmp/subagent-instructions-<session>-builder.md (template at ~/.claude/hooks/lib/subagent-templates/_default.md).
- Workflow agents commit to main; run engine-touching agents SEQUENTIALLY (shared maturin build + git index race).

## REUSABLE WORKFLOWS (saved, .claude/workflows/{group}/)
amputate/headroom-amputation-map · compression/headroom-engine-map · compression/headroom-recoverability-refute · compression/headroom-max-compression · review/headroom-fullscale-review · (_drafts/headroom-adversarial-verify, headroom-fix-weaknesses)

## KEY ARTIFACTS
DESIGN.md, BENCHMARKS.md (repo root); verify/REPORT.md + verify/heldout/REPORT.md (the truth); benchmarks/ (run_bench, run_final, datasets, BASELINE.md).

## SIMPLIFY-AUDIT (2026-06-19) — lazy-dev over-engineering audit, REPORT-ONLY
User: "simplify my entire codebase" via /workflow-architect + Lazy Senior Dev (ultracode). Built a dynamic
workflow (`.claude/workflows/_drafts/simplify-audit.js`, validated 0 warn) — 9 read-only `Explore` auditors
(no Edit/Write) per area → selective adversarial verify of big `delete` claims → synth returns markdown;
ORCHESTRATOR writes the single additive file `lazy-dev-AUDIT.md`. Nothing mutates the repo. Run wf_713eb596-33f.
- TREE: auditing branch `verify/phase2-audit-report` @ a654963a (the latest-work tree; main stale @ 47eaf125).
  Stale `wf_ad2e78a5` worktrees from the dead P0+P1 run still in .claude/worktrees/ — audit excludes them.
- KEY SCOUT FINDING (the headline): the "amputated" bloat is STILL LOAD-BEARING via keep-set imports —
  proxy(3894) <- transforms/pipeline.py(lazy)+compression_policy.py+ccr/*; tokenizers(1816) <- pipeline.py(lazy);
  models(596) <- config.py+cache/*; providers <- tokenizer.py; storage <- cache/backends; hooks.py <- compress.py;
  relevance(1017)+shared_context lazily re-exported by __init__.py. observability/telemetry(4001)/integrations/
  onnx_runtime/component_tracker appear unreferenced (only proxy/models reach telemetry). So naive "rm dead dir"
  is WRONG; the audit traces LIVE vs VESTIGIAL reachability from compress() + names the untangle per cut.
- DELIVERABLE: ranked cut-list in 3 tiers (safe-now / cut-after-untangle / internal-shrink). Applying = SEPARATE
  user-gated step (compression engine w/ hard CCR/cache/parity invariants — never auto-delete).
- DONE (run wf_713eb596-33f, 15 agents/967k tok): wrote `lazy-dev-AUDIT.md` (repo root). 45 findings, 11 verified-safe, 1 refuted.
  Verify CAUGHT THE SCOUT BEING WRONG (its whole point): telemetry/(4001) is LIVE (SmartCrusher TOIN loop, every lossy
  compress) — NOT cuttable; wiki/(6200) REFUTED (mkdocs docs_dir:wiki + REALIGNMENT retirement plan + live CHANGELOG link).
  I CAUGHT a synth coherence bug: REALIGNMENT/(3900) ranked Tier-1 "dead" yet cited as the LIVE plan that keeps wiki/ →
  reclassified as roadmap docs (archive, not blind-delete). Honest cut budget of ~91k LOC: ~1.3k zero-risk now (sql/docker/
  stray scripts/manifest lines), ~9.3k after small export/import untangles (cache-optimizer family 4.4k = biggest LOC lever;
  CCR batch+mcp_server 1.9k — but mcp_server load-bearing IF the planned MCP retrieve plane is built next; relevance 1k;
  interceptors+binaries 1.2k; shared_context, ml_models, bench dedup), ~35 lines internal shrink. Amputation INCOMPLETE +
  keep-set more entangled than handoff claimed. Workflow saved: `.claude/workflows/cleanup/simplify-audit.js` (reusable).
  Committed report+workflow. APPLYING NOT STARTED (user-gated).
- CUT APPLIED (user: "cut all code, archive anything important, test it runs"): archive-not-delete + test-gated.
  ~7.4k LOC moved to `archive/` (reversible; restore map in archive/RESTORE-LOG.md), engine GREEN throughout.
  TWO GATES every batch (advisor — pytest shares the audit's lazy/dynamic blind spot): G1 pytest 519/31 (== baseline,
  zero regress); G2 surface-walk all __all__ resolve (59→56); final: CCR recovery 21/21 byte-exact + compress() saved 6316 tok.
  Commits: B1 2f234c04 (docs/sql/docker/scripts), B4/5 1755d4b1 (semantic+prefix_tracker+shared_context), B6 55befe92
  (proxy/interceptors+binaries), B3 05ff00b2 (_OPTIONAL_EXPORTS+create_pipeline+manifest). HEAD=05ff00b2.
  GATE OVERRODE STATIC AUDIT (good): count_tokens_*/Tokenizer.available are TESTED → NOT vestigial, kept; telemetry/onnx/wiki LIVE, kept.
  DEFERRED (entangled/live — need real untangle code, unsafe for pure-move loop; full specs in archive/RESTORE-LOG.md):
  cache-optimizer cluster ~3.8k (registry←tokenizers, compression_feedback←compression_store), relevance 1k (unconditional
  BM25 import), ml_models, proxy/helpers.py (SSE live), ccr batch+mcp_server 1.9k (mcp_server KEPT — load-bearing if MCP
  retrieve plane built next), benchmarks/verify dedup (needs verify/ run). These are the user's next-decision items.
- SIMPLIFY-AUDIT V2 (2026-06-20): user wants exhaustive 2nd pass, more/different agent types, find ALL hidden fat.
  Built `.claude/workflows/_drafts/simplify-audit-v2.js` (validated): 10 read-only SPECIALIZED lenses (ecc:python-reviewer,
  ecc:rust-reviewer, Explore, code-architect, ecc:type-design-analyzer, config-auditor, ecc:pr-test-analyzer, ecc:comment-analyzer)
  → loop-until-dry (cap 3) → perspective-diverse verify (drop only MAJORITY-refuted, keep uncertain as needs-review) →
  completeness critic → synth. GROUNDED in real tools (ran vulture/ruff/deptry inline → `.claude/runtime/fat-groundtruth.md`).
  Advisor fixes pre-launch: (1) keep uncertain not just confirmed, (2) LOUD dead-lens logging (null≠clean), (3) complexity-lint
  quarantined out of cut tiers. Run wf_c4d4246b-f17 IN FLIGHT.
- RECLAIMED 19GB: rm'd 8 dead `.claude/worktrees/wf_ad2e78a5-*` + `git worktree prune` (handoff's never-done TODO). Engine imports OK.
- Tool signals already in hand: ruff 36 dead-code/unused (7 ERA001 commented-out, ~22 RUF100 unused-noqa) + 431 lint (complexity-heavy,
  NOT for cutting); vulture 2 (conservative); 1 Rust #[allow(dead_code)] live_zone.rs:1299; deptry needs rescope (lens redoes).
- V2 DONE (wf_c4d4246b-f17): 281 agents/12.7M tok/3 rounds/230 survivors/0 net-refuted. Report `lazy-dev-AUDIT-v2.md` (committed 7b999384).
  Tier-1 ~2.6k LOC safe now + 1.45MB stale JSON (verify/*raw_results.json, benchmarks/*_results.json) + 25.6MB gifs(needs-review) +
  ~2.8k doc-cruft lines (wiki HeadroomClient/SharedContext/RollingWindow stale). Tier-2 ~6.6k incl live_zone. Synth caught ERA001
  6/7 false-positives, dual-lockfiles not-dup. My spot-checks confirmed (conftest providers.openai import, artifacts, gifs).
- VENV INCIDENT + FIX: `brew install cloc` removed python@3.13 → .venv interpreter symlink dangled (engine unusable). Root-caused +
  FIXED via `brew install python@3.13` (restored /usr/local/opt/python@3.13). Verified: .venv python 3.13.14, engine OK, recovery 21/21.
- RUST WORKFLOW DONE (wf_c7539e4d-6cf): 47 agents/2.56M tok. 22 findings -> 6 dead targets, ~7,750 net dead Rust LOC.
  Report `lazy-dev-AUDIT-rust.md`. TIER1 SAFE: pipeline/ subtree 4,212 (zero pyo3, zero callers, no headroom-proxy crate) +
  safety.rs 215. TIER2: live_zone.rs 2,899 (hoist private AuthMode enum first, ≠ canonical auth_mode.rs) + recommendations.rs 329 +
  lib.rs FFI 94. REFUTED my dup hypothesis: Rust log/diff/search compressors NOT dead — live via pyclass; only offloads/ that point
  at them are dead. Spot-checked + confirmed (2 crates only, CompressionPipeline 0 pyo3 bridge, only dead mod.rs:60 re-export).
- CONSOLIDATED: `lazy-dev-AUDIT-final.md` written. Repo=63,164 code LOC (cloc, Py 34k+Rust 29k). Cuttable: Tier1 ~7,070 (Rust 4,427 +
  Py 2,640), Tier2 ~9,920 (Rust 3,322 + Py 6,600) = ~17k code ≈ 27% (matched my 25-33% prediction) + ~2.8k doc + 1.45MB/25.6MB disk.
  3 docs: lazy-dev-AUDIT-v2.md (py/doc/disk detail), lazy-dev-AUDIT-rust.md (rust detail), lazy-dev-AUDIT-final.md (merged exec).
- NEXT (user-gated): apply via archive+2-gate loop. Suggested order: Rust Tier1 pipeline/+safety (4.4k cleanest big win) -> Py Tier1 -> untangle tiers.

## BLOAT REMOVAL — PHASE 1 ✅ COMPLETE (2026-06-20)
DONE: teammate tier1-cutter (id a609285, sonnet) cut ALL 6 Tier-1 items green, 0 kept-as-live. ~4,634 code LOC + ~1.2MB.
Commits: fdfd817f (pipeline/ 4,212) · 1573cd92 (safety.rs 215) · dd8bf221 (conftest 206) · 5ea86f3b (compression_store:1097) · e24f7f44 (stale JSON ~1.2MB). HEAD=e24f7f44.
ORCHESTRATOR RE-VERIFIED INDEPENDENTLY (not just trusting report): pytest 519/31, cargo 0-failed all suites, surface 56, recovery 21/21, compress OK. Tree clean. Must-not-touch confirmed intact (proxy/telemetry/onnx/relevance/live_zone.rs/recommendations.rs/src/auth_mode.rs/src/compression_policy.rs/log+diff+search_compressor.rs all present). Empty leftover dirs (pipeline/{offloads,reformats}, memory/{adapters,backends}) rmdir'd.
TIER-2 CAVEAT (from teammate): safety.rs `tool_pair_indices` (tool-pair atomicity) was dead — never called outside own tests. If live_zone dispatcher later needs tool-pair atomicity, re-implement or restore from archive/.
## BLOAT REMOVAL — PHASE 2 (TIER-2 CUTS) ✅ COMPLETE (2026-06-21)
DONE: teammate a609285 cut ~4,967 LOC (4,395 Rust+FFI + 572 Py). Commits f8493718 (live_zone+recommendations bundle) + 8a90716f (batch_processor). HEAD=8a90716f. Everything else RESTORED as live (gate loop sorted empirically, exactly as designed).
ORCHESTRATOR RE-VERIFIED INDEPENDENTLY: pytest 519/31, cargo 0-failed all suites, surface 56, recovery 21/21, compress OK. Tree clean. Removed FFI symbol compress_openai_responses_live_zone = 0 refs anywhere (my stale handoff note was WRONG — it had ZERO callers, not proxy-only; proxy NOT broken). live_zone_tail (proxy memory-inject mode) + live_zone_only (transforms/compression_policy.py flag) are UNRELATED live strings, untouched. must-keep 11/11 intact.
★ GATE-BLINDNESS GUARD EARNED ITS KEEP: ml_models.py G2 stayed GREEN (spacy path env-gated = one of the 31 skips) but guard-grep FIRED on dynamic_detector.py:603 `MLModelRegistry.get_spacy()` live call-site → teammate restored it. Without the whole-tree grep guard, pytest-green would have wrongly cut live code.
TIER-2 RESTORED-AS-LIVE (recon was right "mostly live"): compression_feedback.py (G4 red, CCR feedback loop compression_store:1089), ml_models.py+dynamic_detector.py (functional NER/semantic call-sites, NOT plumbing), relevance/embedding.py+hybrid.py + cache/anthropic+openai+google+registry (all G3 red — TOP-56 PUBLIC SURFACE, not dead).
★ TIER-3 CANDIDATE (NOT dead-code — coordinated API deprecation): the surface-only-live items (relevance EmbeddingScorer/HybridScorer/create_scorer, cache optimizer registry) are published in headroom.__all__+_LAZY_EXPORTS with little/no runtime instantiation. Removable ONLY as a deprecation (drop __all__/_LAZY_EXPORTS + docs + version bump = API break), never as a pure-move cut. Flag to user if they want to shrink the public surface.

CUMULATIVE (Tier-1 + Tier-2): ~9,601 code LOC removed (Rust 8,822 + Py 779) + ~1.2MB artifacts, engine GREEN throughout, fully reversible in archive/.

## ★ ROADMAP RE-SEQUENCED 2026-06-21 (user) — NORTH STAR: codebase BEYOND-PERFECT before any MCP tool creation
New order: (3) v4 audit + apply 4th-pass cuts → (4) HARDEN TESTS to best quality+coverage → (5) proxy→hook+MCP rebuild.
The hook+MCP build is DEFERRED; test-hardening is promoted above it. Doc-only update now (user: "enbart uppdatera docs just nu, vill bara berätta what our next steps are. MCP tool creation will have to wait until the codebase is beyond perfect for usage").
- STEP 3 ✅ AUDIT DONE: v4 feature-reachability audit (wf_c7c82ad2-c7c, 86 agents/3.8M tok/70 findings/0 panel-refuted). Report `lazy-dev-AUDIT-v4.md` committed 0d111fc5. HEADLINE: **the tree is now essentially LEAN.** Static tools clean (cargo 0-warn, vulture 2, ruff 34 nits). Tier-1-safe ~3.65k (~8%) but ~1.9k of it is the proxy-coupled CCR plane (response_handler 896/context_tracker 660/batch_store 313) that dies at the proxy rebuild anyway → genuinely-free non-proxy residual is only ~1.3-1.7k (config.py deadflags ~250, utils.py 13 orphans 110, get_siglip 60, Rust test-only fns ~70, dup verify/measure.py 879=Tier-2). Big numbers (cache-optimizer 2.5k, anchor_selector 770, code_compressor 2k, embedding scorers 533) = TIER-3 PUBLIC SURFACE → API-deprecation+version-bump, NOT free deletes (~6.4k). v4 EARNED ITS KEEP: (a) 2 deptry FALSE-POSITIVES confirmed LIVE — KompressCompressor (ML fallback, torch optional) + ComponentTracker (compression_store+batch_store import it); (b) ★ csv_schema_decoder.py TRAP — 591 LOC looks dead (0 prod callers) but tests/test_ccr_recovery_invariant.py:36 + verify/ import decode_csv_schema_rows to PROVE byte-exact recovery → MUST NOT CUT (I spot-confirmed both). Apply scope (when reached): small config/utils/Rust-test-fn deadflags + the dup measure.py; proxy-coupled CCR plane rides the rebuild; surface-tail is a separate deprecation decision.
- APPLY of v4 cuts is OPTIONAL/SMALL and sequenced per the roadmap (user prioritized test-hardening next). Report-only until user says apply.
- ✅ v4 TIER-1 APPLY DONE (2026-06-23): teammate a609285 cut ~3,900 LOC (18 commits, ef3776e5→5f0ce4b3). ORCHESTRATOR added component_tracker.py (388, TRANSITIVE ORPHAN created by the batch_store cut — 0 importers after) at 2c525cc0. TOTAL THIS ROUND ~4,288. Re-verified independently: pytest 509/31 (519→509 = 10 intentionally-archived compression_units dead tests, 0 live regress), surface 54 (56→54 intentional: −HeadroomMode −CacheOptimizerConfig; models 4→3 −get_siglip), recovery 21/21, cargo 0-failed, compress OK. Tree clean.
  KEY: response_handler.py loud-CCR-miss invariant test was MIGRATED to the live HeadroomMCPServer._retrieve_content path (not dropped) — verified test_ccr_eviction_loud_miss 5/5 green. KEPT (teammate judgment): Rust ccr::compute_key + marker_for (test-only but guard CCR-backend round-trip + marker-format-pinned invariants; no live keying fn to redirect to). Self-inflicted RED (over-deleted StubScorer) caught by cargo + fixed.
  CUMULATIVE all rounds (Tier-1+2+v4): ~13,900 code LOC removed, engine GREEN throughout, fully reversible in archive/.
- ✅ TIER-3 CUT DONE (2026-06-23): teammate a609285 cut 10/11 rows (HEAD 37d440e1) + orchestrator removed inert enable_code_aware flag (f0f12993). Surface 54→40. Re-verified: pytest 443 passed/0 failed/14 skipped (509→443 drop = archived cut-feature tests, ZERO live regression), recovery 21, compress OK, cargo 0-failed. LIVE split-halves all survived (CacheConfig, TokenCounter, relevance base+bm25, csv_schema TRAP, KompressCompressor — all import OK). Row 9 PARTIAL: RelevanceScorerConfig RETAINED (LIVE field SmartCrusherConfig.relevance:269 — removing broke item-2 RED; belongs to Tier-2 SmartCrusherConfig work, not surface cut). code_compressor untangle: content_router CODE_AWARE branch collapsed to KOMPRESS fallback (live invariant code→KOMPRESS 20/20). Current tree = 39,171 code LOC (Py 16,089 + Rust 23,082).
- ★ VERSION BUMP DEFERRED (not done): I was going to bump pyproject 0.25→0.26, but the architecture-clarification turn revealed this is the USER'S FORK (not maintainer) + collaboration-pending + deliverable-shape undecided → unilateral version bump is premature. Version scheme ties to the deliverable-shape decision. Flagged, not executed. Docs still reference retired [code] extra (README:102,231, CHANGELOG:525, wiki/*) — left per docs carve-out.
- ★★ NEW CONTEXT (2026-06-23, reframes everything): user is NOT a headroom maintainer — FORKED the OSS repo, found it over-engineered, building a MINIMALISTIC hook+MCP alternative to the proxy (hook=lazy-dev data-plane, MCP=on-demand full-output retrieve). END GOAL: present to headroom devs for COLLABORATION — both methods side-by-side (their proxy + user's hook/MCP), either as a mentioned repo in their README or two solutions in one repo. So proxy is NOT "deferred for rebuild" — it's DORMANT for the user's method.
- ★ DORMANT PROXY-ROUTE FOOTPRINT (measured 2026-06-23): ~2,850 code LOC is proxy-route, dormant for hook/MCP: (1) PURE proxy transport = headroom/proxy/ 1,847 (helpers.py SSE/streaming/interception 2931-raw + auth_mode.py 262) — deletable now; (2) PROXY-PURPOSE policy layer ~1,010 woven into live engine = compression_policy.py 276 + compression_policy.rs 484 + auth_mode.rs 250 — SmartCrusher reads a per-request compression_policy from kwargs that ONLY THE PROXY feeds; for hook/MCP it's None → default pass-through → auth-mode branches dormant, refactor-to-remove (NOT clean delete); (3) mcp_server _retrieve_via_proxy + httpx tendril (tens of LOC) — un-couple, mcp_server stays. KEY REFRAME: the "Py↔Rust auth-mode parity invariant" I'd protected as untouchable IS ITSELF proxy-route (parity only matters when the proxy applies policy in both langs); never running proxy → that invariant is moot.
- ✅✅ PROXY-ROUTE REMOVED — CODEBASE CLEANUP COMPLETE (2026-06-23). HEAD 63ad09a2. ~5,200 LOC retired (4 gated commits f69c58d3/eb39a07a/b312b2fc/63ad09a2). Tree now 36,605 code LOC (Py 14,007 + Rust 22,598). ORCHESTRATOR RE-VERIFIED: cargo 0-failed, pytest 417/14, surface 40, recovery 21, compress OK. ★ STRONGEST EVIDENCE: git diff of compress-path engine files (smart_crusher/content_router/cache_aligner/compress/pipeline) across the 4 commits = ONLY a cache_aligner DOCSTRING reword (6+/6−), ZERO live logic edited. Retrieve plane verified: 26 tests green (eviction loud-miss + recovery byte-exact). proxy/ + Rust auth_mode.rs/compression_policy.rs gone, 0 live proxy refs, must-keep intact (mcp_server functional CCR-direct, TRAP, Kompress, live compressors). Skipped the planned adversarial verify workflow — the risk it budgeted for (live-logic change) didn't materialize (docstring-only), so it'd re-confirm a verified no-op (lazy-dev).
  TEAMMATE CAUGHT AN AUDIT ERROR: test_adaptive_sizer_parity.py is NOT dead (v4 Tier-0 row wrong) — guards a LIVE Py↔Rust adaptive_sizer parity imported by log_compressor:348 + search_compressor:256. Retained. Also test_ccr_eviction_loud_miss retained (the migrated loud-miss guard).
  mcp_server un-couple: removed _retrieve_via_proxy/_fetch_full_proxy_stats/httpx/DEFAULT_PROXY_URL/proxy_url ctor + create_ccr_mcp_server(proxy_url) param + CLI --proxy-url/--direct; INLINED safe_decode_for_logging (the one live CCR import from proxy.helpers) as stdlib-only _safe_decode_for_logging. Retrieve = local CCR store only, works standalone. This IS the foundation of the user's MCP tool.
- NOTE: HeadroomMCPServer() needs `pip install mcp` (MCP SDK) to instantiate the actual server — env dep, not a regression; the user installs it when building the MCP tool.
- REMAINING (packaging, not code — small, user-gated): version bump (create_ccr_mcp_server signature changed, CLI flags removed = breaking) + CHANGELOG + stale-docs sweep (README/wiki/CHANGELOG still ref proxy/CLI/[code] extra). Deferred per docs carve-out + deliverable-shape.
- ★ PHASE 4 RUNNING (2026-06-23): test-quality-audit workflow (wf_f8febf7f) found 25 CONFIRMED bugs + ~95-test plan (test-hardening-PLAN.md committed 78f8cd97). USER DECIDED: "fix ALL 25 bugs, NO compression-result tradeoff — each fix yields better-or-same compression." Captured compression FLOOR via run_bench (test-baseline.md: logs 92.8%/search 92.2%/repeated 96.5%/disk 50%/multiturn 70.6%/code 0%; needle-recall output-OR-CCR 100%). NEW per-fix gate = run_bench ≥ floor + recall 100% (restore baseline files after). Lossy bugs (#2/#24/#25) → fix by CCR-RECOVERABLE not compress-less.
  ✅ CRITICAL TIER DONE + ORCHESTRATOR-VERIFIED (HEAD 54c3f9d7): 7/7 fixed (0a5cf3c7 #20 JWT, 6ed5110c #2 [0.8,0.9)→CCR-recoverable, 69730be4 #24, a224c56c #25, 32996700 #12 un-invert min_ratio MONOTONE not endpoint-swap, 105e8acb #3 global top-k, 54c3f9d7 #13 de-dup double-EXECUTION). pytest 451/14 (+34 correct-behavior tests), recovery 21, csv fuzz 10/10, ★ run_bench == FLOOR exactly on all 6 datasets + needle-recall 100% (I re-ran it myself — no-degradation constraint HELD). 2 bench-neutral flags: #3 ratio-fidelity (honors target_ratio API = correct), #25 defensive (trigger unreachable via ref Rust encoder, fixed anyway).
  ★★ NEW FIND #26 (orchestrator caught via loop, teammate's "recovery 21" missed it): test_ccr_recovery_invariant.py::test_opaque_blob_recovers_from_output_marker is ~7.5% FLAKY (3/40 runs). PRE-EXISTING (test from 031d4bc6 2026-06-12, 11 days before fixes — NOT a regression; critical tier didn't touch the test file). Uses os.urandom(600) blobs → either a rare RECOVERY HOLE (hash-collision in opaque <<ccr:HASH>> markers OR Py↔Rust store divergence on the byte-exact invariant) OR test non-determinism (violates contract rule 8 — should pin fixed vectors). Root-cause owed in the store/recovery tier. The deterministic gate (-p no:cacheprovider, full suite) is green; flake only surfaces on repeat runs.
  ✅ TIER 2 DONE + VERIFIED (HEAD 922c2290, 10 commits, 40 new tests, suite 487/14): #26 root-caused = TEST NON-DETERMINISM not a recovery hole (200-trial seeded harness: 0/200 collision/divergence; byte-exact invariant HOLDS; fixed via random.Random(0) seed; I confirmed flake gone 0/50). Silent-failure #4/#10/#11/#17/#18 + store-guards #21/#22/#23 fixed. I re-verified: run_bench == floor all 6 + recall 100%, recovery 21, 487 pass. #21 hash floor=6 (matches recovery regex [a-f0-9]{6,} — higher would 404→silent-loss). #23 defensive (only synthetic repro; hardened rebuild-on-no-progress). 2 FOLLOW-UP FLAGS: (a) #4 upstream content_router._try_ml_compressor:1561 re-swallows propagated exceptions = same silent-failure one layer up — narrow it too for system-level loudness; (b) #22 env/default-resolved ttl=0 not guarded (only explicit param).
  ✅✅ ALL 25 BUGS DONE + VERIFIED (HEAD 518b7f29, suite 533/14, recovery 21, run_bench == floor + recall 100% — NO-DEGRADATION HELD END-TO-END across all 25). Final metrics tier: FIXED #19 (savings inversion — real), #1 (score_threshold threaded), #5 (prefix-hash length-framed), #14/#15/#16 (parser metrics), #4-upstream (PARTIAL). DETERMINED-no-fix (correctly, not blind-fixed): #6 (warnings ARE surfaced via class path/logger; only convenience wrapper returns 2-tuple — vetoable), #8 (BY-DESIGN: TOIN gets the 93% token-axis; count==original correct since 0 rows dropped), #7 (MOOT post-proxy-removal: nothing sets compression_policy), #22-env (already guarded by _get_env_default_ttl_seconds), #9 (test-gap: +17 sentinel tests). 89 new mutation-sensitive tests total; coverage 54→59%.
  ★ SCORE.PY AXES ARE MISLEADING HERE (verified): B1_fixed_vector=0 is a DETECTION ARTIFACT (its regex only counts 16+-char string/byte literals; the tests pin list/int/hash literals which ARE fixed-vectors but don't match). A2_private_symbol 166→194 is largely LEGITIMATE internal-invariant coverage (advisor's explicit keep: _adaptive_min_ratio, _redact, store internals, sentinel format). Real quality = mutation-sensitive tests + coverage, both UP. Do NOT chase these axes.
  ★ 2 OPEN DECISIONS (user): (1) #4-upstream — narrow content_router._apply_strategy_to_content:1457 (changes error-handling for ALL 7 strategies) for full silent-failure propagation, OR accept the partial fix (loud at inner boundary, caught at outer net). (2) #6 — keep align_for_cache 2-tuple (warnings available via class path/logger) OR make the wrapper return warnings (breaking API change). My rec: #4 do-it-carefully (no-silent-failures principle, gates protect), #6 keep (don't break API for nothing).
  USER DECIDED (2026-06-23): (1) #4-upstream → SMARTEN 1457 full fix (narrow _apply_strategy_to_content:1457, all 7 strategies, real bugs propagate loud + model-unavailable still passthrough); (2) hardening → FULL ~95-test plan, iterate-to-plateau all 8 modules. Teammate running both: #4-1457 first (engine fix, gated run_bench==floor), then full hardening (test-only, lock current behavior, GENUINE mutation-resistance — real recovery byte-literal pins NOT round-trip, boundary enumeration, parametrize, 0%-cov modules; KEEP legit internal-invariant A2; coverage floor 59%; per-module gate; iterate-to-plateau). NOT chasing the misleading score.py axes.
  ✅✅ PHASE 4 FULLY COMPLETE (2026-06-23, HEAD 8c2d0031):
    PART A (#4-1457 full fix): narrowed _apply_strategy_to_content:1439 except Exception → except MODEL_UNAVAILABLE + re-raise for real bugs. All 7 strategies verified (parametrized test). Layer 2 completes 2-layer fix. Gates: pytest 542/14 (+9 new), recovery 21/21, run_bench == floor all 6 + recall 100% (baseline restored). Commit 7c90d6ea.
    PART B (full hardening): 9 modules, 84 new tests (total tests 533→626):
      csv_schema_decoder (13): B1a literal #24 exact output + parametrized var/col shapes; B1b all 5 col-class boundaries; B1c affix malformed; ordinal-advance + declared-count + None→returns-None boundaries.
      compression_store (14): B1 byte-exact literal store→retrieve; hash floor 5→6 boundary; non-hex rejected; ttl=0→ttl=1 boundary; exists()/retrieve() unknown→False/None; real-producer hash widths.
      content_router (9 in Part A): Part A already adds 9 to kompress_exception tests (all 7 strategies).
      kompress (24): B1a _bucket_count 15 boundary transitions; B1b 3 structure_hash literals; K-9 max-score semantics; K-10 OR-mask semantics; K-chunk exact output; n_words 9→10 boundary.
      cache_aligner (10): B1a 2 exact hash literals (current fixed formula); enabled=False boundary; idx0 never dropped; order preserved; 3 parametrized distinct-hash pairs.
      smart_crusher: already at plateau from bug-fix phase (sentinel literal, TOIN count, policy-moot).
      mcp_server: already at plateau from bug-fix phase (savings_percent exact literal, live-entry query boundary).
      parser: already at plateau from bug-fix phase (waste-signal metric literals, None-text TypeError).
      cache/base (23): B1a all 12 default field values; 4 CacheStrategy enum values; field-override changes observable value; factory isolation.
    SCORE.PY: B1_fixed_vector 0→5 (undercounts — regex only matches 16+char; real pins are int/hash/list literals); D2_param_ratio 0.017→0.043; A2 166→201 (legitimate internal-invariant coverage kept).
    COVERAGE: TOTAL 59% (floor held); cache/base 0%→covered; kompress 47% (floor was 13%).
    MUTATION-SENSITIVITY VERIFIED: each module spot-checked (literal pin fails on formula change, boundary test fails on off-by-one).
  NOTE on content_router coverage module gap: test_content_router_kompress_exception.py (Part A) added 9 tests but score.py notes content_router already had tests for all plan items (#10,#11,#12,#13) from bug-fix phase — content_router at plateau.
  (hist) PROGRESS: 15/25 original bugs fixed (7 critical + 8 tier-2) + #26 bonus. REMAINING = metrics/observability tier (10: #1,#5,#6,#7,#8,#9,#14,#15,#16,#19 — lowest risk, compression-output-neutral; #7 may be auto-resolved by proxy removal since compression_policy is gone/always-None; #9 is a TEST-GAP add-tests not a fix; #19 confirm-by-design naming first) + the 2 follow-ups. THEN the non-bug mutation-resistance hardening (re-score first — the 74 bug-fix tests already moved B1/D2 a lot; only fill remaining axis/coverage gaps + the 0%-cov modules).
  (hist) EXECUTION: teammate a609285 did CRITICAL TIER first (7: #20 JWT-leak security, #2/#24/#25 recovery silent-loss, #12 inverted-min_ratio + #3 per-chunk-ratio quality, #13 strategy-chain). Per-bug: confirm-not-by-design (Cluster G precedent; #12/#19 esp) → fix → correct-behavior regression test → gates (pytest 417 + recovery 21 + Py↔Rust parity + run_bench floor). Commit per bug. ON COMPLETE: I verify the floor held + review by-design calls, then greenlight remaining tiers (silent-failure #4/#10/#11/#17/#18 + store-guards #21/#22/#23 + observability/metrics #1/#5/#6/#7/#8/#9/#14/#15/#16/#19) + the non-bug B1/boundary/coverage hardening.
  ★ TEST-HARDENING is a SEPARATE track from fixes: fixed areas get correct-behavior tests; unchanged surface gets B1/boundary/coverage locking current behavior. Bug-fixing was NOT pre-authorized as test-hardening — user explicitly authorized it with the no-degradation constraint.
- PHASE 5 (after): build the hook (productized) + 2-tool MCP (set_compression + retrieve — retrieve already CCR-direct standalone). Needs `pip install mcp`.

  (prev) ✅ DELIVERABLE-SHAPE DECIDED (2026-06-23): user chose "fristående minimal fork" → DELETE proxy-route. Teammate ran the removal. Order: (1) mcp_server.py un-couple (remove _retrieve_via_proxy+httpx, keep CCR-direct retrieve functional) → (2) compression_policy.py + parity tests (test_compression_policy{,_toin_gate}.py + test_adaptive_sizer_parity.py) archive → (3) proxy/ dir delete → (4) Rust auth_mode.rs + compression_policy.rs + cargo test. RECON (de-risked): engine reads compression_policy ONLY via kwargs.get() duck-typed, 0 setters anywhere → always None → policy classes removable without breaking reads; AuthMode 0 use outside proxy/policy/tests; Rust twins 0 pyo3-bridge refs (test-only). LIVE-ENGINE touch → restore on ANY behavior change (compress/recovery = guards). REMOVES the Py↔Rust auth-mode parity (it was proxy-route). ON COMPLETE: re-verify + run adversarial verify workflow (no hidden policy setter, recovery/cache-prefix intact, mcp retrieve standalone, compress unchanged).
  (prev) ★ DELIVERABLE-SHAPE DECISION (was pending, governs proxy-delete + version): (a) standalone minimalistic fork → DELETE the ~2,850 proxy-route (lighter); (b) side-by-side in their repo (collab vision) → KEEP proxy/ as THEIR method, just don't wire it into the user's hook/MCP path. This decides whether proxy/ is deleted + the version scheme.

  (prev) ★ TIER-3 CUT RAN (2026-06-23, user decided "kör" — go internal-engine-only, retire the published-library API; breaking change ACCEPTED): teammate a609285 cutting ALL 11 Tier-3 rows (~6.4k). KEEP-THE-LIVE-PART splits flagged: cache/base.py keep CacheConfig(69)/cut BaseCacheOptimizer(254); providers/base.py keep TokenCounter(18)/cut Provider(34)+retype pipeline param; relevance keep base+bm25/cut embedding+hybrid. code_compressor cut TOGETHER w/ compression_summary + content_router CODE_AWARE branch untangle. User reasoned it through: Tier-3 = old proxy-arch + dead Rust-duplicated Python, ZERO connection to the MCP/hook future (retrieve=CCR store, set_compression=RoutingPolicy, hook=ContentRouter — all kept). ON COMPLETE: re-verify gates + ★ I do pyproject version bump (0.25.0 → breaking) + CHANGELOG note (published-API removal).
  (prev) ★ TIER-3 SURFACE DECISION (was pending): ~6.4k unused PUBLIC exports (anchor_selector 770, cache-optimizer cluster 2.5k, code_compressor 2k, embedding scorers 533, compression_cache 315, Provider ABC, observability Protocol). CRITICAL: headroom-ai is a PUBLISHED PyPI package (pyproject v0.25.0, name=headroom-ai, docs site headroom-docs.vercel.app) → these are PUBLIC API contract, cutting = breaking change for pip-install users (NOT internal dead-code). No external consumer found in Biljakten (uses only headroom.transforms via hook). DECISION = keep published-library identity (KEEP surface) vs go internal-engine-only via hook+MCP (free to slim, major version bump). Asked user.
- STEP 4 (NEW, user-prioritized): HARDEN TESTS. Iterate suite to best QUALITY + coverage (coverage = FLOOR not goal). Use installed `test-quality-tools:test-quality` skill + scorecard: mutation-resistance, anti-fragility rules, boundary tests for every </<=/>/>=/==/!=, real-I/O fixtures over mocks, contract-named tests; iterate-to-plateau per module. REASONING (user): better tests → find+improve codebase → plausibly BETTER COMPRESSION (the eval `break` pass already surfaced silent-loss holes this way). Hit the hard-invariant surfaces first (CCR recovery, Py↔Rust parity, cache ordering, compress() route).
  ★ SKILL LIMITATIONS (verified by reading the skill files 2026-06-21 — set expectations, do NOT expect "perfect"):
    (1) Stops at PLATEAU, not perfection — 3 dry rounds OR zero-violations+all-boundaries-tested; the dry-rounds arm usually
        fires first → stops when the MODEL can't improve further. "Quality" is relative (better-than-start), not absolute.
    (2) Best axes (boundary B.2 / real-I/O B.3 / contract-naming E.3 / tautology A.3) are MODEL JUDGEMENT, not auto-counted.
    (3) NO real mutation engine — "mutation-resistance" is a proxy (boundary + pinned literals + break-the-behavior gate). Monotonic (revert-on-no-gain, never degrades).
    (4) ★ NO RUST PROFILE in score.py (python/js/go/kotlin/swift only). ~Half the codebase (Rust 23k) gets ZERO auto-scoring; Python is the only validated profile.
  TWO-TRACK because of the Rust gap: Python → skill + score.py loop; Rust → apply the contract principles by hand (+ optional `cargo-mutants` for a real mutation score on the engine core).
- STEP 5 (DEFERRED until codebase beyond-perfect): PROXY→HOOK+MCP REBUILD. proxy→DELETE; extract live SSE utils (proxy/helpers.py parse_sse_events/safe_decode → ccr/sse_parser.py) FIRST; build hook (data-plane, productized Biljakten-style) + 2-tool fastmcp (set_compression + retrieve→CCR direct, un-couple mcp_server.py:472 _retrieve_via_proxy). Engine gates BLIND to transport → needs NEW functional verification (not the archive loop). DO NOT START until Step 4 done.

(historical) Phase-2 brief sent to teammate a609285 via SendMessage.
★ ORCHESTRATOR RECON REFUTED v2-audit's Tier-2 Python (~6.6k "cuttable" is mostly LIVE/import-woven — v1 was right to defer):
  - LIVE (will restore): relevance/bm25.py+base.py (compression_store:258 instantiates BM25Scorer), compression_feedback.py (CCR feedback-loop, heavy use in compression_store), cache-optimizer cluster anthropic/openai/google/registry (woven into cache/__init__ 56-surface + tokenizers/__init__→registry; likely test-pinned).
  - VESTIGIAL CANDIDATES (0 runtime caller found, gate-verifiable): relevance/embedding.py+hybrid.py (smart_crusher uses RUST HybridScorer crate not these), ccr/batch_processor.py (BatchProcessor never instantiated, only ccr/__init__:24 re-export), models/ml_models.py + cache/dynamic_detector.py (no compress-path caller; ml_models lazy-imported inside dynamic_detector funcs).
  - Realistic Python yield ~1-2k, NOT 6.6k. Real Tier-2 win = Rust live_zone bundle ~3.3k.
★ TEAMMATE TIER-2 SCOPE: (A) Rust live_zone.rs 2,899 + recommendations.rs 329 + lib.rs FFI ~94 + mod.rs re-exports + co-located cargo tests (archive live_zone+recommendations together; private AuthMode≠canonical src/auth_mode.rs which is KEPT; hoist only if cargo demands). live_zone is proxy-only → cutting it makes proxy/ Python reference a removed FFI = EXPECTED/OK (proxy deleted next step, archived). (B) Python cluster attempt-all in order embedding/hybrid→batch_processor→ml_models/dynamic_detector→cache-optimizer cluster.
★ NEW HARDENING vs Tier-1 (gate-blindness guard): for every GREEN cut, grep whole tree (excl archive/) for stray refs before commit — pytest doesn't cover every path; stray ref → restore.
★ MUST-KEEP: ccr/mcp_server.py (MCP retrieve plane), relevance/bm25+base, compression_feedback (if red), proxy/ Python files (defer — but DO remove their Rust FFI per bundle A).
ON TEAMMATE COMPLETE: re-verify gates myself, review KEPT(live) section, then user gives PROXY-REBUILD instructions (step 3): proxy→hook(data-plane)+2-tool fastmcp(set_compression+retrieve→CCR direct, un-couple mcp_server.py:472 _retrieve_via_proxy). Extract live SSE utils (proxy/helpers.py parse_sse_events/safe_decode → ccr/sse_parser.py) BEFORE deleting proxy.

(prev) AWAITED USER Tier-2 instructions (proxy→hook+MCP rebuild + untangle cuts). Reuse same teammate via SendMessage to:'a609285ce32cd6df9'.

--- (historical, Phase 1 while-running notes below) ---
## BLOAT REMOVAL — PHASE 1 RAN (2026-06-20)
Sonnet teammate (background Agent, name=tier1-cutter) ran Tier-1 cleanup. Method = proven archive+2-gate loop:
archive (git mv → archive/) → rebuild if Rust (maturin) → gates (cargo test + pytest 519/31 + surface-walk 56 +
recovery 21/21 + compress() round-trip) → green=keep-removed+commit / red=dig deeper (fixable small refactor? do it;
genuinely live? restore + report caveat). Sequential, one item at a time, commit per green batch.
- TEAMMATE TIER-1 SCOPE (EXCLUDES proxy — proxy handled by the hook+MCP rebuild in Tier 2): Rust pipeline/ subtree
  (4,212, drop mod.rs:32,59-63 re-exports) + safety.rs (215, drop mod.rs:34,65); Python conftest dead fixtures (193,
  incl nonexistent providers.openai import), commented-out code compression_store.py:1097, stale JSON results
  (verify/*raw_results.json + benchmarks/*_results.json ~1.45MB), empty phantom dirs memory/{adapters,backends}.
- MUST-NOT-TOUCH (verified live/invariant): ALL proxy/ (rebuild later), telemetry/, onnx_runtime.py, auth_mode.rs,
  compression_policy.rs, SmartCrusher (crusher/compaction/analyzer/planning/orchestration/anchor_selector), ccr/,
  tokenizer*, cache_control.rs, log/diff/search_compressor.rs (live via pyclass — dup hypothesis REFUTED), wiki/,
  feature-gated rust, live_zone.rs + recommendations.rs (those are Tier 2). NO complexity-lint churn.
- ON TEAMMATE COMPLETE: review his report (esp. anything he KEPT + caveats), then user gives Tier-2 instructions
  (which include the proxy→hook+MCP rebuild). Reuse the SAME teammate via SendMessage for Tier 2.
