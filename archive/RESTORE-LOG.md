# RESTORE-LOG — cut-and-archive pass (2026-06-19)

Branch `verify/phase2-audit-report`. Every batch below kept **both gates green**:
- G1 `pytest tests/` → `519 passed, 31 skipped` (baseline, unchanged through every batch)
- G2 surface-walk → all `headroom.__all__` entries resolve (59 → 56 after public-API removals)
- Final extra gate: real `compress()` on a 400-row JSON array (saved 6316 tok, ratio 0.98) + CCR recovery invariant 21/21 byte-exact.

## ARCHIVED (moved, not deleted — ~7.5k LOC)

| Batch | Item | ~LOC | Restore |
|------|------|------|---------|
| B1 | `REALIGNMENT/` (roadmap docs) | 3900 | `git mv archive/REALIGNMENT .` |
| B1 | `sql/`, docker (`Dockerfile`/`docker-compose.yml`/`docker-bake.hcl`/`docker/`/`.dockerignore`), `claude_analysis_ttl.py`, `PR.md`, `TESTING-copilot-subscription.md`, `ENTERPRISE.md`, `.changelog.md` | ~1.3k | `git mv archive/<x> .` |
| B4/B5 | `cache/semantic.py` | 455 | `git mv archive/semantic.py headroom/cache/` + re-add to both `__init__` export tables |
| B4/B5 | `cache/prefix_tracker.py` | 355 | `git mv archive/prefix_tracker.py headroom/cache/` + re-add to `cache/__init__` tables |
| B4/B5 | `shared_context.py` | 219 | `git mv archive/headroom/shared_context.py headroom/` + re-add `SharedContext` to `headroom/__init__` |
| B6 | `proxy/interceptors/` | 656 | `git mv archive/headroom/interceptors headroom/proxy/` + restore guard block in `transforms/pipeline.py` + `intercept_tool_results` field in `config.py` |
| B6 | `binaries.py` | 527 | `git mv archive/headroom/binaries.py headroom/` (only after interceptors restored) |
| B3 | dead `_OPTIONAL_EXPORTS` infra, `create_pipeline()` wrapper, stale `mkdocs`/`pyproject` lines | ~45 | git history |

Empty stray dirs `headroom/{integrations,observability,storage}/` were removed (no tracked content).

## EMPIRICAL CORRECTIONS — the gate overrode the static audit
The audit's "vestigial" label was **wrong** for these; the two gates proved they are used, so they were NOT cut:
- `tokenizer.count_tokens_text` / `count_tokens_messages` / `Tokenizer.available` — imported + asserted by `tests/test_tokenizer.py`. Kept.
- `telemetry/` (4001) — LIVE on the `compress()` lossy path (SmartCrusher TOIN). `onnx_runtime.py` — LIVE (kompress). `wiki/` — live docs build. All kept (matches audit appendix).

## DEFERRED — entangled / live / rebuild-incoming (NOT cut; needs real untangle, out of scope for a pure-move loop)
The audit's headline finding: the "amputated" bloat is woven into the keep-set. These need code changes (not just a move),
which the archive+test loop can't validate safely (risk of a false-green where behavior silently no-ops). Each with its untangle:

- **Cache-optimizer cluster** (`anthropic` 517, `openai` 584, `google` 884, `registry` 175, `dynamic_detector` 1034, `compression_feedback` 613, `cache/base.py`) ≈ **3.8k LOC.** Entangled: `registry` ← `tokenizers/__init__.py`; `openai` → `dynamic_detector`; `compression_feedback` ← `compression_store.py:1089` (lazy live hook). Untangle: decouple `tokenizers` from `registry`, make the `compression_store` feedback import optional, then drop exports + move.
- **`relevance/`** (1017) — LIVE: `compression_store.py:48` imports `BM25Scorer` **unconditionally** → every CCR write loads it. Untangle: lazy-init the scorer (defer to first `search()`).
- **`models/ml_models.py`** (398) — reachable if `cache_aligner.detection_tiers` includes `ner`/`semantic`; coupled to `dynamic_detector`. Untangle: make ner/semantic imports `try/except ImportError`.
- **`proxy/helpers.py`** (~2931) — SSE utils (`parse_sse_events_from_byte_buffer`, `safe_decode_for_logging`) are LIVE (used by `ccr/`); `apply_session_sticky_ccr_tool` boundary unclear. Untangle: extract the ~50 live LOC to `ccr/sse_parser.py`, then move the rest. Manual review.
- **`ccr/batch_processor.py` + `ccr/mcp_server.py`** (1912) — uncalled today, BUT `mcp_server.py` is **load-bearing if the planned MCP retrieve plane is built** (the user's known next step). Intentionally kept — don't cut what you're about to rebuild.
- **`benchmarks/metrics.py`** (432) + **`verify/heldout/measure.py`** (879 dup) — bench-harness; cutting = repointing imports not covered by `pytest tests/`. Needs an explicit `verify/` run to validate. Deferred.
- Tier-3 leftovers (config `default_mode`/`prefix_freeze`/`cache_optimizer` fields, `simulate()` wrapper) — low value / ambiguous refs; left for a manual micro-trim.
