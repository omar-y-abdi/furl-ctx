# lazy-dev-AUDIT-final.md — consolidated bloat findings (Python + Rust + disk/doc)

> **Merge of two verified audits.** Python/doc/disk detail → `lazy-dev-AUDIT-v2.md` (281 agents, 230 findings).
> Dead-Rust detail → `lazy-dev-AUDIT-rust.md` (47 agents, 6 dead targets). v1's light rust-core rows are **superseded** by the deep Rust pass.
> Branch `verify/phase2-audit-report`. **REPORT-ONLY** — applying is the separate gated archive+test loop (proven: v1 cut ~7.4k green).
> All claims below spot-checked against the live tree by the orchestrator (the way v1's REALIGNMENT coherence bug was caught).

## Repo size (code only, comments + blanks excluded — `cloc`)
**Python 33,969 + Rust 29,195 = ~63,164 source LOC.** (Plus 72k JSON / 5k text = test fixtures, not maintainable code.) The old "~91k" counted comments+blanks.

## GRAND TOTAL — cuttable

| Bucket | LOC | % of 63k | Risk |
|---|---:|---:|---|
| **Tier 1 — safe now** (verified dead, minimal/no untangle) | **~7,070** | ~11% | low |
| **Tier 2 — after untangle** (dead/vestigial, 1 co-requisite edit each) | **~9,920** | ~16% | medium (per-cut verify) |
| **Code subtotal** | **~17,000** | **~27%** | — |
| Doc cruft (wiki/README/llms.txt stale) | ~2,800 lines | — | low |
| Disk: 19 GB worktrees | **DONE** (reclaimed) | — | — |
| Disk: stale JSON results + media | 1.45 MB + 25.6 MB | — | media needs-review |

**~27% of source code is removable** — landing squarely in my earlier 25–33% prediction. Most is behind small untangles, not `rm`. The "amputation" left more dead weight than the handoff claimed, and it's split almost evenly Python/Rust.

---

## TIER 1 — SAFE NOW (~7,070 LOC)

### Rust (~4,427 — verified, panel-confirmed)
| What | Paths | ~LOC | Co-requisite | Tests to move |
|---|---|---:|---|---|
| **`pipeline/` subtree (13 files)** — biggest single cut | `transforms/pipeline/{orchestrator,config,traits,mod}.rs` + `offloads/*` + `reformats/*` | **4,212** | drop `pub use/pub mod pipeline` (`mod.rs:32,59-63`) | none (inline `#[cfg(test)]` only) |
| `transforms/safety.rs` | `transforms/safety.rs` | 215 | drop `mod.rs:34,65` | none |
> Verified: zero pyo3 bridge (`lib.rs`), only non-test ref is the dead `mod.rs:60` re-export, consumer crate `headroom-proxy` doesn't exist.

### Python (~2,640 — from v2, see `lazy-dev-AUDIT-v2.md` Tier 1)
Dead `proxy/helpers.py` clusters (720), `conftest.py` 15 dead fixtures incl. nonexistent `providers.openai` import (193), commented-out code `compression_store.py:1097`, + more. Stale JSON results (`verify/*raw_results.json`, `benchmarks/*_results.json`, 1.45 MB) cut here too.

---

## TIER 2 — CUT AFTER UNTANGLE (~9,920 LOC)

### Rust (~3,322 — needs-review, AuthMode coupling)
| What | Paths | ~LOC | Untangle |
|---|---|---:|---|
| **`live_zone.rs`** whole-request dispatcher | `transforms/live_zone.rs` | 2,899 | hoist its private `AuthMode` enum (≠ canonical `auth_mode.rs`, **keep that**) before delete; lands with the `lib.rs` FFI removal + tests `live_zone_{ccr,dispatch,thresholds,token_validation}.rs` |
| `recommendations.rs` (PR-F3 never landed) | `transforms/recommendations.rs` | 329 | couples to live_zone `AuthMode`; review together; test `recommendations_loader.rs` |
| dead FFI glue + re-exports | `furl-py/lib.rs` (~94) + `mod.rs` (~20) | ~114 | ride with the above |

### Python (~6,600 — from v2 + v1-deferred cluster)
cache-optimizer cluster (~3.8k: anthropic/openai/google/registry/dynamic_detector/compression_feedback), `relevance/` (1k, unconditional BM25 import), remaining `proxy/helpers.py` (~2.8k SSE-live boundary), `ccr/batch_processor.py` (keep `mcp_server.py` — needed for the planned MCP retrieve plane), `ml_models.py`. Each needs an export/import untangle — see `lazy-dev-AUDIT-v2.md` Tier 2.

---

## NON-CUTS (verified LIVE / load-bearing — do not touch)
- **Rust compressors** `log/diff/search_compressor.rs` — **NOT dead duplicates** (the rust audit refuted that hypothesis): they survive via their own pyclass bridge, used by the live `compress()` path. The `offloads/` that point at them are dead, but the compressors are live.
- `SmartCrusher` subsystem (crusher/compaction/analyzer/planning/orchestration/anchor_selector), `ccr/`, `tokenizer/`, `cache_control.rs` — the live engine + hard invariants.
- `auth_mode.rs` / `compression_policy.rs` — Py↔Rust parity invariant (Phase F).
- Python `telemetry/` (4001, SmartCrusher TOIN loop), `onnx_runtime.py`, `wiki/` (docs build) — confirmed live in v1/v2.
- Feature-gated Rust (magika/embeddings/redis) — intentional conditional compilation.
- Complexity-lint (154 magic-values, C901, too-many-*) — **quarantined, NOT recommended**: high-churn regression risk on a hardened, invariant-bound engine.

---

## Doc cruft (~2,800 lines — `lazy-dev-AUDIT-v2.md` D1–D15)
Wiki/README/llms.txt documenting removed `FurlClient`/`SharedContext`/`RollingWindow`/CLI; inconsistent savings claims (README 60–95% vs honest BENCHMARKS 0–54%). Note: 6 of 7 ruff ERA001 "commented-out code" hits were **false positives** (comment headers) — the v2 synth correctly kept them.

## Next step
Apply via the proven **archive + two-gate test loop** (pytest + surface-walk + CCR recovery + compress() round-trip; restore-on-red). Suggested order: Rust Tier-1 `pipeline/`+`safety.rs` (4.4k, cleanest big win) → Python Tier-1 → then the untangle tiers per-cut. **Report-only until you say go.**
