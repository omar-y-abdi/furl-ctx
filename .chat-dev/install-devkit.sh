#!/usr/bin/env bash
set -euo pipefail

ARTIFACT_DIR="${1:-/mnt/data}"
DEVKIT_ROOT="${FURL_DEVKIT_ROOT:-/mnt/data/furl-devkit}"
REPO_ROOT="${FURL_REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"

if [[ -z "$REPO_ROOT" || ! -f "$REPO_ROOT/pyproject.toml" ]]; then
  echo "error: run this script from a full furl-ctx checkout (or set FURL_REPO_ROOT)" >&2
  exit 2
fi

need_zip() {
  local name="$1"
  local path="$ARTIFACT_DIR/$name.zip"
  [[ -f "$path" ]] || { echo "error: missing $path" >&2; exit 3; }
  printf '%s\n' "$path"
}

extract_zip() {
  local zip="$1" dest="$2"
  rm -rf "$dest"
  mkdir -p "$dest"
  unzip -q -o "$zip" -d "$dest"
}

mkdir -p "$DEVKIT_ROOT" "$DEVKIT_ROOT/home" "$DEVKIT_ROOT/cache" "$DEVKIT_ROOT/bin" "$DEVKIT_ROOT/venvs" "$DEVKIT_ROOT/wheelhouses"
WORK="$DEVKIT_ROOT/.install"
rm -rf "$WORK"
mkdir -p "$WORK"

RUST_ZIP=$(need_zip chat-dev-rust)
PY312_ZIP=$(need_zip chat-dev-python312)
CACHE_ZIP=$(need_zip chat-dev-runtime-caches)
COMMITLINT_ZIP=$(need_zip chat-dev-commitlint)
extract_zip "$RUST_ZIP" "$WORK/rust"
extract_zip "$PY312_ZIP" "$WORK/py312"
extract_zip "$CACHE_ZIP" "$WORK/cache"
extract_zip "$COMMITLINT_ZIP" "$WORK/commitlint"

# Rust is a direct standalone sysroot; Cargo cache/tools live in the transported cargo home.
rm -rf "$DEVKIT_ROOT/rust-toolchain"
mkdir -p "$DEVKIT_ROOT/rust-toolchain"
tar --no-same-owner -C "$DEVKIT_ROOT/rust-toolchain" -xzf "$WORK/rust/rust-toolchain.tgz"
tar --no-same-owner -C "$DEVKIT_ROOT/home" -xzf "$WORK/rust/cargo-home.tgz"
mkdir -p "$DEVKIT_ROOT/home/.cargo"
tar --no-same-owner -C "$DEVKIT_ROOT/home/.cargo" -xzf "$WORK/rust/rustsec-db.tgz"

# Hosted CPython archives are safest at the prefix they were built for.
PY312_PREFIX=$(cat "$WORK/py312/python312-prefix.txt")
if [[ ! -x "$PY312_PREFIX/bin/python" ]]; then
  [[ -w /opt || $(id -u) -eq 0 ]] || {
    echo "error: Python runtime requires write access to /opt (current uid=$(id -u))" >&2
    exit 4
  }
  tar --no-same-owner -C / -xzf "$WORK/py312/python312.tgz"
fi

rm -rf "$DEVKIT_ROOT/wheelhouses/py312"
mkdir -p "$DEVKIT_ROOT/wheelhouses/py312"
tar --no-same-owner -C "$DEVKIT_ROOT/wheelhouses/py312" --strip-components=1 -xzf "$WORK/py312/python-wheelhouse.tgz"

# Core Python venv: all installs are offline and come from the generated wheelhouse.
rm -rf "$DEVKIT_ROOT/venvs/py312"
"$PY312_PREFIX/bin/python" -m venv "$DEVKIT_ROOT/venvs/py312"
PIP="$DEVKIT_ROOT/venvs/py312/bin/python"
"$PIP" -m pip install --no-index --find-links "$DEVKIT_ROOT/wheelhouses/py312" \
  -r "$DEVKIT_ROOT/wheelhouses/py312/requirements-resolved.txt"
"$PIP" -m pip install --no-index --find-links "$DEVKIT_ROOT/wheelhouses/py312" 'maturin>=1.9,<2.0'

# Runtime caches. Keep PRE_COMMIT_HOME at the exact path used when hook envs were built.
rm -rf "$DEVKIT_ROOT/cache/furl-tiktoken-cache" "$DEVKIT_ROOT/cache/tree-sitter-language-pack" /tmp/furl-precommit
mkdir -p "$DEVKIT_ROOT/cache"
tar --no-same-owner -C "$DEVKIT_ROOT/cache" -xzf "$WORK/cache/tiktoken-cache.tgz"
tar --no-same-owner -C /tmp -xzf "$WORK/cache/precommit-cache.tgz"
tar --no-same-owner -C "$DEVKIT_ROOT/cache" -xzf "$WORK/cache/tree-sitter-cache.tgz"
mkdir -p "$HOME/.cache"
rm -rf "$HOME/.cache/tree-sitter-language-pack"
ln -s "$DEVKIT_ROOT/cache/tree-sitter-language-pack" "$HOME/.cache/tree-sitter-language-pack"

rm -rf "$DEVKIT_ROOT/commitlint"
mkdir -p "$DEVKIT_ROOT/commitlint"
tar --no-same-owner -C "$DEVKIT_ROOT/commitlint" --strip-components=1 -xzf "$WORK/commitlint/commitlint.tgz"

# Make cargo-audit default to the transported RustSec snapshot rather than attempting network I/O.
REAL_AUDIT="$DEVKIT_ROOT/home/.cargo/bin/cargo-audit"
if [[ -x "$REAL_AUDIT" && ! -e "$DEVKIT_ROOT/home/.cargo/bin/cargo-audit-real" ]]; then
  mv "$REAL_AUDIT" "$DEVKIT_ROOT/home/.cargo/bin/cargo-audit-real"
fi
cat > "$DEVKIT_ROOT/bin/cargo-audit" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "audit" ]]; then shift; fi
REAL="${CARGO_HOME:?}/bin/cargo-audit-real"
if [[ "${1:-}" == "--version" || "${1:-}" == "-V" ]]; then exec "$REAL" --version; fi
exec "$REAL" audit --no-fetch --db "$CARGO_HOME/advisory-db" "$@"
EOF
chmod +x "$DEVKIT_ROOT/bin/cargo-audit"

install_compat() {
  local tag="$1"
  local zip="$ARTIFACT_DIR/chat-dev-${tag}.zip"
  [[ -f "$zip" ]] || return 0
  local d="$WORK/$tag"
  extract_zip "$zip" "$d"
  local version_file="$d/version-${tag}.txt"
  local prefix
  prefix=$(sed -n '2p' "$version_file")
  local py_archive="$d/python-${tag}.tgz"
  if [[ ! -x "$prefix/bin/python" ]]; then
    mkdir -p "$(dirname "$prefix")"
    tar --no-same-owner -C "$(dirname "$prefix")" -xzf "$py_archive"
  fi
  rm -rf "$DEVKIT_ROOT/wheelhouses/$tag" "$DEVKIT_ROOT/venvs/$tag"
  mkdir -p "$DEVKIT_ROOT/wheelhouses/$tag"
  tar --no-same-owner -C "$DEVKIT_ROOT/wheelhouses/$tag" --strip-components=1 -xzf "$d/wheelhouse-${tag}.tgz"
  "$prefix/bin/python" -m venv "$DEVKIT_ROOT/venvs/$tag"
  "$DEVKIT_ROOT/venvs/$tag/bin/python" -m pip install --no-index --find-links "$DEVKIT_ROOT/wheelhouses/$tag" \
    -r "$DEVKIT_ROOT/wheelhouses/$tag/requirements.txt"
}
install_compat py310
install_compat py314

cat > "$DEVKIT_ROOT/activate-furl-dev.sh" <<EOF
#!/usr/bin/env bash
export FURL_DEVKIT_ROOT="$DEVKIT_ROOT"
export FURL_REPO_ROOT="$REPO_ROOT"
export RUST_TOOLCHAIN_ROOT="$DEVKIT_ROOT/rust-toolchain"
export CARGO_HOME="$DEVKIT_ROOT/home/.cargo"
export TIKTOKEN_CACHE_DIR="$DEVKIT_ROOT/cache/furl-tiktoken-cache"
export TREE_SITTER_LANGUAGE_PACK_CACHE_DIR="$DEVKIT_ROOT/cache"
export PRE_COMMIT_HOME="/tmp/furl-precommit"
export CARGO_NET_OFFLINE=true
export PIP_NO_INDEX=1
export PIP_FIND_LINKS="$DEVKIT_ROOT/wheelhouses/py312"
export FURL_PY310="$DEVKIT_ROOT/venvs/py310/bin/python"
export FURL_PY312="$DEVKIT_ROOT/venvs/py312/bin/python"
export FURL_PY314="$DEVKIT_ROOT/venvs/py314/bin/python"
export VIRTUAL_ENV="$DEVKIT_ROOT/venvs/py312"
export NODE_PATH="$DEVKIT_ROOT/commitlint/node_modules"
export PATH="$DEVKIT_ROOT/bin:$DEVKIT_ROOT/rust-toolchain/bin:$DEVKIT_ROOT/venvs/py312/bin:$DEVKIT_ROOT/commitlint/node_modules/.bin:$DEVKIT_ROOT/home/.cargo/bin:\$PATH"
EOF
chmod +x "$DEVKIT_ROOT/activate-furl-dev.sh"

cp "$WORK/rust/manifest.txt" "$DEVKIT_ROOT/bootstrap-manifest.txt" 2>/dev/null || true
rm -rf "$WORK"

echo "Installed furl devkit at $DEVKIT_ROOT"
echo "Activate with: source $DEVKIT_ROOT/activate-furl-dev.sh"
