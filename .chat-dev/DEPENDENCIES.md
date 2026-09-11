# Furl development dependency contract

The permanent `chat-dev-bootstrap` workflow must make these capabilities available offline in a ChatGPT sandbox.

## Primary toolchain

- Rust/Cargo 1.95.0 from `rust-toolchain.toml`, including rustfmt and Clippy.
- cargo-audit and cargo-deny plus Cargo registry/source cache for `Cargo.lock` and a local RustSec DB snapshot.
- CPython 3.12 for primary development, plus Python 3.10 and 3.14 compatibility runtimes.
- Maturin (`>=1.9,<2.0`) and the project extras `dev,mcp,code`.
- CI lint pins: Ruff 0.16.2 and Mypy 1.14.1.
- pytest, pytest-cov, pytest-asyncio, pytest-split, PyYAML, Google RE2.
- tiktoken with warmed `o200k_base` and `cl100k_base` data.
- tree-sitter and tree-sitter-language-pack with parsers cached for Python, JavaScript, TypeScript, Go, Rust, Java, C and C++.
- pre-commit with its configured hook environments warmed.
- Node/npm commitlint CLI + conventional config.
- Twine, auditwheel, patchelf, pyelftools and packaging for distribution inspection.

The base sandbox already normally supplies Git, GCC/G++, CMake, Ninja, Node/npm, jq and uv; verify instead of assuming.

## Artifact contract

- `chat-dev-repo-bundle` — complete Git bundle (`git bundle create --all`) for native local Git work.
- `chat-dev-rust` — Rust sysroot, Cargo home/cache, RustSec DB and manifest.
- `chat-dev-python312` — CPython 3.12 runtime, resolved wheelhouse and manifest.
- `chat-dev-runtime-caches` — tiktoken, tree-sitter-language-pack and pre-commit runtime caches.
- `chat-dev-commitlint` — local npm commitlint installation.
- `chat-dev-py310` / `chat-dev-py314` — compatibility Python runtimes and version-specific wheelhouses.

Artifacts use 90-day retention subject to repository policy. Regenerate by changing `.chat-dev/BOOTSTRAP_TRIGGER` or manually dispatching the workflow.

## Known sandbox limits that packages cannot fix

- Direct DNS/network may be blocked. Use the GitHub connector/artifact bridge.
- Docker CLI without a daemon/socket is not useful. Run manylinux/container release gates in hosted GitHub Actions.
- Native `git push/fetch` can remain unavailable even when local Git is fully functional. Publish verified local Git objects through the GitHub connector.
