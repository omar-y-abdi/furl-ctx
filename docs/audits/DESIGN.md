> **HISTORICAL — Phase-2 design document (written 2026-06, archived to docs/audits/ 2026-07-03). SUPERSEDED.**
> This describes the engine as it was audited BEFORE the Phase-2/3 work landed and
> must not be read as current behavior. Known-stale core claims: the silent-loss
> path described below is CLOSED (store write + recovery pointer are UNCONDITIONAL
> on every drop — `enable_ccr_marker` gates neither; see `CODEBASE-MAP.md`
> §CONTRACT-ENFORCEMENT); the default routing policy is `min-tokens`, not
> lossless-first; all `file:line` anchors predate two refactor eras (the
> crusher.rs split into walk/route/persist among them). Kept for audit-trail
> value only.

# DESIGN — Furl Engine Improvements (Phase 2)

Grounded in the engine map (`headroom-engine-map` workflow, 5-facet read + synthesis).
Every claim cites `file:line` in `crates/furl-core/src/transforms/smart_crusher/` unless noted.

## How the engine drops data today (the audited reality)

`SmartCrusher.crush_array` (crusher.rs:584) on a JSON dict-array:
1. `adaptive_k` caps survivors at `max_items_after_crush=15` (config.rs:94). `len<=k` → passthrough.
2. **Lossless-first**: if a compaction stage saves ≥ `lossless_min_savings_ratio`, ship rendered string, drop nothing (crusher.rs:618-638).
3. Else **lossy**: `analyze_array` → pick planner (SmartSample default / ClusterSample / TopN / TimeSeries).
4. `keep = ∪(` position anchors 0.3/0.15, KeepErrors + KeepStructuralOutliers, numeric anomalies >2σ, change-points±1, query-anchors, relevance≥0.3 **(additive only)** `)` (planning.rs:169-229).
5. `prioritize_indices` (orchestration.rs:152-230): dedup by `md5(python_json_dumps_sort_keys(item))[:16]` → fill → **over-budget terminal drop**.
6. `execute_plan` keeps only `keep_indices`, drops the rest **irreversibly** (crusher.rs:268-276).
7. **Signal**: if `dropped_count>0 AND enable_ccr_marker` → store full original (SHA-256[:12]), append `{"_ccr_dropped":"<<ccr:HASH N_rows_offloaded>>"}` (crusher.rs:691-706).

### Three measured weaknesses → root causes (file:line)

| Weakness | Root cause |
|---|---|
| **Needles silently dropped** (90→24/15) | `orchestration.rs:217-226` — over-budget fill is **lowest-index-first**, stops at budget. A distinct mid-array item that fires no constraint/anomaly is dropped purely by position. AND `crusher.rs:704-705` — when `enable_ccr_marker=false`, rows drop with **no store + no marker** = silent, unrecoverable. |
| **Varying field defeats dedup** (~30% on logs) | `compute_item_hash` (anchor_selector.rs:350-355) hashes the **whole** item via `python_json_dumps_sort_keys`. One varying column (timestamp/id/uuid) → every row hash unique → dedup (orchestration.rs:64-65), cluster (planning.rs:368-402), fill-diversity (orchestration.rs:125-126) all no-op. |
| **"Same answers" is false** | The drop is real deletion. Recoverability hinges entirely on `enable_ccr_marker`; the marker signals only `N_rows_offloaded`, not which/why; retrieval returns the **entire** original (crusher.rs:697-701). |

---

## Improvement 1 — Safe Dedup (lossy → controlled)

**Decision: layer all three options — Option 3 as the recoverability floor (P0), Option 1 as the retention fix, Option 2 as the opt-out.** They are complementary, not exclusive.

### 1A (P0) — Unconditional persist: kill the silent-loss class
- **What**: whenever `dropped_count>0`, ALWAYS compute canonical + hash + `ccr_store.put` (decouple the store write at crusher.rs:691 from `enable_ccr_marker`). Let `inject_retrieval_marker` gate only the marker **text**, not persistence.
- **Why**: the canonical serialization is already computed once (crusher.rs:697); marginal cost = one store write. Turns "silently dropped needle" into "recoverable (and, when marker on, signalled)". Directly makes the audited failure impossible.
- **Trade-off**: store/TTL pressure if a caller drops-without-marker at high volume → mitigate with the existing TTL + a size cap; measure (open Q4).

### 1B — Novelty-ranked fill + singleton pinning: drop fewer needles
- **What**: at the terminal fill (orchestration.rs:217-226) replace lowest-index-first with **novelty-first** — rank `current \ prioritized` by distinctness (rarity of content-hash family / dissimilarity to the already-kept set) and fill highest-novelty first. Additionally **pin field-value singletons** (`unique_count==1` in any field, from existing FieldStats) the same way structural outliers are pinned (planning.rs).
- **Why**: directly attacks "unique mid-array needle dropped by index position" without changing output shape or the recoverability contract. Reuses FieldStats + constraint-pinning machinery.
- **Trade-off**: a novelty computation per candidate (cheap — content-hash family counts already available after the stable-hash change) + pinning singletons can push survivors past `max_items_after_crush=15` and inflate tokens → cap pinned-singletons, measure budget impact (open Q5).

### 1C — Explicit lossless / no-drop flag: caller opt-out
- **What**: expose `lossless_min_savings_ratio` + a per-request `no_drop` flag; sensitive arrays ship the lossless compacted rendering or pass through rather than enter the lossy path (lean on crusher.rs:618-638).
- **Why**: zero needle loss by construction for opted-in content; minimal code; deterministic.
- **Trade-off**: gives up compression on those arrays; needs the caller/router to mark sensitivity.

---

## Improvement 2 — Field-Aware (columnar) Compression

**Decision: a separate stable-projection hash for dedup/cluster/fill, keeping the full-item canonical hash for CCR keys (preserves Python/Rust byte-parity + recoverability).** This is the key design constraint — the existing `compute_item_hash` is documented byte-for-byte parity with Python `json.dumps(sort_keys=True)`; changing it in place would break parity and CCR keys.

1. **Field-role classifier** over existing FieldStats (analyzer.rs:59-128, types.rs:54-77) — no new analysis pass:
   - `CONSTANT`: `is_constant` (unique_count==1 across the array).
   - `VARYING-IDENTITY`: `unique_ratio > ~0.9` AND shape-matches timestamp/ISO-8601/uuid/hex/monotone-int. These are the fields that force unique hashes.
   - `CONTENT`: everything else (value-bearing).
2. **`stable_item_hash(item, exclude_set)`** — a NEW hash that serializes only `CONSTANT + CONTENT` fields (filter keys in the walker at anchor_selector.rs:451-475). Thread the `exclude_set` from `create_plan` (holds the analysis) down through `prioritize_indices`/`deduplicate_indices_by_content`/`fill_remaining_slots` (orchestration.rs:152,49,82) — the only callers.
3. **Result**: 90 rows identical-except-timestamp collapse to one stable hash → dedup + cluster + fill-diversity work as intended → log/structured compression rises from ~30% toward 82-95%. The kept representative carries its real varying values; multiplicity recorded as `_dup_count` (and the varying values can be kept compactly in a sidecar if needed).
4. **Python parity**: mirror the stable-hash + classifier in `furl_ctx/transforms/smart_crusher.py` (and broaden the log normalizer `log_compressor.py:395-419` / `log_compressor.rs:32-36` to template ISO-8601/UUID/hex runs). The **canonical CCR hash stays full-item**, so the retrieve contract and parity fixtures for CCR are untouched.

---

## Improvement 3 — Honest Benchmarking (build FIRST, drives Imp1 tuning)

Real, high-entropy inputs only (P0 constraint — no synthetic low-entropy):
- **Real codebase**: this repo post-amputate (~87k LOC) — code/file content.
- **Real logs**: captured tool output / public Loghub sample — varying-field structured rows.
- **Real search results**: a set of distinct API/tool responses with varying fields.
- **Needle-recall harness** (open Q1): inject a known unique "needle" row at varied positions/cardinalities into 90+ row arrays; measure retention.

Three metrics per benchmark, reported separately:
1. **Lossless token reduction** — savings with zero info loss.
2. **Lossy drop ratio** — fraction removed.
3. **Information retention** — % distinct items preserved OR CCR-recoverable (needle recall).

Benchmark the CURRENT engine first (baseline), then after each change. Regressions block.

---

## Open questions to resolve by measurement (not by guessing)
1. Empirical needle-recall today (needle-injection harness).
2. VARYING-IDENTITY `unique_ratio` cutoff + shape patterns that separate identity-noise from content.
3. CCR TTL (~1800s since Engine P0-3) vs retrieval latency under unconditional persist (1A).
4. Store/TTL pressure under unconditional persist.
5. Budget blow-up from singleton pinning (1B) past `max_items_after_crush=15`.
6. Is there a legitimate caller of the silent-loss path (`enable_ccr_marker=false`)? If not → remove it.
7. Does novelty round-robin fill break Python parity fixtures (orchestration.rs:81 notes stride mirrors Python)?

## Build order
Imp3 baseline harness → measure current → implement 1A (silent-loss kill) → implement Imp2 (stable hash, biggest ratio win) → implement 1B (novelty fill + singleton pin) → 1C (flag) → re-benchmark → adversarial needle-retention verify → BENCHMARKS.md.
