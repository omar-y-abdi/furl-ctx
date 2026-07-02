# Headroom Rust Rewrite — Developer Guide

This document covers the Rust port of Headroom. It is the only new top-level
doc created in Phase 0; longer-form design/plan writeups live elsewhere and
are not versioned in this repo.

## Workspace layout

```
Cargo.toml                       # workspace root
rust-toolchain.toml              # pins stable rustc with rustfmt+clippy
crates/
  headroom-core/                 # library: shared types + transform trait surface
  headroom-py/                   # PyO3 cdylib exposing `headroom._core`
```

`cargo build --workspace` builds every crate. `default-members` drops
`headroom-py` from `cargo run`/bare-`cargo test` flows so that `cargo test
--workspace` does not try to execute the PyO3 cdylib standalone (it can't
find `libpython` without a Python interpreter hosting it).

## Common commands

`just` is not installed on dev boxes here; a `Makefile` at the repo root
exposes the same targets:

| Target | What it does |
| --- | --- |
| `make test` | `cargo test --workspace` |
| `make bench` | `cargo bench --workspace` |
| `make build-wheel` | `maturin build --release -m crates/headroom-py/Cargo.toml` |
| `make verify-rust-core` | maturin-develop + import-verify `headroom._core` in one shot |
| `make fmt` | `cargo fmt --all` |
| `make fmt-check` | `cargo fmt --check` |

## Maturin + Python wiring

`headroom-py` is a PyO3 cdylib that exposes `headroom._core` in Python. The
`extension-module` feature is opt-in so plain `cargo build --workspace` does
not try to link against `libpython` on systems that don't have it.

### First-time setup (clean venv recommended)

```bash
python3.11 -m venv /tmp/hr-rust-venv
source /tmp/hr-rust-venv/bin/activate
pip install maturin
cd crates/headroom-py
maturin develop           # editable dev build, installs headroom._core
cd /tmp                   # IMPORTANT: step out of the repo root first
python -c "from headroom._core import hello; print(hello())"
# => headroom-core
```

> Why `cd /tmp`? The repo root also contains the Python `headroom/` package.
> Running the smoke import from the repo root makes Python resolve `headroom`
> to `./headroom/__init__.py` (the full SDK, which pulls in heavy deps) instead
> of the lightweight namespace package installed by maturin. Tests should
> either run outside the repo root, or ensure `headroom` is installed into
> the same venv (then the maturin-installed `_core.so` lands alongside it and
> both imports resolve).

### Release wheels

```bash
make build-wheel
# wheels land under target/wheels/
```

CI (`.github/workflows/rust.yml`) builds linux-x86_64, macos-arm64, and
macos-x86_64 wheels via `PyO3/maturin-action` and uploads them as artifacts.

## Known regressions in retired-Python components

The Stage 3b/3c.1b retirements deleted Python source for `DiffCompressor`
and `SmartCrusher` and replaced them with PyO3-delegating shims. The
2026-04-28 audit found that the retirements shipped with subsystems
silently disconnected. This section tracks each gap and its disposition
so they don't regress further or get forgotten.

### SmartCrusher

| Subsystem | State | Tracked by |
|---|---|---|
| TOIN learning loop | **Re-attached 2026-04-28.** Shim's `crush()` and `_smart_crush_content()` now call `toin.record_compression()` after a real compression. Filtered on `strategy != "passthrough"` to ignore JSON re-canonicalization. Best-effort: TOIN failures are logged at debug level and don't break compression. | `tests/test_smart_crusher_toin_attachment.py` |
| CCR marker emission knob | **Honored end-to-end 2026-04-29.** New `enable_ccr_marker: bool` field on Rust `SmartCrusherConfig`; `crush_array` checks it before emitting the `<<ccr:HASH>>` marker text and the CCR store write. Python shim flips it from `ccr_config.enabled and ccr_config.inject_retrieval_marker` — both flags collapse to the same Rust gate, since storing payloads under either off-switch makes no sense. Scope: gates only the row-drop sentinel path; Stage-3c.2 opaque-string CCR substitutions still emit always (no Python equivalent, no production caller asks for suppression). | `tests/test_smart_crusher_toin_attachment.py` + `crates/headroom-core/.../crusher.rs::tests::enable_ccr_marker_*` |
| Custom relevance scorer | **Closed (fail-loud) 2026-04-29.** `relevance_config` and `scorer` constructor args remain in the signature for source compat, but the shim raises `NotImplementedError` when either is non-None — silently dropping a user-supplied scorer is a textbook silent-fallback bug. Full plumbing waits on Stage-3c.2's relevance-crate Python bridge. | `tests/test_smart_crusher_toin_attachment.py::test_custom_*_arg_raises_not_implemented` |
| Per-tool TOIN learning hook | **Re-attached partially.** `_smart_crush_content` accepts `tool_name` and now threads it into the TOIN record. The hook is best-effort — it improves `query_context` aggregation but doesn't drive per-tool overrides yet. | `tests/test_smart_crusher_toin_attachment.py::test_smart_crush_content_records_to_toin` |

### DiffCompressor

| Subsystem | State |
|---|---|
| Adaptive context windows | Honored byte-for-byte (parity fixture-locked). |
| TOIN integration | Never had one — DiffCompressor records via `_record_to_toin` in ContentRouter, which already runs for non-SmartCrusher strategies. No regression. |

### Phase 3e.1 — `signals/` trait module + KeywordDetector (2026-04-29)

The Python `error_detection.py` regex registry was retired and reborn as a
trait + tier system in `crates/headroom-core/src/signals/`. See
`signals/README.md` for the full architecture; the highlights:

- **Per-granularity traits.** `LineImportanceDetector` ships today; future
  `ContentTypeDetector` and `ItemImportanceDetector<I>` will follow as their
  consumers get touched.
- **`Tiered<T>` combinator.** Composition, not inheritance. Future ML
  detectors slot in as new tiers without changes to `KeywordDetector` or
  any caller.
- **One concrete impl.** `KeywordDetector` (aho-corasick) is the only tier
  registered today. **No NoOp/stub impls** — per project no-silent-fallbacks
  rule, future tiers land with their real implementations.
- **Bug fixes baked in.** `ERROR_KEYWORDS` regex now includes
  `timeout|abort|denied|rejected` (previously drifted from the keyword set);
  `token` dropped from `SECURITY_KEYWORDS` (false-positived on every LLM
  metric reference). Both fixed in the Python regex too via the shim that
  recompiles patterns from the Rust-exposed keyword tables.
- **Companion canonical extension path.** `signals/README.md` documents
  the BGE classifier head — a 384-dim → 4-class softmax on top of the
  already-loaded `bge-small-en-v1.5` embedder — as the natural ML tier.
  Two alternatives kept open: distilled tinyBERT in ONNX, logistic
  regression on lexical features.

### Phase 3g (queued) — Compression Pipeline Formalization (issue #315)

Strategic decision 2026-04-29: after Phase 3e (compressor ports) and
Phase 3f (Rust MCP scaffold) wrap, formalize the lossless-then-lossy-
then-CCR ordering as a cross-cutting `CompressionPipeline` orchestrator
+ `LosslessTransform` / `LossyTransform` traits in
`crates/headroom-core/src/pipeline/`. Existing compressors get
refactored as compositions of pluggable transforms. The crucial design
choice — **parsers for structure, models at the prose/structure
boundary** — is captured in issue #315 and
`memory/project_lossless_first_pipeline.md`. Do NOT start coding before
3e/3f finish.

### Watch list (potential regressions, not yet audited)

- `CCRConfig.enabled=False` end-to-end — **closed 2026-04-29**. Both `enabled=False` and `inject_retrieval_marker=False` collapse to the same Rust `enable_ccr_marker=False` gate (no marker, no store write). See the SmartCrusher table above.
- `SmartCrusherConfig.use_feedback_hints=False` — **closed**: the field (and `toin_confidence_threshold`) was removed from both the Rust config struct and the pyo3 constructor after the Python feedback/TOIN system was deleted; nothing forwards or honors it anymore.

When any item above changes, update both this section and the test file. The shim's docstring also references this section — keep them aligned.

## Phase 0 Blockers

These are known limitations for Phase 0. They are tracked here so Phase 1
doesn't rediscover them.

- **`rust-toolchain.toml`** pins `channel = "stable"` rather than a specific
  version so CI picks up the same toolchain the local box uses. Tighten to a
  pinned version (e.g. `1.78`) once the port stabilizes.

## CCR storage — process-local, request-window-scoped

The CCR store (`crates/headroom-core/src/ccr/backends/`) ships a single
backend: `InMemoryCcrStore`, a process-local sharded `DashMap` constructed once
at startup and shared across worker threads behind an `Arc`. Entries are bounded
(capacity-LRU + TTL, defaults 1000 entries / 300 s) and lost on restart, so
byte-exact CCR recovery is guaranteed only **within the process / request
window** — long enough for the `<<ccr:HASH>>` markers a single compression emits
to round-trip on the model's retrieval call.

No persistent or cross-process backend ships. Spilling evicted entries to a
durable store (keyed by session for cleanup) is a deliberately-deferred epic —
see `CCR-RETENTION.md` for the design and trade-offs. On the Python side,
`CompressionStore` exposes a `headroom.ccr_backend` setuptools entry point so a
durable adapter can be registered out-of-tree (`HEADROOM_CCR_BACKEND=<name>`),
but none ships by default and the in-memory backend is the only one wired.
