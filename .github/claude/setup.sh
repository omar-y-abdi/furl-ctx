#!/usr/bin/env bash
set -euo pipefail

# Environment setup for Claude Code web sessions on furl-ctx.
# Wire it into the web environment config as: bash .github/claude/setup.sh
# Idempotent and safe to re-run. Installs the Rust toolchain, uv, a project
# venv, and builds the native extension in release mode so tests, verify.run
# and benchmarks work immediately when the agent starts.

log() { printf '[setup] %s\n' "$*"; }

if ! command -v cargo >/dev/null 2>&1; then
  log "installing rust toolchain (stable, minimal profile)"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable
fi
# shellcheck disable=SC1091
. "$HOME/.cargo/env" 2>/dev/null || export PATH="$HOME/.cargo/bin:$PATH"
rustup component add rustfmt clippy >/dev/null 2>&1 || true

if ! command -v uv >/dev/null 2>&1; then
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if [ ! -d .venv ]; then
  log "creating project venv (python 3.12)"
  uv venv .venv --python 3.12 --seed
fi
# shellcheck disable=SC1091
. .venv/bin/activate

log "building native extension in release mode with dev,mcp extras"
pip install --quiet --upgrade pip 'maturin>=1.9,<2.0'
maturin develop --release --extras dev,mcp
pip install --quiet google-re2 pytest-split

log "sanity checks"
python -c "import furl_ctx"
python -c "import re2"
cargo fmt --version >/dev/null
cargo clippy --version >/dev/null
log "setup OK: furl_ctx importable, re2 present, rust toolchain ready"
