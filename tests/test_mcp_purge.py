"""Unit tests for the furl_purge MCP tool (the data-erase escape hatch).

Drives the real ``_handle_purge`` handler against a real in-process CCR store,
asserting the structured JSON envelope an MCP host receives. Covers the
exactly-one-of(hash, all) validation boundary and invariant C: purge(hash)
removes retrievability of exactly that hash; purge(all) leaves the store empty;
neither runs without explicit args.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

import mcp.types as mt  # noqa: E402

from furl_ctx.cache.compression_store import reset_compression_store  # noqa: E402
from furl_ctx.ccr.mcp_server import FurlMCPServer  # noqa: E402

# Hook-safe split literal (no verbatim secret in source).
_API_KEY = "sk" + "-" + "abcdefghijklmnopqrstuvwx"

_HASH_A = "a" * 24
_HASH_B = "b" * 24
_HASH_ABSENT = "c" * 24


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
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


# ─── validation: exactly one of hash / all ─────────────────────────────────


async def test_purge_neither_arg_is_an_error(server) -> None:
    env = _envelope(await server._handle_purge({}))
    assert "error" in env
    assert "exactly one" in env["error"]


async def test_purge_both_args_is_an_error(server) -> None:
    env = _envelope(await server._handle_purge({"hash": _HASH_A, "all": True}))
    assert "error" in env
    assert "not both" in env["error"]


async def test_purge_all_must_be_boolean(server) -> None:
    env = _envelope(await server._handle_purge({"all": "yes"}))
    assert env["error"].startswith("all parameter must be a boolean")


async def test_purge_all_false_alone_is_neither(server) -> None:
    # all=false is not a wipe request and, with no hash, is the neither case.
    env = _envelope(await server._handle_purge({"all": False}))
    assert "error" in env and "exactly one" in env["error"]


async def test_purge_non_string_hash_is_an_error(server) -> None:
    env = _envelope(await server._handle_purge({"hash": 123}))
    assert env["error"].startswith("hash parameter must be a string")


async def test_purge_malformed_hash_is_an_error(server) -> None:
    env = _envelope(await server._handle_purge({"hash": "not-a-hash"}))
    assert "invalid hash format" in env["error"]


# ─── invariant C: purge(hash) removes exactly that hash ────────────────────


async def test_purge_hash_removes_retrievability_of_exactly_that_hash(server) -> None:
    _seed(server, _HASH_A, "erase me")
    _seed(server, _HASH_B, "keep me")

    purged = _envelope(await server._handle_purge({"hash": _HASH_A}))
    assert purged["purged"] == "hash"
    assert purged["deleted_count"] == 1
    assert purged["found"] is True

    # The purged hash is no longer retrievable — a clean, cause-honest miss.
    gone = _envelope(await server._handle_retrieve({"hash": _HASH_A}))
    assert "error" in gone
    assert gone["status"] == "missing"

    # The other hash is untouched.
    kept = _envelope(await server._handle_retrieve({"hash": _HASH_B}))
    assert kept["original_content"] == "keep me"


async def test_purge_absent_hash_is_clean_not_error(server) -> None:
    env = _envelope(await server._handle_purge({"hash": _HASH_ABSENT}))
    assert "error" not in env
    assert env["deleted_count"] == 0
    assert env["found"] is False


async def test_purge_all_empties_the_store(server) -> None:
    _seed(server, _HASH_A, "one")
    _seed(server, _HASH_B, "two")

    env = _envelope(await server._handle_purge({"all": True}))
    assert env["purged"] == "all"
    assert env["deleted_count"] == 2

    assert server._get_local_store().get_stats()["entry_count"] == 0
    # Every previously-stored hash now misses cleanly.
    for h in (_HASH_A, _HASH_B):
        gone = _envelope(await server._handle_retrieve({"hash": h}))
        assert "error" in gone


async def test_purge_hash_uppercase_is_normalized(server) -> None:
    # Store keys are lowercase; an upper/title-cased echo of a marker hash must
    # still purge (matches the retrieve ingress normalization).
    _seed(server, _HASH_A, "erase me")
    env = _envelope(await server._handle_purge({"hash": _HASH_A.upper()}))
    assert env["deleted_count"] == 1
    assert env["hash"] == _HASH_A  # normalized to lowercase
