#!/usr/bin/env bash
set -euo pipefail
DEVKIT_ROOT="${FURL_DEVKIT_ROOT:-/mnt/data/furl-devkit}"
REPO_ROOT="${FURL_REPO_ROOT:-$(git rev-parse --show-toplevel)}"
source "$DEVKIT_ROOT/activate-furl-dev.sh"
cd "$REPO_ROOT"

printf '%s\n' '== versions =='
rustc --version
cargo --version
cargo fmt --version
cargo clippy --version
python --version
ruff --version
mypy --version
maturin --version
cargo audit --version
cargo deny --version
commitlint --version

printf '%s\n' '== package integrity =='
python -m pip check
python - <<'PY'
import re2, tiktoken
for name in ('o200k_base','cl100k_base'):
    tiktoken.get_encoding(name)
import tree_sitter_language_pack as p
for name in ('python','javascript','typescript','go','rust','java','c','cpp'):
    p.get_parser(name)
print('runtime caches OK')
PY

printf '%s\n' '== fast repo gates =='
cargo fmt --all -- --check
cargo clippy --workspace -- -D warnings
ruff check .
ruff format --check .
mypy furl_ctx --ignore-missing-imports
cargo deny check licenses
cargo audit

printf '%s\n' '== native extension =='
maturin develop --profile ci
python -c 'import furl_ctx._core; print("furl_ctx._core import OK")'

if [[ "${1:-}" == "--full" ]]; then
  cargo test --workspace
  for group in 1 2 3 4; do
    python -m pytest tests -q --splits 4 --group "$group"
  done
fi

echo 'furl dev environment: PASS'
