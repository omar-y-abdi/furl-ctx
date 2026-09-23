#!/usr/bin/env bash
# Vercel runs this inside its build virtualenv; the resulting wheel is vendored.
set -euo pipefail
cd "$(dirname "$0")"
root="$(cd ../.. && pwd)"
if [[ ! -f "$root/crates/furl-py/Cargo.toml" ]]; then
  echo 'Enable "Include source files outside of the Root Directory" for deploy/remote.' >&2
  exit 1
fi
export RUSTUP_TOOLCHAIN
RUSTUP_TOOLCHAIN="$(python -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1],"rb"))["toolchain"]["channel"])' "$root/rust-toolchain.toml")"
export PATH="$HOME/.cargo/bin:$PATH"
build_dir="$(mktemp -d)"
trap 'rm -rf "$build_dir"' EXIT
if ! command -v rustup >/dev/null; then
  installer="$build_dir/rustup.sh"
  curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o "$installer"
  sh "$installer" -y --profile minimal --default-toolchain "$RUSTUP_TOOLCHAIN" --no-modify-path
fi
rustup toolchain install "$RUSTUP_TOOLCHAIN" --profile minimal
uv pip install -r requirements-vercel.txt maturin==1.15.0
(cd "$root" && maturin build --release --locked --out "$build_dir")
uv pip install --no-deps "$build_dir/"*.whl
uv pip uninstall maturin
export TIKTOKEN_CACHE_DIR="$PWD/tokenizers"
python -c 'import furl_ctx._core, tiktoken; [tiktoken.get_encoding(n) for n in ("o200k_base", "cl100k_base")]'
