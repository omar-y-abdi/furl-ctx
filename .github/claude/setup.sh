#!/usr/bin/env bash
set -euo pipefail

# Environment setup for Claude Code web sessions on furl-ctx.
# Two-phase design for the cloud sandbox, where this may run BEFORE the
# checkout exists and from an arbitrary cwd:
#   Phase A, always: make the global toolchain ready, rust plus uv.
#   Phase B, only when the checkout is found: venv, native build, sanity.
# When no checkout exists yet this exits 0 on purpose. The session prompt
# re-runs this script from inside the repo on first use, and then Phase B
# fires because cwd itself matches. Idempotent throughout.

log() { printf '[setup] %s\n' "$*"; }

is_repo_root() {
  [ -f "$1/pyproject.toml" ] && [ -f "$1/Cargo.toml" ] \
    && grep -q '^name = "furl-ctx"' "$1/pyproject.toml" 2>/dev/null
}

find_root() {
  # Strategy 1: environment hints various sandboxes set.
  local hint
  for hint in "${CLAUDE_PROJECT_DIR:-}" "${PROJECT_DIR:-}" "${WORKSPACE:-}" \
              "${WORKSPACE_DIR:-}" "${REPO_DIR:-}" "${GITHUB_WORKSPACE:-}"; do
    if [ -n "$hint" ] && is_repo_root "$hint"; then printf '%s' "$hint"; return 0; fi
  done
  # Strategy 2: cwd and common locations, shallow globs.
  local d
  for d in "$PWD" "$PWD"/*/ "$HOME"/*/ "$HOME"/*/*/ \
           /workspace /workspace/*/ /workspaces/*/ /repo /repo/*/ \
           /project /project/*/ /app /code /src /srv/*/ \
           /home/*/ /home/*/*/ /root/*/ ; do
    d="${d%/}"
    if is_repo_root "$d"; then printf '%s' "$d"; return 0; fi
  done
  # Strategy 3: bounded filesystem sweep.
  local f
  while IFS= read -r f; do
    if grep -q '^name = "furl-ctx"' "$f" 2>/dev/null; then
      dirname "$f"
      return 0
    fi
  done < <(find / -maxdepth 5 -name pyproject.toml \
             -not -path '/proc/*' -not -path '/sys/*' -not -path '/dev/*' \
             -not -path '*/node_modules/*' -not -path '*/.venv/*' \
             -not -path '*/.cache/*' -not -path '*/.rustup/*' \
             -not -path '*/.cargo/*' 2>/dev/null)
  return 1
}

# --- Phase A: global toolchain, always ------------------------------------
if ! command -v cargo >/dev/null 2>&1; then
  # shellcheck disable=SC1091
  . "$HOME/.cargo/env" 2>/dev/null || export PATH="$HOME/.cargo/bin:$PATH"
fi
if ! command -v cargo >/dev/null 2>&1; then
  log "installing rust toolchain (stable, minimal profile)"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable
  # shellcheck disable=SC1091
  . "$HOME/.cargo/env" 2>/dev/null || export PATH="$HOME/.cargo/bin:$PATH"
fi
rustup component add rustfmt clippy >/dev/null 2>&1 || true

if ! command -v uv >/dev/null 2>&1; then
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
log "phase A done: cargo=$(command -v cargo || echo missing) uv=$(command -v uv || echo missing)"

# --- Locate the checkout ---------------------------------------------------
if ! ROOT="$(find_root)"; then
  log "checkout not present at setup time; this sandbox clones the repo later."
  log "Global toolchain is ready. The in-repo bootstrap covers the rest:"
  log "run 'bash .github/claude/setup.sh' from the checkout on first use."
  log "diagnostics for the record: PWD=$PWD"
  env | grep -iE '^(CLAUDE|WORKSPACE|PROJECT|REPO|GITHUB)[A-Z_]*=' >&2 || true
  ls / >&2 2>/dev/null || true
  ls /home >&2 2>/dev/null || true
  exit 0
fi
log "repo root: $ROOT"
cd "$ROOT"

# --- Phase B: project venv + native extension -----------------------------
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
log "setup OK: root=$ROOT, furl_ctx importable, re2 present, rust toolchain ready"
