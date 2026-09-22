FROM rust:1.95.0-bookworm AS rust

FROM python:3.12.14-slim-bookworm AS build
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=rust /usr/local/cargo /usr/local/cargo
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo PATH=/usr/local/cargo/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY Cargo.toml Cargo.lock rust-toolchain.toml pyproject.toml README.md LICENSE NOTICE ./
COPY crates ./crates
COPY furl_ctx ./furl_ctx
RUN python -m pip install --no-cache-dir maturin==1.15.0 \
    && maturin build --release --locked --out /wheels
COPY deploy/remote/requirements.txt /requirements.txt
RUN python -m pip wheel --wheel-dir /wheels -r /requirements.txt

FROM python:3.12.14-slim-bookworm
COPY --from=build /wheels /wheels
COPY deploy/remote/requirements.txt /requirements.txt
RUN python -m pip install --no-index --find-links=/wheels -r /requirements.txt \
    && python -m pip install --no-index --no-deps /wheels/furl_ctx-*.whl \
    && rm -rf /wheels /root/.cache
ENV TIKTOKEN_CACHE_DIR=/opt/furl/tokenizer-cache \
    PYTHONPATH=/opt/furl \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FURL_REMOTE_DATA_DIR=/var/lib/furl
RUN mkdir -p /opt/furl/tokenizer-cache /var/lib/furl \
    && python -c "import tiktoken; [tiktoken.get_encoding(n) for n in ('o200k_base', 'cl100k_base')]" \
    && useradd --uid 10001 --no-create-home --home-dir /var/lib/furl furl \
    && chown 10001:10001 /var/lib/furl && chmod 700 /var/lib/furl \
    && chmod -R a+rX /opt/furl
COPY deploy/remote/furl_remote /opt/furl/furl_remote
USER 10001:10001
WORKDIR /opt/furl
EXPOSE 8000
CMD ["python", "-m", "furl_remote"]
