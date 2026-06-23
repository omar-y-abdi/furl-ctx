# Phase-4 Test-Hardening PLAN — Headroom (Python track)

> Baseline ref: HEAD `90b1df8a`, branch `verify/phase2-audit-report`. Source of truth: `/Users/k/dev/headroom/.claude/runtime/test-baseline.md`.
> **REPORT-ONLY.** The teammate implements this sequentially (single suite, no parallel pytest/git race). Excludes `archive/`, `target/`, `.venv*`.

## Headline

- **~95 new tests planned** (kompress 22 from its audit; the other 7 modules estimated bottom-up: 1 mutation-sensitive test per real bug + B1 literal vectors + boundary tests). Treat as an estimate-floor, not a quota — stop at per-module plateau.
- **25 REAL bugs confirmed** across 8 modules (verdict=`real`). This validates the thesis: *harder tests find bugs → which expose where compression silently loses or misreports data → which is the lever for trustworthy compression.* The eval-`break` pass already found silent-loss holes this way; this pass found 25 more.
  - **+1 false-alarm** (kompress multi-token max/OR semantics — code is CORRECT, it is a **test gap**, not a bug) and **+1 uncertain** (csv-schema malformed `__affix:` preamble — mechanism proven but unreachable via the reference Rust formatter). Neither counts in the 25; both are flagged `needs-review` below.
- **score.py axes to move** (baseline → target direction):
  - **B1_fixed_vector: 0 → UP** — ★ the #1 lever. ZERO tests currently pin a literal. Every module section below LEADS with B1 (recovered-byte / compressed-output / hash literals). Behavior-SAFE: pinning a literal reinforces the recovery invariant with zero risk.
  - **D2_param_ratio: 0.017 → UP** — boundary/role/threshold cases land as `@pytest.mark.parametrize`, not unrolled bodies.
  - **A2_private_symbol: 166 → DOWN, SELECTIVELY** — route through public paths where one reaches the same behavior (`decode_csv_schema_rows`, `store()`/`retrieve()`, `align_for_cache`, MCP tool-output dicts, `parser` public fns). ★ **KEEP** internal access for invariants with NO public path: Py↔Rust hash parity, the `<<ccr:HASH>>` sentinel format, anchor-selector internals. Never delete internal-invariant coverage to win the axis.
  - **A1/A4/A5 (4/4/1): hold or down** — prefer fixed literals over recomputed-expected / substring asserts.
- **Coverage = NON-REGRESSION FLOOR only** (TOTAL 54%; per-module floors below). Coverage is not the goal; the axes are.

## Two-track reminder

This plan is **PYTHON only** (score.py-validated profile, lang=python). **Rust hardening is a separate hand pass** (no Rust profile in score.py; optional `cargo-mutants`) — out of scope here. Several bugs have a Rust counterpart (csv-schema formatter, Py↔Rust hash parity); the Python tests pin the Python-side contract only.

## ★ Critical framing: test-hardening LOCKS CURRENT BEHAVIOR — it is NOT a bug fix

- **Do NOT instruct the teammate to fix any bug.** A fix changes invariant-bound engine behavior (recovery / Py↔Rust parity / cache-prefix / TOIN signal) which is **NOT pre-authorized**. Recall Cluster G: the eval `break` looked like silent-loss but was **by-design**. Bug-fixing and test-hardening are **separate tracks**.
- **A real-bug test pins what the engine does TODAY.** Example: assert `savings_percent == 35` (the inverted value), assert `cache_key is None` AND the last 3 words are absent for a ratio in `[0.8, 0.9)`. These PASS today (satisfying the all-pass gate) and will FAIL the day someone changes the behavior — surfacing the change for an explicit decision. A test asserting the *hoped-for* value (`savings_percent == 65`) would fail today and break the green gate — another reason the tracks are separate.
- The 25 real bugs are **SURFACED TO THE USER as a fix / defer / intended-behavior DECISION** (table below). The plan proceeds independently of that decision.

## CONFIRMED BUGS — fix / defer / intended-behavior decision (USER DECIDES; teammate only locks current behavior)

| # | bug | module | location | repro (one-liner) | invariant at risk |
|---|---|---|---|---|---|
| 1 | `score_threshold` config dead on default ONNX-CPU path | kompress_compressor.py | :372, :1216-1217 | `KompressConfig(score_threshold=0.7)` ⇒ identical output to default on ONNX | config field must affect behavior (Rule 4) |
| 2 | 512-token truncation ⇒ unrecoverable silent word loss when ratio ∈ [0.8,0.9) | kompress_compressor.py | :814-820, :898, :1263 | 20 words, 17 visible ⇒ ratio 0.85, `cache_key is None`, 3 words gone | CCR recovery byte-exact |
| 3 | per-chunk `target_ratio` rounding ⇒ fewer words than global ratio | kompress_compressor.py | :1106 (& :860) | 12 words, chunk=5, ratio=0.5 ⇒ 5 kept (0.417), not 6 | caller-visible `target_ratio` semantics |
| 4 | blanket `except Exception` ⇒ any model/tokenizer bug becomes silent passthrough | kompress_compressor.py | :922 | stub `get_keep_mask` raises ⇒ `compressed==original`, ratio 1.0 | callers can't distinguish bug from intentional passthrough |
| 5 | `stable_prefix_hash` collision: 1 sys-msg w/ `\n---\n` vs 2 sys-msgs | cache_aligner.py | :314-319 | both ⇒ hash `567988c630975a24`, `prefix_changed=False` | hash uniquely identifies system-prompt set (observability) |
| 6 | `align_for_cache` always runs `apply()` (bypasses `should_apply`), warnings discarded | cache_aligner.py | :367-388 | volatile content ⇒ caller gets `(messages, hash)`, no warnings | public-API contract: "runs detection" yet caller sees nothing |
| 7 | thread-local `compression_policy` leaks from `apply()` into later `crush()` | smart_crusher.py | :1009 (set), :606-612 (gate) | `apply(read_only)` then `crush()` ⇒ 0 TOIN recordings | TOIN learning-signal integrity |
| 8 | TOIN `compressed_count` = original_count for lossless csv-schema / markdown-kv | smart_crusher.py | :643-651 | 60 dicts, 93% bytes saved ⇒ TOIN sees orig=comp=60 (0% reduction) | TOIN count-based learning signal |
| 9 | `is_ccr_sentinel` / `strip_ccr_sentinels` public API entirely untested | smart_crusher.py | :85-103 | (zero tests; mutation of `CCR_SENTINEL_KEY` survives) | correct TOIN `compressed_count`; downstream sentinel iteration |
| 10 | per-request runtime options dropped for multi-message `apply()` (TLS vs worker threads) | content_router.py | :1969-1977 (set), :2294-2309 (workers) | ≥2 cache-miss msgs + `force_kompress=True` ⇒ workers read `False` | per-request options must reach every worker compression |
| 11 | empty-output guard restores content but leaves `routing_log` w/ phantom savings | content_router.py | :1034-1050 | compressor returns `""` ⇒ `tokens_saved=N`, ratio 0.0 recorded | empty-guard must report passthrough (saved=0, ratio=1.0) |
| 12 | adaptive `min_ratio` INVERTED: high pressure ⇒ stricter, rejects MORE | content_router.py | :2022-2029 | ratio 0.70 kept at pressure 0.0, rejected at pressure 1.0 | high pressure must accept marginal compressions, not reject |
| 13 | `strategy_chain` double-appends `'kompress'` (inner + outer fallback) | content_router.py | :1327-1338, :1416-1423 | `compress('[1,2,3]')` ⇒ `['smart_crusher','kompress','kompress']` | strategy_chain: each strategy at most once |
| 14 | `whitespace_tokens` always 0 (normalization formula never yields savings) | parser.py | :116-119 | 35-space input ⇒ `whitespace_tokens==0` (metric only) | `WasteSignals.whitespace_tokens` accuracy |
| 15 | HTML comment double-counted in `html_noise_tokens` | parser.py | :100 | `<!-- c -->` ⇒ 52 chars, should be 26 (metric only) | `html_noise_tokens` no double-counting |
| 16 | JSON char-gate vs token-gate mismatch + greedy unanchored merge | parser.py | :21, :124-127 | 3 sub-threshold JSONs + prose ⇒ merged `json_bloat_tokens=536` (metric only) | `json_bloat_tokens` accuracy |
| 17 | `None` text in content block ⇒ `TypeError` (silently disables waste-signals) | parser.py | :167, :70, :443 | `{'type':'text','text':None}` ⇒ TypeError; pipeline ⇒ `waste_signals=None` | `parse_message_to_blocks` total over conformant shapes |
| 18 | search-no-results ⇒ false eviction error for a LIVE entry | ccr/mcp_server.py | :330-373 | live entry + `query='zzz_nomatch'` ⇒ `error='no longer retrievable'` AND `status='available'` | loud, cause-honest miss; live entry never errors |
| 19 | `savings_percent` inverted (reports retention) | ccr/mcp_server.py | :308-310 | 65% reduction ⇒ `savings_percent==35` while `tokens_saved==65` | sibling-field self-consistency |
| 20 | `_redact_retrieval_log_payload` leaks JWT in plain-text `Authorization` header | cache/compression_store.py | :130-132 | `Authorization: Bearer <JWT>` ⇒ token unredacted in log | no credential in `headroom_retrieve` log events |
| 21 | `store(explicit_hash='a')` (1 char) accepted — no min-length guard | cache/compression_store.py | :322-326 | 1-char hex key stored; collidable / overwrite | explicit_hash entropy vs marker contract |
| 22 | `store(ttl=0)` ⇒ immediately-expired entry leaks until next `store()` | cache/compression_store.py | :350 | `ttl=0` ⇒ entry in backend+heap, expired, no error | backend must not hold immediately-expired entries |
| 23 | `_evict_if_needed` may not evict ⇒ store exceeds `max_entries` (stale-heap path) | cache/compression_store.py | :917-936 | stale heap ts + ratio<0.5 ⇒ store at 3 with max=2 | store must never exceed `max_entries` |
| 24 | empty-string sole-var-column row dropped + arith-fold misaligns all later rows | csv_schema_decoder.py | :443 | `[3]{seq:int=0+1,msg:string}\nhello\n\nworld` ⇒ 2 rows, `world` seq wrong | lossless decode byte-exact |
| 25 | const+arith-only table (zero var cols) ⇒ always `[]` (total silent loss) | csv_schema_decoder.py | :380-388, :442 | `[3]{x:int=5,seq:int=0+1}` ⇒ `[]` | reference-decoder contract for any conformant producer |

### needs-review (NOT in the 25; do NOT add a fix-asserting test)

| bug | module | verdict | how to handle |
|---|---|---|---|
| multi-token max-score / any-True OR semantics | kompress_compressor.py | **false_alarm** — code is CORRECT | Add as a **test gap** filler: a multi-subword stub that pins CORRECT current behavior (max wins, OR keeps). Frame = locks correct behavior; kills the `s > → s <` min-mutation. (See kompress §, test K-9/K-10.) |
| malformed `__affix:` (<2 segs) processed as data row | csv_schema_decoder.py | **uncertain** — unreachable via reference Rust formatter | Add a defensive **decoder-contract** regression test pinning current output; flag in the section that the reference formatter cannot emit the trigger (alt-producer only). User decides whether to treat as a real defect. |

## NOT-RUN lenses (coverage gap, NOT "clean")

- **`headroom/cache/base.py` (`CacheConfig`)** — baseline high-value target #7, **0% cov**, and ZERO test references confirmed (grep). It received NEITHER a full audit lens NOR a bug entry. This is an un-audited surface, not a verified-clean one. → Add a minimal config-field-affects-behavior + B1 default-value pass (see §9); flag for a future dedicated audit lens.
- **Lens-coverage gap (meta):** a full audit JSON was delivered ONLY for `kompress_compressor.py`. The other 7 modules are **bug-hunt-only** — their contract-violation / boundary-gap inventories were not produced. The per-module plans below for those 7 are **bug-derived + baseline-cov** (not invented audit inventories). A full audit lens on cache_aligner / smart_crusher / content_router / parser / mcp_server / compression_store / csv_schema_decoder remains owed.

---

## PER-MODULE HARDENING PLAN — ranked by bug VALUE (invariant severity, not bug count)

Teammate works this list **TOP-DOWN, iterate-to-plateau per module**. **Gate per module:** full pytest green + recovery 21 still pass + module coverage ≥ floor + every new test mutation-sensitive (must fail when the pinned behavior breaks). NEVER weaken a test to win an axis. NEVER chase coverage through privates. REPL-verify any library/engine assumption.

> Ranking rationale: recovery/security/context-safety hitters lead. `parser` has 4 bugs but every one is scoped "compression output unaffected" (metrics/diagnostics only) → ranks BELOW its count.

### 1. `headroom/transforms/csv_schema_decoder.py` — recovery byte-exact (HARDEST invariant) · cov: not captured in baseline (recovery-surface module)
- **Bugs:** #24 (silent row-drop + arith misalignment), #25 (zero-var-col ⇒ `[]`), needs-review `__affix:` (uncertain).
- **B1 + boundary FIRST (public path — `decode_csv_schema_rows`, A2-friendly):**
  - **CD-B1a** `@parametrize` pin literal decode outputs: `decode_csv_schema_rows('[3]{seq:int=0+1,msg:string}\nhello\n\nworld')` == `[{'msg':'hello','seq':0},{'msg':'world','seq':1}]` (pins the CURRENT buggy 2-row / shifted-seq output — locks bug #24).
  - **CD-B1b** `decode_csv_schema_rows('[3]{x:int=5,seq:int=0+1}')` == `[]` and `decode_csv_schema_rows('[2]{seq:int=10+5}')` == `[]` (locks bug #25 total-loss); contrast row: `[2]{x:int=5,y:int=10}` == `[{'x':5,'y':10},{'x':5,'y':10}]` (the degenerate path that WORKS — guards the boundary between var-only/const-only/arith-present).
  - **CD-B1c** needs-review: `[1]{col:string^}\n__affix:col=prefix_only\nactual_row` ⇒ pin current `[{'col':'__affix:col=prefix_only'},{'col':'actual_row'}]`; comment that reference formatter can't emit this (alt-producer only).
- **est new tests: 6–8** (3 B1 literal blocks + parametrized boundary cases around var/const/arith column-class combinations + empty-line vs malformed-line ordinal-increment edge at :443 vs :447).

### 2. `headroom/cache/compression_store.py` — recovery + SECURITY (JWT) · cov: not captured in baseline (recovery-surface; 21 recovery tests exist)
- **Bugs:** #20 (JWT leak), #21 (1-char hash), #22 (ttl=0 leak), #23 (over-capacity eviction).
- **B1 + boundary FIRST (public path — `store()`/`retrieve()`/`_redact_…`; SECURITY literal leads):**
  - **CS-B1a (security)** pin `_redact_retrieval_log_payload('Authorization: Bearer eyJ…sig')` == `'Authorization: [REDACTED] eyJ…sig'` (locks bug #20 current leak; the JWT literal IS load-bearing). Contrast: JSON-quoted header redacts correctly. ★ This is the test the user most wants to see flip the day a fix lands.
  - **CS-bnd-hash** `@parametrize` `store(explicit_hash=…)`: `'a'` (1 char) accepted today; pin contract widths `{12,24}` referenced by `tool_injection.py` as the documented anti-spoof guard (boundary at 1 vs 12 vs 24).
  - **CS-bnd-ttl** `store(ttl=0)` ⇒ entry present in backend + `retrieve()` returns `None` + entry still in backend after `exists(clean_expired=False)` (locks #22 leak-until-next-store); boundary vs `ttl=1`.
  - **CS-cap** reproduce #23: `max_entries=2`, stale-heap timestamp + stale-ratio < 0.5 ⇒ assert count==3 (current over-capacity behavior). ★ KEEP internal (`_eviction_heap`/`_stale_heap_entries` have no public path — legitimate A2).
- **est new tests: 7–9** (1 security B1 + parametrized hash-width + ttl boundary + capacity-overflow + a recovery round-trip B1 literal that re-affirms byte-exact `retrieve()`).

### 3. `headroom/transforms/content_router.py` — routes EVERY compress(); context-safety · cov: not captured in baseline (dispatch core)
- **Bugs:** #10 (TLS worker-thread option drop), #11 (phantom-savings metrics), #12 (inverted `min_ratio` — agent overflows exactly when context tightest), #13 (`strategy_chain` double-append).
- **B1 + boundary FIRST:**
  - **CR-B1a** pin `compress('[1,2,3]').strategy_chain == ['smart_crusher','kompress','kompress']` (public path; locks #13 current double-append literal).
  - **CR-bnd-minratio** `@parametrize` the inverted threshold (the load-bearing math): ratio 0.70 ACCEPTED at `context_pressure=0.0`, REJECTED at `1.0`; pin `min_ratio = 0.85 - 0.20*pressure` at pressures {0.0, 0.5, 1.0} (locks #12). Boundary at ratio==min_ratio (strict `<`).
  - **CR-empty-guard** stub compressor returns `""` ⇒ after guard `compressed==content` BUT `tokens_saved==N` and `compression_ratio==0.0` and observer received `compressed_tokens=0` (locks #11 phantom metrics).
  - **CR-tls** ≥2 cache-miss msgs + `force_kompress=True` (and separately `target_ratio=0.5`) ⇒ worker reads default `False`/`None` (locks #10). May need internal property read in worker thread — KEEP if no public observation path; prefer asserting the observable downstream effect (TOIN write occurred despite `toin_read_only=True`) where possible to lower A2.
- **est new tests: 8–10** (B1 strategy-chain + parametrized min_ratio + empty-guard metrics + TLS-drop across the 4 symptoms).

### 4. `headroom/transforms/kompress_compressor.py` — LIVE default text/code compressor; [0.8,0.9) unrecoverable loss · cov 13% (floor) · audit est 22
- **Bugs:** #1 (dead `score_threshold`), #2 (truncation silent loss [0.8,0.9)), #3 (per-chunk ratio), #4 (blanket-except passthrough). Plus needs-review false-alarm (multi-subword max/OR).
- **REQUIRED isolation fixture (from audit):** autouse fixture snapshotting/restoring `_kompress_cache` and `_execution_semaphores` module globals per test — without it, injected stubs leak into real-model tests.
- **B1 FIRST (pure deterministic functions — REPL-pinned literals, A2 internal-but-legitimate / no public path):**
  - **K-B1a** `@parametrize` `_bucket_count`: `(0,'0'),(1,'1-2'),(2,'2-4'),(7,'4-8'),(8,'8-16'),(15,'8-16'),(16,'16-32'),(255,'128-256'),(256,'256-512')`. Kills bit_length arithmetic mutations.
  - **K-B1b** `_kompress_content_signature.structure_hash`: `'hello world this is a test string for kompress'` ⇒ `'8666dafa67919f1fd5313c7f'`; `'ERROR: fatal exception occurred traceback'` ⇒ `'aad3962be514bf419b181f27'` AND `has_error_like_field is True`; `''` ⇒ `'c82c95f4649398549cb57165'`. Kills shape-string formula mutations.
- **Boundary (parametrized, lifts D2):** the `n_words < 10` boundary at 9 vs 10 for EACH of `compress` (:783), `compress_batch` (:1030), `apply` (:1255); CCR `ratio < 0.8` (:898) at 0.79 vs 0.80; apply `ratio < 0.9` (:1263) at 0.89 vs 0.90; `target_ratio=0.0` ⇒ `max(1,…)` keeps ≥1.
- **Bug-lock tests:**
  - **K-2** TruncatingTokenizer(17/20) + keep-all + `enable_ccr=True` ⇒ `compressed_tokens==17`, `cache_key is None`, words 17/18/19 absent, `apply()` applied (ratio 0.85 ∈ [0.8,0.9) unrecoverable). Second arm at 14/20 (ratio 0.7<0.8) ⇒ `cache_key is not None`. Pins both sides of the CCR gap (#2).
  - **K-3** chunk=5, ratio=0.5, 12 words ⇒ `compressed_tokens==5` (per-chunk, not 6) (#3).
  - **K-4** stub `get_keep_mask` raises ⇒ `compressed==original`, ratio 1.0 (#4).
  - **K-1** stub ONNX model, `score_threshold=0.7`, scores 0.6 ⇒ all kept (hardcoded `>0.5` wins, threshold ignored) (#1).
  - **K-9 (false-alarm / test-gap, locks CORRECT behavior):** `word_ids=[None,0,0,1,1,None]`, scores `[_,0.9,0.1,0.3,0.4,_]` ⇒ word0 uses 0.9 not 0.1, word1 uses 0.4 not 0.3 (max wins). **K-10:** mask `[_,True,False,False,True,_]` ⇒ BOTH word0 AND word1 kept (OR semantics). Kills `s > → s <` min-mutation.
  - **K-chunk** 3-chunk offset: selective-keep stub ⇒ exact output `'zero two four six eight ten'` (kills `wid+chunk_start` / `sorted(kept_ids)` mutations).
- **est new tests: 22** (matches audit estimate).

### 5. `headroom/transforms/cache_aligner.py` — cache-prefix ordering invariant · cov 16% (floor)
- **Bugs:** #5 (`stable_prefix_hash` collision), #6 (`align_for_cache` discards warnings).
- **B1 FIRST (public path — `align_for_cache` / `CacheAligner.apply`, A2-friendly):**
  - **CA-B1a** pin collision literal: `apply([{'system':'a\n---\nb'}]).cache_metrics.stable_prefix_hash == '567988c630975a24'` AND `apply([{'system':'a'},{'system':'b'}])` ⇒ same hash ⇒ `prefix_changed is False` on the structural change (locks #5). The hash literal is load-bearing.
  - **CA-6** `align_for_cache([{'system':'UUID: 550e8400-…'}])` returns `(messages, hash)` with NO warnings surfaced even though detection ran (locks #6 discard-warnings contract). Boundary: default `CacheAlignerConfig(enabled=False)` still runs `apply()` (bypasses `should_apply`).
- ★ KEEP any prefix-ordering / idx0-never-dropped internal assertions — that invariant has weak public observability and is the module's reason to exist.
- **est new tests: 5–7** (B1 collision pair + warnings-discard + enabled-flag boundary + an idx0-ordering lock).

### 6. `headroom/transforms/smart_crusher.py` — core engine, TOIN loop · cov 70% (floor — highest, keep it)
- **Bugs:** #7 (TLS policy leak), #8 (lossless TOIN count = original), #9 (sentinel API untested).
- **B1 FIRST (public path — `is_ccr_sentinel`/`strip_ccr_sentinels` are PUBLIC, currently zero tests; A2-friendly):**
  - **SC-B1a** `is_ccr_sentinel({'_ccr_dropped':'<<ccr:abc123 5 rows>>'}) is True`; `is_ccr_sentinel({'id':1}) is False`; `is_ccr_sentinel('contains _ccr_dropped') is False` (kills the dropped-`isinstance` mutation); `len(strip_ccr_sentinels([{'id':1}, sentinel, {'id':2}])) == 2` (locks #9). ★ KEEP `CCR_SENTINEL_KEY` / `<<ccr:HASH>>` sentinel-format checks internal — no public path, legitimate A2.
  - **SC-8** lossless csv-schema path (60 dicts) ⇒ TOIN `record_compression` receives `original_count==60` AND `compressed_count==60` while real `len(result.compressed)` shows ~89% byte reduction (locks #8 zeroed learning signal). Patch `toin.record_compression` to capture kwargs (edge mock — keeps C1 low).
  - **SC-7** A/B: fresh crusher `crush(60-item array)` ⇒ TOIN patterns==1; then `apply(read_only)` then `crush()` on same instance ⇒ TOIN patterns==0 (locks #7 stale-policy leak). Assert `_runtime_compression_policy` still set after `apply()` returns — KEEP internal (no public reset hook).
- **est new tests: 6–8** (sentinel B1 + lossless-count + TLS-leak A/B + a recovery-path sentinel round-trip).

### 7. `headroom/ccr/mcp_server.py` — user's MCP retrieve plane · cov 20% (floor)
- **Bugs:** #18 (false eviction error for live entry), #19 (`savings_percent` inverted).
- **B1 FIRST (public path — tool-output dicts, A2-friendly):**
  - **MS-B1a** compress achieving 65% reduction ⇒ tool dict `savings_percent == 35` AND `tokens_saved == 65` in the SAME response (locks #19 inversion + the sibling-field self-contradiction). The `35` literal is load-bearing.
  - **MS-18** store one live entry (ttl=3600), `_retrieve_content(hash, query='zzz_nomatch')` ⇒ response has `status=='available'` AND `error` contains `'no longer retrievable'` (locks #18 contradictory live-entry miss). Boundary: BM25 threshold 0.3 — a matching query returns the entry with no error.
- **est new tests: 4–6** (B1 savings_percent + parametrized matching-vs-nonmatching query + a clean-hit retrieve B1).

### 8. `headroom/parser.py` — metrics/diagnostics (compression OUTPUT unaffected — ranked below its 4-bug count) · cov 42% (floor)
- **Bugs:** #14 (whitespace always 0), #15 (HTML comment double-count), #16 (JSON gate mismatch + greedy merge), #17 (`None` text ⇒ TypeError disabling diagnostics).
- **B1 FIRST (public path — `detect_waste_signals` / `parse_message_to_blocks`, A2-friendly):**
  - **PR-B1a** `@parametrize` waste-signal literals against a length-based mock tokenizer: `'a'+' '*20+'b'+' '*15+'c'` ⇒ `whitespace_tokens == 0` (#14); `'Text <!-- simple comment --> end'` ⇒ `html_noise_tokens == 52` (not 26) (#15); 3 sub-threshold JSON objects + prose ⇒ `json_bloat_tokens == 536` (greedy merge) (#16). All pin CURRENT buggy metrics.
  - **PR-17** `parse_message_to_blocks({'role':'user','content':[{'type':'text','text':None}]})` raises `TypeError`; AND via `compress()` pipeline the bare-except at pipeline.py:431 ⇒ result has `waste_signals is None` with `tokens_saved>0` (locks #17 silently-disabled diagnostics). Same pattern parametrized over `_extract_tool_result_text` (:70) and `get_message_content_text` (:443).
- **est new tests: 6–8** (parametrized 3-metric B1 block + None-text TypeError across 3 call sites + pipeline-swallow assertion).

### 9. `headroom/cache/base.py` (`CacheConfig`) — NOT-RUN, 0% cov (minimal pass)
- No bug, no audit lens. Add a thin **config-field-affects-behavior** + **B1 default-value** pass per RULES.md delivery-gate item 6.
- **CB-B1a** pin each `CacheConfig` default value (literal); for any field claimed to tune behavior, one parametrized test that a changed value changes the observable outcome.
- **est new tests: 3–5.** Flag for a future dedicated audit lens (this is a coverage gap, not verified-clean).

---

## Estimated totals

| module | floor (cov) | est new tests |
|---|---|---|
| csv_schema_decoder | recovery-surface (not in baseline) | 6–8 |
| compression_store | recovery-surface (21 exist) | 7–9 |
| content_router | not in baseline | 8–10 |
| kompress_compressor | 13% | 22 |
| cache_aligner | 16% | 5–7 |
| smart_crusher | 70% | 6–8 |
| ccr/mcp_server | 20% | 4–6 |
| parser | 42% | 6–8 |
| cache/base (NOT-RUN) | 0% | 3–5 |
| **TOTAL** | TOTAL 54% (non-regression floor) | **~70–98 (plan to ~95)** |

## Execution gate (per module, iterate-to-plateau)

1. Full `pytest` stays green (real-bug tests pin CURRENT behavior → they pass today).
2. Recovery 21 still pass.
3. Module coverage ≥ its baseline floor (floor, not target).
4. Every new test mutation-sensitive (fails when the pinned behavior breaks).
5. Move B1↑ and D2↑; reduce A2 ONLY via genuine public paths; never delete internal-invariant coverage to win an axis.

**REPORT-ONLY.** Teammate implements **sequentially** — no parallel suite/git race. Bug fixes are a SEPARATE track gated on the user's per-bug fix/defer/intended-behavior decision; this plan does not authorize any engine-behavior change.