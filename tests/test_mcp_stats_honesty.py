"""COR-36 — the MCP stats surface must not lie to the operator.

Three lies, one fix each (furl_ctx/ccr/mcp_server.py):

1. ``_compress_content`` ignored ``result.error``: a fail-open compress
   (original messages back, tokens_after=0) was stored as "compressed" and
   booked as savings_percent=100. Error results must return an error-shaped
   payload, skip the store write, AND skip the stats record.
2. ``_handle_read`` cache hits booked fictional savings into the compression
   totals via ``record_compression(cached_tokens, 5, "read_cache_hit")``.
   Hits now land in dedicated ``cache_hits``/``cache_tokens_avoided``
   counters (additive keys; existing keys keep their meaning).
3. ``original_tokens`` for furl_read entries was a whitespace word count fed
   as a token count. It is now counted by the same tokenizer compress() uses.

The TOIN/telemetry parts of the original finding are moot (telemetry
excised); the local MCP stats surface pinned here is what remains.
"""

from __future__ import annotations

import hashlib
import importlib
import json

import pytest

pytest.importorskip("mcp")

from furl_ctx.cache.compression_store import (  # noqa: E402
    get_compression_store,
    reset_compression_store,
)
from furl_ctx.ccr.mcp_server import FurlMCPServer  # noqa: E402
from furl_ctx.compress import CompressResult  # noqa: E402
from furl_ctx.tokenizers import get_tokenizer  # noqa: E402

# The model the MCP server compresses (and therefore counts) with.
_SERVER_MODEL = "claude-sonnet-4-5-20250929"

# furl_ctx/__init__.py re-exports the `compress` function, shadowing the
# submodule attribute — resolve the real module object for monkeypatching.
compress_mod = importlib.import_module("furl_ctx.compress")


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    # Mirror test_mcp_server_handlers isolation: workspace jail in tmp (the
    # sqlite default backend AND the shared-stats file — whose path follows
    # FURL_WORKSPACE_DIR per call, SEC-7 — then live under the sandboxed
    # workspace), fresh store singleton around every test.
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("FURL_CCR_BACKEND", raising=False)
    reset_compression_store()
    yield
    reset_compression_store()


@pytest.fixture
def server() -> FurlMCPServer:
    return FurlMCPServer()


def _patch_compress_error(monkeypatch, *, tokens_before: int, error: str) -> None:
    def fake_compress(messages, **kwargs):
        # Mirror compress()'s fail-open contract (compress.py error path):
        # ORIGINAL messages back, tokens_after=0, error set.
        return CompressResult(
            messages=messages,
            tokens_before=tokens_before,
            tokens_after=0,
            error=error,
        )

    monkeypatch.setattr(compress_mod, "compress", fake_compress)


# ─── 1. error result → error payload, no store write, no stats record ───────


def test_error_result_returns_error_shaped_payload(server, monkeypatch) -> None:
    _patch_compress_error(monkeypatch, tokens_before=123, error="pipeline exploded")

    out = server._compress_content("payload that fails")

    assert "error" in out
    assert "pipeline exploded" in out["error"]
    # Nothing was stored, so no retrieval hash may be advertised.
    assert "hash" not in out
    assert "compressed" not in out


def test_error_result_skips_store_write(server, monkeypatch) -> None:
    payload = "original that must NOT be stored as compressed"
    _patch_compress_error(monkeypatch, tokens_before=50, error="boom")

    server._compress_content(payload)

    store = get_compression_store()
    # The default-hash key the skipped store write would have used: on error
    # the old code persisted the ORIGINAL under this key with 0 tokens.
    key = hashlib.sha256(payload.encode()).hexdigest()[:24]
    assert not store.exists(key)
    assert store.get_stats()["entry_count"] == 0


def test_error_result_skips_stats_record(server, monkeypatch) -> None:
    _patch_compress_error(monkeypatch, tokens_before=1000, error="boom")

    server._compress_content("x" * 100)

    stats = server._stats
    # The old code recorded (1000, 0) → savings_percent=100 with 1000 tokens
    # "saved" — pure fiction. Nothing may be recorded on the error path.
    assert stats.compressions == 0
    assert stats.total_input_tokens == 0
    assert stats.total_tokens_saved == 0
    assert stats.events == []


async def test_error_payload_reaches_host_envelope(server, monkeypatch) -> None:
    # Host-visible shape: the error payload must ship through _handle_compress
    # as the JSON envelope an MCP host actually receives.
    _patch_compress_error(monkeypatch, tokens_before=7, error="boom")

    result = await server._handle_compress({"content": "irrelevant"})

    env = json.loads(result[0].text)
    assert "error" in env
    assert "hash" not in env


# ─── 2. cache hits land in dedicated counters, not compression totals ───────


async def test_cache_hit_uses_separate_counters(server, tmp_path) -> None:
    content = "alpha beta gamma delta epsilon zeta eta theta"
    f = tmp_path / "cached.txt"
    f.write_text(content)

    await server._handle_read({"file_path": str(f)})  # fresh read caches
    env = json.loads((await server._handle_read({"file_path": str(f)}))[0].text)
    assert env["status"] == "cached"  # sanity: second read hit the cache

    stats = server._stats
    expected_tokens = get_tokenizer(_SERVER_MODEL).count_text(content)
    assert stats.cache_hits == 1
    assert stats.cache_tokens_avoided == max(0, expected_tokens - 5)
    # Compression totals stay untouched by hits (the old code booked the hit
    # as a compression: compressions=1, input=cached_tokens, saved=cached-5).
    assert stats.compressions == 0
    assert stats.total_input_tokens == 0
    assert stats.total_output_tokens == 0
    assert stats.total_tokens_saved == 0


async def test_cache_hit_counters_surface_in_stats_dict(server, tmp_path) -> None:
    f = tmp_path / "cached.txt"
    f.write_text("some cached file body with several words in it")
    await server._handle_read({"file_path": str(f)})
    await server._handle_read({"file_path": str(f)})

    d = server._stats.to_dict()

    assert d["cache_hits"] == 1
    assert d["cache_tokens_avoided"] == server._stats.cache_tokens_avoided
    assert d["cache_tokens_avoided"] > 0
    # Additive shape change: the existing keys keep their meaning — a session
    # of pure cache hits performed zero compressions and saved zero
    # compression tokens.
    assert d["compressions"] == 0
    assert d["total_tokens_saved"] == 0
    assert d["savings_percent"] == 0


# ─── 3. original_tokens comes from the tokenizer, not a word count ──────────


async def test_read_original_tokens_uses_tokenizer_not_word_count(server, tmp_path) -> None:
    # Structured log line: word-split gives 3 "words" but tiktoken produces
    # 15 tokens (punctuation, path separators, number literals each get their
    # own token). This divergence proves we're using the real tokenizer, not
    # a word count. Pin is in o200k_base (claude-* uses it since Q1).
    content = "HTTP/2.0 status_code=404 path=/api/v1/users"  # 3 words, 15 o200k tokens
    f = tmp_path / "counted.txt"
    f.write_text(content)

    await server._handle_read({"file_path": str(f)})

    # furl_read stores under the default content hash — recompute it.
    key = hashlib.sha256(content.encode()).hexdigest()[:24]
    entry = get_compression_store().retrieve(key)
    assert entry is not None

    expected = get_tokenizer(_SERVER_MODEL).count_text(content)
    word_count = len(content.split())
    # Known-string pin: claude-* now uses o200k_base (Q1).
    # "HTTP/2.0 status_code=404 path=/api/v1/users" → 15 tokens, 3 words.
    assert expected == 15
    assert expected != word_count
    assert entry.original_tokens == expected
    assert entry.original_tokens != word_count
