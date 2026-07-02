# Furl Rust build targets. `just` is not installed on dev boxes; this
# Makefile is the source of truth and is mirrored by .github/workflows/rust.yml.

SHELL := /bin/bash
CARGO ?= cargo
MATURIN ?= maturin
PYTHON ?= python3

.PHONY: help test bench build-wheel fmt fmt-check lint clippy clean ci-precheck ci-precheck-rust ci-precheck-python ci-precheck-commitlint install-git-hooks verify-rust-core

help:
	@echo "Furl Rust targets:"
	@echo "  make test               - cargo test --workspace"
	@echo "  make bench              - cargo bench --workspace"
	@echo "  make build-wheel        - release wheel for furl-py"
	@echo "  make verify-rust-core   - build + install + import-verify furl_ctx._core"
	@echo "  make fmt                - cargo fmt --all"
	@echo "  make fmt-check          - cargo fmt --all -- --check"
	@echo "  make lint               - cargo clippy --workspace -- -D warnings"
	@echo "  make clean              - cargo clean"
	@echo ""
	@echo "Pre-push verification (run BEFORE git push to catch CI failures locally):"
	@echo "  make ci-precheck        - run all CI gates (rust + python + commitlint)"
	@echo "  make ci-precheck-rust   - cargo fmt --check + clippy + test"
	@echo "  make ci-precheck-python - build the extension + run the python suite"
	@echo "  make ci-precheck-commitlint - lint commits since origin/main"
	@echo "  make install-git-hooks  - install pre-commit hooks (ruff/format/mypy on every commit)"

test:
	$(CARGO) test --workspace

bench:
	$(CARGO) bench --workspace

build-wheel:
	$(MATURIN) build --release -m crates/furl-py/Cargo.toml

# maturin-develop + import-verify in one shot. Run this any time you suspect
# the engine is silently falling back to Python-only mode because the compiled
# `furl_ctx._core` extension is stale or unbuilt. SmartCrusher and the
# diff/log/search compressors hard-import `furl_ctx._core`, so a missing
# extension is a hard ImportError, not a silent degrade.
verify-rust-core:
	@if [ -z "$$VIRTUAL_ENV" ]; then \
		echo "error: activate a venv first (e.g. source .venv/bin/activate)"; \
		exit 1; \
	fi
	bash scripts/build_rust_extension.sh

fmt:
	$(CARGO) fmt --all

fmt-check:
	$(CARGO) fmt --all -- --check

clippy lint:
	$(CARGO) clippy --workspace -- -D warnings

clean:
	$(CARGO) clean

# ─── Pre-push CI gate ──────────────────────────────────────────────────────
#
# These targets run the same checks GitHub Actions runs, locally. The intent
# is: if `make ci-precheck` is green, `git push` will not turn red.
#
# Run `make ci-precheck` before EVERY `git push`. Install the pre-commit hooks
# (ruff/format/mypy on every commit) one-time with:
#   make install-git-hooks

ci-precheck: ci-precheck-rust ci-precheck-python ci-precheck-commitlint
	@echo ""
	@echo "✅ ci-precheck PASSED — safe to push."

ci-precheck-rust:
	@echo "── ci-precheck-rust ────────────────────────────────────────────"
	$(CARGO) fmt --all -- --check
	$(CARGO) clippy --workspace -- -D warnings
	$(CARGO) test --workspace

# Builds the Rust extension first because most tests instantiate `SmartCrusher`
# / the compressors, which hard-import `furl_ctx._core`.
ci-precheck-python:
	@echo "── ci-precheck-python ─────────────────────────────────────────"
	@if [ -z "$$VIRTUAL_ENV" ]; then \
		echo "error: activate a venv first (e.g. source .venv/bin/activate)"; \
		exit 1; \
	fi
	ruff check .
	ruff format --check .
	mypy furl_ctx --ignore-missing-imports
	bash scripts/build_rust_extension.sh
	$(PYTHON) -m pytest tests/ -q

# Lint commits since `origin/main`. Requires npx (Node 18+) on PATH.
# Skips silently if npx is unavailable; install nodejs to enable.
ci-precheck-commitlint:
	@echo "── ci-precheck-commitlint ─────────────────────────────────────"
	@if ! command -v npx >/dev/null 2>&1; then \
		echo "skip: npx not on PATH (install node 18+ to enable commitlint pre-check)"; \
		exit 0; \
	fi
	@if ! git rev-parse --verify origin/main >/dev/null 2>&1; then \
		echo "skip: origin/main not fetched (run 'git fetch origin main')"; \
		exit 0; \
	fi
	npx --yes --package=@commitlint/cli --package=@commitlint/config-conventional -- \
		commitlint --from origin/main --to HEAD --config .commitlintrc.json

install-git-hooks:
	@pre-commit install
