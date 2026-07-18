#!/usr/bin/env bash
set -euo pipefail

# Environment setup for Claude Code web sessions on furl-ctx.
# The cloud sandbox runs this from the HOME directory, not from the checkout,
# so the script locates the repo itself before doing anything. Idempotent.

log() { printf '[setup] %s\n' "$*"; }

# --- 1. Locate the furl-ctx checkout -------------------------------------
# Fingerprint: a directory holding both pyproject.toml (name = "furl-ctx")
# and the workspace Cargo.toml. Searches cwd, then one and two levels under
# HOME, which covers every layout the sandbox uses.
find_root() {
  local d
  for d in "$PWD/" "$HOME"/*/ "$HOME"/*/*/; do
    if [ -f "${d}pyproject.toml" ] && [ -f "${d}Cargo.toml" ] \
       && grep -q '^name = "furl-ctx"' "${d}pyproject.toml" 2>/dev/null; then
      printf '%s' "${d%/}"
      return 0
    fi
  done
  return 1
}

if ! ROOT="$(find_root)"; then
  log "ERROR: could not locate the furl-ctx checkout starting from $PWD"
  log "HOME contents for debugging:"
  ls -la "$HOME" >&2
  exit 1
fi
log "repo root: $ROOT"
cd "$ROOT"

# --- 2. Rust toolchain -----------------------------------------------------
if ! command -v cargo >/dev/null 2>&1; then
  log "installing rust toolchain (stable, minimal profile)"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable
fi
# shellcheck disable=SC1091
. "$HOME/.cargo/env" 2>/dev/null || export PATH="$HOME/.cargo/bin:$PATH"
rustup component add rustfmt clippy >/dev/null 2>&1 || true

# --- 3. uv -------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# --- 4. Project venv + native extension ----------------------------------
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

# --- 5. Sanity ------------------------------------------------------------
log "sanity checks"
python -c "import furl_ctx"
python -c "import re2"
cargo fmt --version >/dev/null
cargo clippy --version >/dev/null
log "setup OK: root=$ROOT, furl_ctx importable, re2 present, rust toolchain ready"
