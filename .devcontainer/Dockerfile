ARG VARIANT=3.12-bookworm
FROM mcr.microsoft.com/devcontainers/python:1-${VARIANT}

# A manual rustup install would leave the shared Cargo registry root-owned, so the first maturin build as `vscode` would fail with `Permission denied`.
RUN rm -f /etc/apt/sources.list.d/yarn.list

RUN python -m pip install --no-cache-dir 'uv>=0.7.0' 'maturin>=1.5,<2.0'
