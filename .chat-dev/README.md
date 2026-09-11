# chat-dev bootstrap branch

`chat-dev` exists for ChatGPT/agent development in network-restricted sandboxes. It is **not** the base branch for product changes. Product work starts from the current remote `main`; this branch only carries bootstrap tooling, agent instructions, and a reproducible offline dev environment.

## Fresh-chat bootstrap

1. Read `CHAT_DEV_SYSTEM_PROMPT.md` and `.chat-dev/skills/speculative-tool-calling/{SKILL,TESTS,REFERENCES}.md`.
2. Try native GitHub access once. If `git clone/fetch` fails because GitHub DNS/network is blocked, do not retry. Use the GitHub connector.
3. Find the latest successful `chat-dev-bootstrap` run on branch `chat-dev`.
4. Download artifacts with these exact filenames into `/mnt/data`:
   - `chat-dev-repo-bundle.zip`
   - `chat-dev-rust.zip`
   - `chat-dev-python312.zip`
   - `chat-dev-runtime-caches.zip`
   - `chat-dev-commitlint.zip`
   - optionally `chat-dev-py310.zip` and `chat-dev-py314.zip`
5. If artifacts expired, re-run the latest `chat-dev-bootstrap` job(s). If re-run is unavailable, make a harmless update to `.chat-dev/BOOTSTRAP_TRIGGER` on `chat-dev`; the push rebuilds artifacts.
6. Materialize/download connector files so the exact ZIPs exist under `/mnt/data`, then run:

```bash
bash /path/to/checkout/.chat-dev/install-devkit.sh /mnt/data
source /mnt/data/furl-devkit/activate-furl-dev.sh
cd /mnt/data/furl-ctx
bash .chat-dev/verify-furl-dev.sh
git switch main
```

If the checkout does not exist yet, run `.chat-dev/bootstrap-repo-from-bundle.sh` from any temporary `chat-dev` checkout/materialized copy. The created repo intentionally lands on local `chat-dev` so its bootstrap scripts are present. After installation/verification, compare local `main` with the remote `main` SHA you read through the GitHub connector, refresh if stale, then `git switch main` before creating a product-work branch.

## Expected capabilities

The generated environment contains Rust 1.95.0, Cargo, rustfmt, Clippy, cargo-audit, cargo-deny, Python 3.12 plus all `dev/mcp/code` extras, exact CI Ruff/Mypy pins, Maturin, RE2, pytest/pytest-split, pre-commit, tiktoken caches, all eight tree-sitter parser caches, commitlint, Twine, auditwheel and patchelf. Optional artifacts add Python 3.10 and 3.14 compatibility runtimes.

Direct GitHub/PyPI/crates.io access may still be blocked. The environment is designed to build/test offline after artifact import. Docker remains unavailable when the sandbox has no daemon/socket; run container/release jobs through GitHub Actions instead.

## Source of truth

- Architecture/navigation: `CODEBASE-MAP.md`
- Python packaging/tool versions: `pyproject.toml`, `uv.lock`
- Rust workspace/toolchain: `Cargo.toml`, `Cargo.lock`, `rust-toolchain.toml`
- Local gates: `Makefile`, `.pre-commit-config.yaml`, `.commitlintrc.json`
- Hosted CI truth: `.github/workflows/ci.yml`, `.github/workflows/rust.yml`
- Rust-specific guide: `RUST_DEV.md`
- Contribution policy: `CONTRIBUTING.md`

When these files change on `main`, treat `chat-dev` as potentially stale and refresh its bootstrap recipes before relying on exact dependency parity.
