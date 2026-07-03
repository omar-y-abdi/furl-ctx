# Furl Rust Rewrite — Developer Guide

This document covers the Rust port of Furl. It is the only new top-level
doc created in Phase 0; longer-form design/plan writeups live elsewhere and
are not versioned in this repo.

## Workspace layout

```
Cargo.toml                       # workspace root
rust-toolchain.toml              # pins stable rustc with rustfmt+clippy
crates/
  furl-core/                 # library: shared types + transform trait surface
  furl-py/                   # PyO3 cdylib exposing `furl_ctx._core`
```

`cargo build --workspace` builds every crate. `default-members` drops
`furl-py` from `cargo run`/bare-`cargo test` flows so that `cargo test
--workspace` does not try to execute the PyO3 cdylib standalone (it can't
find `libpython` without a Python interpreter hosting it).

## Common commands

`just` is not installed on dev boxes here; a `Makefile` at the repo root
exposes the same targets:

| Target | What it does |
| --- | --- |
| `make test` | `cargo test --workspace` |
| `make bench` | `cargo bench --workspace` |
| `make build-wheel` | `maturin build --release -m crates/furl-py/Cargo.toml` |
| `make verify-rust-core` | maturin-develop + import-verify `furl_ctx._core` in one shot |
| `make fmt` | `cargo fmt --all` |
| `make fmt-check` | `cargo fmt --check` |

## Maturin + Python wiring

`furl-py` is a PyO3 cdylib that exposes `furl_ctx._core` in Python. The
`extension-module` feature is opt-in so plain `cargo build --workspace` does
not try to link against `libpython` on systems that don't have it.

### First-time setup (clean venv recommended)

```bash
python3.11 -m venv /tmp/hr-rust-venv
source /tmp/hr-rust-venv/bin/activate
pip install maturin
cd crates/furl-py
maturin develop           # editable dev build, installs furl_ctx._core
cd /tmp                   # IMPORTANT: step out of the repo root first
python -c "from furl_ctx._core import hello; print(hello())"
# => furl-core
```

> Why `cd /tmp`? The repo root also contains the Python `furl_ctx/` package.
> Running the smoke import from the repo root makes Python resolve `furl_ctx`
> to `./furl_ctx/__init__.py` (the full SDK, which pulls in heavy deps) instead
> of the lightweight namespace package installed by maturin. Tests should
> either run outside the repo root, or ensure `furl_ctx` is installed into
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
| Compression-feedback learning loop | **Deleted with the telemetry/feedback plane (2026-06 excision).** The recorder the 2026-04-28 audit had re-attached (and its per-tool `tool_name` hook) went with the rest of the learning plane; the shim records nothing after a compression. Not a silent gap — the subsystem no longer exists. | — (subsystem and its test file deleted together) |
| CCR marker emission knob | **Superseded by the recovery invariant (Defect 1).** The `enable_ccr_marker: bool` field still exists on Rust `SmartCrusherConfig` (flipped from `ccr_config.enabled and ccr_config.inject_retrieval_marker`), but it does NOT gate the `<<ccr:HASH>>` recovery pointer or the CCR store write — both are UNCONDITIONAL on every drop (a drop without its pointer is silent loss; `persist_dropped` emits both regardless of the flag). The flag is the retrieval-tool advertisement preference, preserved for embedders that read it back; the router also requires it for the optional CCR-offload fallback. The log/search/diff `Retrieve …: hash=…` marker lines are likewise unconditional recovery pointers (pinned by `tests/test_lossless_only_mode.py::test_log_route_recovery_marker_immune_to_ccr_flags`). Marker-free output is what `lossless_only` strict mode is for — it never drops, so nothing needs a pointer. | `crusher.rs::tests::enable_ccr_marker_false_still_surfaces_recovery_pointer`; `tests/test_ccr_recovery_invariant.py` |
| Custom relevance scorer | **Closed (fail-loud) 2026-04-29.** `relevance_config` and `scorer` constructor args remain in the signature for source compat, but the shim raises `NotImplementedError` when either is non-None — silently dropping a user-supplied scorer is a textbook silent-fallback bug. | fail-loud guard in `furl_ctx/transforms/smart_crusher.py` (its dedicated test was deleted with the feedback-plane test file) |

### DiffCompressor

| Subsystem | State |
|---|---|
| Adaptive context windows | Honored byte-for-byte (parity fixture-locked). |
| Feedback-plane integration | Never had one — and the ContentRouter recording hook it would have used was deleted with the feedback plane (2026-06 excision). Nothing to regress. |

### Phase 3e.1 — `signals/` trait module + KeywordDetector (2026-04-29)

The Python `error_detection.py` regex registry was retired and reborn as a
trait + tier system in `crates/furl-core/src/signals/`. See
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
  the BGE classifier head — a 384-dim → 4-class softmax on a
  `bge-small-en-v1.5` embedder — as the natural *future* ML tier. Note:
  the embedding scorer was excised along with the never-shipped
  `embeddings` feature (the core is ML-free today), so that tier would
  have to reintroduce an ONNX embedder. Two alternatives kept open:
  distilled tinyBERT in ONNX, logistic regression on lexical features.

### Phase 3g (closed) — Compression Pipeline Formalization (issue #315)

Strategic decision 2026-04-29: after Phase 3e (compressor ports) and
Phase 3f (Rust MCP scaffold) wrap, formalize the lossless-then-lossy-
then-CCR ordering as a cross-cutting `CompressionPipeline` orchestrator
+ `LosslessTransform` / `LossyTransform` traits in
`crates/furl-core/src/pipeline/`. The crucial design choice —
**parsers for structure, models at the prose/structure boundary** — is
captured in issue #315.

**Status: closed.** The orchestrator + trait pair was built and later
DELETED in the dead-code sweep — no separate offload-pipeline trait
survives in the current tree; offloading is inlined in
`crusher.rs::persist_dropped` (see CODEBASE-MAP.md).

### Watch list (potential regressions, not yet audited)

- `CCRConfig.enabled=False` end-to-end — **closed; semantics corrected (Defect 1)**. Both `enabled=False` and `inject_retrieval_marker=False` collapse to the same Rust `enable_ccr_marker=False` field, which gates NEITHER the recovery pointer NOR the store write — persistence and pointer emission are unconditional on every drop. The flag pair is the retrieval-tool advertisement preference (plus the router's CCR-offload gate). See the SmartCrusher table above.
- `SmartCrusherConfig.use_feedback_hints=False` — **closed**: the field (and its confidence-threshold sibling) was removed from both the Rust config struct and the pyo3 constructor after the Python feedback/learning system was deleted; nothing forwards or honors it anymore.

When any item above changes, update both this section and the test file. The shim's docstring also references this section — keep them aligned.

## Phase 0 Blockers

These are known limitations for Phase 0. They are tracked here so Phase 1
doesn't rediscover them.

- **`rust-toolchain.toml`** pins `channel = "stable"` rather than a specific
  version so CI picks up the same toolchain the local box uses. Tighten to a
  pinned version (e.g. `1.78`) once the port stabilizes.

## CCR storage — process-local, request-window-scoped

The CCR store (`crates/furl-core/src/ccr/backends/`) ships a single
backend: `InMemoryCcrStore`, a process-local sharded `DashMap` constructed once
at startup and shared across worker threads behind an `Arc`. Entries are bounded
(capacity-LRU + TTL, defaults 1000 entries / 300 s) and lost on restart, so
byte-exact CCR recovery is guaranteed only **within the process / request
window** — long enough for the `<<ccr:HASH>>` markers a single compression emits
to round-trip on the model's retrieval call.

No persistent or cross-process backend ships. Spilling evicted entries to a
durable store (keyed by session for cleanup) is a deliberately-deferred epic —
see `CCR-RETENTION.md` for the design and trade-offs. On the Python side,
`CompressionStore` exposes a `furl_ctx.ccr_backend` setuptools entry point so a
durable adapter can be registered out-of-tree (`FURL_CCR_BACKEND=<name>`),
but none ships by default and the in-memory backend is the only one wired.
