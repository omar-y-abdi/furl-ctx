# Headroom Compression Engine — Break-Mode Action Doc

## Summary

A 50-experiment BREAK-mode campaign ran against the Headroom compression engine, hunting for **contract violations**, not compression gains.

**Honesty headline: zero compression improvements were produced.** This is a defects ledger. Of 50 measured experiments:
- **33 found real defects** (`verdict=defect_found`): **28 break a HARD contract** (`contracts_ok=false`); **5 are quality/recall regressions** where contracts technically hold (`contracts_ok=true`).
- **16 are dead ends** (12 `no_change`, 4 `rejected`) — listed below so they are never re-tried.

The 33 defects collapse into **8 root-cause clusters (A–H) + 1 systemic test**. The dominant theme:

> **The Rust compactor proves its lossless encodings round-trip ONLY against the Rust decoder — never against the Python `csv_schema_decoder.py`.** So a whole family of "lossless" paths silently produces unrecoverable output on fresh data with **no CCR sentinel**. This is compounded by a **Contract #4 overfit**: the `verify/` fixtures contain no embedded newlines, commas-in-cells, colons-in-keys, JSON nulls, or nested arrays — so the full benchmark suite passes green while fresh out-of-sample data is destroyed.

Two further clusters break the **prompt-cache contract** because the public `compress()` path never computes a frozen-message count (`compress.py:243` never calls `compute_frozen_count`, so `content_router.py:2152`'s freeze gate is always 0). One cluster breaks **CCR recovery** via FIFO store eviction of the whole-blob.

**Caution on the `delta` field:** it is NOT a single unit — it mixes tokens-saved (`break-1 delta 503`), rows-lost (`break-22 delta -10`), and 0–10 severity scores. **Do not sort by it.** Ranking below is by contract-class, then blast radius, citing the raw measured evidence.

## Top Recommendation

**Build Cluster A first** — replace `headroom/transforms/csv_schema_decoder.py:325` (`lines = text.split("\n")`) with an RFC-4180 quote-aware line reader. This one fix closes the worst defect in the ledger (`newline-in-lossless-csv-field`, `break-22`, **delta -10, total silent loss, no CCR sentinel**) plus three more total-loss 7s.

**Immediately after, land the systemic fix** (Rank 2): a **cross-language round-trip fuzz test** (Rust-compact → Python `decode_csv_schema_rows` → assert deep equality) over adversarial cell shapes. That single test would have caught **every** lossless-corruption defect in this ledger.

## Ranked Actions

| Rank | Cluster | Title | Closes (approach_id) | Worst evidence | Contract |
|------|---------|-------|----------------------|----------------|----------|
| 1 | A | CSV decoder shatters quoted multi-line cells | `newline-in-lossless-csv-field`, `embedded-newline-csv-split-decoder-failure`, `newline-in-const-fold-header`, `embedded-newline-csv-schema-row-desync` | break-22: 10/10 rows lost, delta -10 | #1 / #4 |
| 2 | — | **Systemic**: cross-language round-trip fuzz test | catches break-10/11/13/15/22/27/28/34/38/39/40/46/48 | "stamper proves round-trips only with the Rust decoder" (break-38) | #4 |
| 3 | F | Lossless compaction silently substitutes/erases (5 branches) | `nullable-column-null-empty-string-collapse`, `colon-in-json-key-...`, `json-array-nested-cell-type-corruption-...`, `stringified-json-object-flatten-type-erasure`, `10-degenerate-stringified-nested`, `crush-object-silent-key-drop-no-ccr` | break-23: pure silent key-drop, no store write | #1 |
| 4 | C | Public path never freezes cached prefix | `break-1-cache-prefix-intra-message`, `cached-prefix-cross-message-no-frozen-count`, `content-router-msg0-tool-result-no-index-guard`, `cross-message-dedup-sibling-block-cached-prefix-violation` | break-13: msg w/ zero cache_control compressed | #2 |
| 5 | B | Decoder parses cells without unquoting; marker commas mis-split | `head-dict-csv-quote-unquote-mismatch`, `affix-csv-middle-unquote-defect`, `opaque-ccr-comma-split-decoder-failure` | break-38: 18/18 rows lost | #1 |
| 6 | G | CCR FIFO eviction → unbacked sentinels **[REFRAMED — not silent loss]** | `ttl-capacity-eviction-unbacked-sentinel-v1`, `ccr-lru-eviction-contract-break` | break-9: single call 1060>1000 writes, intra-call eviction | #1 |
| 7 | D | Missing cache_control guards (read_lifecycle, dedup, router) | `read-lifecycle-cache-control-missing-guard`, `read-lifecycle-sibling-block-cache-ordering`, `cache-control-read-lifecycle-openai-tool-message`, `cache-control-dedup-openai-string-message`, `break-7-cache-control-openai-tool-message` | break-31: cache-bust + unrecoverable (store=None) | #2 + #1 |
| 8 | E | `_ensure_ccr_backed`/pinning/mirror blind to non-`<<ccr:` formats | `log-ccr-stale-result-cache-...blind-spot`, `diff-ccr-result-cache-marker-format-gap`, `nested-opaque-ref-unmirrored-ccr` | break-32: result cache serves dead pointer | #1 |
| 9 | H | 12-char SmartCrusher hash rejected by 24-char tool validation | `break-5-huge-field-ccr-hash-mismatch` | LLM cannot retrieve 100% of public-API markers | #1 (tool path) |
| 10 | T2 | Quality/recall regressions (contracts hold) | `near-dup-reference-source-lossy-invalidation`, `granular-row-index-kept-row-cost-inflation`, `mixed-dup-count-match-failure`, `unicode-sentinel-injection-4` | break-6: needle visible recall 72.2%→0.0% | none (quality) |

### Key file:line targets (verified against the codebase map)
- `csv_schema_decoder.py:325` — `text.split("\n")` (Cluster A)
- `csv_schema_decoder.py:205-206, :419, :426` — CSV-quote bypass / no-unquote (Clusters B, F)
- `compactor.rs:243-259` (cell_from_value), `:175` (stamp order), `formatter.rs:285-287` (raw key write), `crusher.rs:617` (crush_object, no CCR) — Cluster F
- `compress.py:243` (no frozen count) → `content_router.py:2152` (freeze gate), `:2584/2640` (per-block-only guards) — Cluster C
- `read_lifecycle.py:390, :444` (missing guards) — Cluster D
- `content_router.py:1736` (`<<ccr:` early-exit), `:2274/2640/2744` (pinning), `smart_crusher.py:705` (mirror prefilter) — Cluster E
- `tool_injection.py:500-507` (24-char gate) vs `walker.rs:130`/`crusher.rs:1616` (12-char emit) — Cluster H
- `in_memory.rs:87/119` (FIFO, cap=1000), `crusher.rs:1160` (persist_dropped), `:1213` (row index stores ALL rows) — Clusters G, T2

> **Cluster G reframe (post-verification).** Independent probes showed the FIFO
> eviction is real but the loss is **already loud** — every model-facing retrieve
> (bulk, search, AND granular `#rows`) returns an explicit `success=False` error
> via `response_handler._execute_retrieval`, never a silent `None`. The bare hash
> the model retrieves is backed by one whole-blob entry (all rows), so granular
> eviction is all-or-nothing, not a silent subset. **G as a silent-loss defect
> does not reproduce.** The one real residual — a capacity eviction misreported as
> a TTL miss — is fixed (cause-honest message). True cross-call *retention* (the
> "free lunch") is the open follow-up. Full analysis + locking tests:
> **`CCR-RETENTION.md`**, `tests/test_ccr_eviction_loud_miss.py`.

## Dead Ends — DO NOT RE-TRY (16)

These were measured and found to have no defect. The discriminator is the `verdict` field.

### `verdict=no_change` (12) — analytically/empirically clean
| approach_id | Why it is closed |
|-------------|------------------|
| `granular-retrieval-effective-savings-negative` | On realistic homogeneous data, effective savings stay **≥ +42% at 50% retrieval**; only contrived 2-field rows go marginally negative (-3.5%) under per-call overhead. Not robust. |
| `mixed-type-ccr-subgroup-skip` | str/number sub-branches skip per-subgroup CCR by design, but the outer `ccr_dropped_sentinel` (crusher.rs:569-584) stores the whole array. All 55 dropped items recoverable. |
| `break-16-unicode-encoding-ccr` | 11 unicode/emoji/NUL/surrogate probes; SHA-256 round-trips byte-faithful; `explicit_hash` mirroring neutralizes format divergence. |
| `hex-field-identity-misclassification-dup-count-crowdout` | `_dup_count` is a UX/doc nuance, not a contract break; 12-char retrieval block is already `break-5`. |
| `near-dup-ccr-dropped-item-recovery-18` | Deduper stores the DROPPED item's own bytes under its own hash; needle recoverable, changed rows stay visible. |
| `break-20-hash-parity-chain-exhaustion` | Universal `explicit_hash` mirroring; `_store_in_ccr` shim has zero production callsites. |
| `ttl-result-cache-race-unbacked-sentinel` | `HASH#rows` never mirrored → `_ensure_ccr_backed` always False for granular outputs → safe recompute. |
| `json-whitespace-separator-byte-mismatch` | Whitespace-only normalization; semantic recovery holds; engine never promised byte-exact wire format. |
| `break-29-huge-field-pathological-row` | OpaqueRef writes to CCR before substitution; all markers recoverable. |
| `break-30-near-dup-ccr-backing` | Store-before-replace invariant: `_persist_original` runs before sentinel emission; False → abort. |
| `unicode-ccr-escape-normalization-36` | UTF-8 vs `\uXXXX` is RFC 8259-permitted normalization; semantic recovery holds (same class as whitespace). |
| `near-dup-ccr-recovery-empirical-42` | Empirical SHA-256 match on fresh data; near-dup output pinned, never enters result cache. |

### `verdict=rejected` (4) — duplicate of an already-found defect or analytically closed
| approach_id | Why |
|-------------|-----|
| `kept-row-index-inflation-measure-divergence` | Same mechanism as `granular-row-index-kept-row-cost-inflation` (break-14); 7-large config now takes lossless path, negative not reproducible. |
| `mixed-type-ccr-subgroup-skip-v2` | Structurally identical to `mixed-type-ccr-subgroup-skip`; single live call site, outer sentinel compensates. |
| `canonical-hash-collision-dual-storage` | Per-row 48-bit collision only degrades granular retrieval; whole-blob fallback (independent hash, written LAST) preserves Contract #1; whole-blob 2nd-preimage at 2⁴⁸ infeasible. |
| `ttl-ensure-ccr-backed-rows-key-analysis` | All three hypothesized bypasses are over-conservative-but-safe or already `break-32/33`. |

> **Note on retrieval-cost tiering:** `granular-retrieval-effective-savings-negative` and `kept-row-index-inflation-measure-divergence` both confirm the retrieval-cost negative is **real only on heterogeneous inputs** (large-kept + small-dropped rows). On realistic homogeneous data it stays **> +42% at 50% retrieval**. The implementer must NOT over-claim `granular-row-index-kept-row-cost-inflation` (break-14) as a general negative.

## Methodology Note

All findings are **measured** (each carries a before/after with sha256s, token counts, or store-retrieval results) and **out-of-sample** (Contract #4: adversarial inputs were generated fresh in `/tmp`, unrelated to `verify/` fixtures — indeed the recurring root cause is that the fixtures fail to exercise these shapes). Code-site claims in this doc were independently re-verified against the live tree (`csv_schema_decoder.py:325/205/419/426`, `content_router.py:1736/2152/2274`, `smart_crusher.py:705`, `read_lifecycle.py:390/444`, `compress.py:243`). Repros were NOT re-run during synthesis — instead, **each fix's repro is specified as its acceptance test** in the build_steps. Tier the only compression-adjacent claim (the retrieval cost-model, break-14) as heterogeneous-input-only; produce no compression-gain claims, because none were measured.