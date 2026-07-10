"""MCP integration tests for cross-store search (NR2-2 feature a).

``furl_retrieve`` called WITHOUT a hash but WITH a query runs a BM25-ranked
full-text search across every stored entry and returns
``(hash, score, preview)`` matches so the caller can follow up with a per-hash
retrieve. Drives the real handler against a real in-process store, asserting
the JSON envelope an MCP host receives.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

import mcp.types as mt  # noqa: E402

from furl_ctx.cache.compression_store import (  # noqa: E402
    reset_compression_store,
)
from furl_ctx.ccr.mcp_server import FurlMCPServer  # noqa: E402

# Hook-safe split literal (no verbatim secret in source).
_API_KEY = "sk" + "-" + "abcdefghijklmnopqrstuvwx"


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    # In-memory backend + per-test workspace so entries never leak between
    # tests and the shared-stats file lands in the sandbox.
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("FURL_CCR_BACKEND", "memory")
    reset_compression_store()
    yield
    reset_compression_store()


@pytest.fixture
def server() -> FurlMCPServer:
    return FurlMCPServer()


def _envelope(result: list[mt.TextContent]) -> dict:
    assert len(result) == 1, f"expected one TextContent, got {result!r}"
    item = result[0]
    assert isinstance(item, mt.TextContent)
    return json.loads(item.text)


def _seed(server: FurlMCPServer, hash_key: str, content: str) -> None:
    server._get_local_store().store(original=content, compressed="c", explicit_hash=hash_key)


async def test_no_hash_with_query_searches_across_all_entries(server) -> None:
    _seed(server, "a" * 12, "the quick brown fox jumps")
    _seed(server, "b" * 12, "alpha needleword beta gamma")
    _seed(server, "c" * 12, "entirely unrelated text")

    env = _envelope(await server._handle_retrieve({"query": "needleword"}))

    assert env["source"] == "cross_store"
    assert env["query"] == "needleword"
    assert env["count"] == 1
    assert env["matches"][0]["hash"] == "b" * 12
    assert "score" in env["matches"][0]
    assert "preview" in env["matches"][0]


async def test_cross_store_search_ranks_by_relevance(server) -> None:
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    _seed(server, "a" * 12, "error in module one")
    _seed(server, "b" * 12, "error in module two")
    _seed(server, "c" * 12, f"error record {uuid} present")

    env = _envelope(await server._handle_retrieve({"query": f"error {uuid}"}))

    # The unique-term entry ranks first; scores are sorted descending.
    assert env["matches"][0]["hash"] == "c" * 12
    scores = [m["score"] for m in env["matches"]]
    assert scores == sorted(scores, reverse=True)


async def test_cross_store_preview_redacts_secret(server) -> None:
    # The load-bearing redaction test: a secret in a stored original must not
    # surface in a cross-store preview (per-hash retrieval's log would mask it).
    _seed(server, "a" * 12, f"config needleword {_API_KEY} trailing text")

    env = _envelope(await server._handle_retrieve({"query": "needleword"}))

    preview = env["matches"][0]["preview"]
    assert _API_KEY not in preview, f"secret leaked into preview: {preview!r}"
    assert "[REDACTED]" in preview


async def test_cross_store_search_no_matches_reports_empty(server) -> None:
    _seed(server, "a" * 12, "some stored content")

    env = _envelope(await server._handle_retrieve({"query": "zzz_nomatch_zzz"}))

    assert env["source"] == "cross_store"
    assert env["count"] == 0
    assert env["matches"] == []
    assert "note" in env


async def test_no_hash_no_query_still_errors(server) -> None:
    # Byte-identical to before the feature: a bare retrieve needs a target.
    env = _envelope(await server._handle_retrieve({}))
    assert env == {"error": "hash parameter is required"}


async def test_no_hash_query_rejects_filter_keys_and_query_only_still_works(server) -> None:
    # Schema-honesty symmetry with the with-hash path: filters project a single
    # entry and require a hash. Before this pin, {query, select_field} with no
    # hash SILENTLY ignored the filter and ran a plain cross-store search.
    _seed(server, "a" * 12, "alpha needleword beta gamma")

    # Direction 1: query + a select filter (no hash) errors cleanly.
    env = _envelope(await server._handle_retrieve({"query": "needleword", "select_field": "kind"}))
    assert "error" in env
    assert "require a hash" in env["error"]

    # A line filter (the other filter family) is rejected the same way.
    env2 = _envelope(await server._handle_retrieve({"query": "needleword", "pattern": "beta"}))
    assert "error" in env2
    assert "require a hash" in env2["error"]

    # Direction 2: query-only still runs the cross-store search, untouched.
    ok = _envelope(await server._handle_retrieve({"query": "needleword"}))
    assert ok["source"] == "cross_store"
    assert ok["count"] == 1
    assert ok["matches"][0]["hash"] == "a" * 12


async def test_cross_store_search_returned_hash_round_trips(server) -> None:
    # The whole point of returning a hash: the caller retrieves it in full next.
    _seed(server, "a" * 12, "findme needleword payload body")

    search = _envelope(await server._handle_retrieve({"query": "needleword"}))
    found_hash = search["matches"][0]["hash"]

    full = _envelope(await server._handle_retrieve({"hash": found_hash}))
    assert full["original_content"] == "findme needleword payload body"


async def test_non_string_query_rejected(server) -> None:
    env = _envelope(await server._handle_retrieve({"query": 123}))
    assert env["error"].startswith("query parameter must be a string")


async def test_mcp_search_reaches_cross_session_sqlite_entry(tmp_path, monkeypatch) -> None:
    """End-to-end: the MCP server (default SQLite backend) finds an entry a
    DIFFERENT session wrote to the shared workspace db.

    Overrides the autouse fixture's memory backend so the server builds its
    real durable SQLite default, then seeds the shared db file through a
    separate ``SqliteBackend`` store (standing in for another session). The
    handler's cross-store search must surface that entry.
    """
    from furl_ctx.cache.backends.sqlite import SqliteBackend
    from furl_ctx.cache.compression_store import CompressionStore

    db_path = tmp_path / "ccr.sqlite3"
    monkeypatch.delenv("FURL_CCR_BACKEND", raising=False)  # use the SQLite default
    monkeypatch.setenv("FURL_CCR_SQLITE_PATH", str(db_path))
    reset_compression_store()

    # "Another session" writes to the same db file.
    other = CompressionStore(backend=SqliteBackend(db_path=db_path))
    other.store(
        original="prior session needleword body",
        compressed="c",
        explicit_hash="a" * 12,
    )

    server = FurlMCPServer()
    env = _envelope(await server._handle_retrieve({"query": "needleword"}))

    assert env["source"] == "cross_store"
    assert env["matches"][0]["hash"] == "a" * 12
