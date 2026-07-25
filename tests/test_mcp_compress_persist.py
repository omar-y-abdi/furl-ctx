"""F9: a router no-op is not stored unless the caller opts in with persist=true.

Report finding 9: ``furl_compress`` stored the ORIGINAL even when the router
returned a no-op (below_min_tokens / no_savings) and saved zero tokens, so a
no-op consumed a retrieval cap slot for content identical to what the caller
already held. The fix skips the store on a no-op by default; ``persist=true``
restores the old always-store behavior when the caller genuinely wants a hash.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

import mcp.types as mt  # noqa: E402

from furl_ctx.cache.compression_store import reset_compression_store  # noqa: E402
from furl_ctx.ccr.mcp_server import COMPRESS_TOOL_NAME, FurlMCPServer  # noqa: E402

# A router no-op: too small to clear the compression floor (below_min_tokens).
_NOOP = "short"
# Big enough for a real crush (so persist has nothing to skip).
_BIG = json.dumps([{"id": i, "msg": f"line {i} " * 4} for i in range(60)])


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
    assert len(result) == 1
    return json.loads(result[0].text)


async def _compress(server: FurlMCPServer, args: dict) -> dict:
    return _envelope(await server._handle_compress(args))


async def _list_tools(server: FurlMCPServer) -> dict:
    """Invoke the registered list_tools handler and return {name: Tool}."""
    handler = None
    for req_type, h in server.server.request_handlers.items():
        if req_type.__name__ == "ListToolsRequest":
            handler = h
            break
    assert handler is not None, "no ListToolsRequest handler registered"
    result = await handler(mt.ListToolsRequest(method="tools/list"))
    return {tool.name: tool for tool in result.root.tools}


# ── default: no-op is NOT stored ────────────────────────────────────────────


async def test_noop_not_stored_by_default(server) -> None:
    env = await _compress(server, {"content": _NOOP})
    assert env["hash"] is None, "a no-op must not return a retrieval hash by default"
    assert env["tokens_saved"] == 0
    assert "NOT stored" in env["note"]
    assert "persist=true" in env["note"]
    # And nothing landed in the store.
    assert server._get_local_store().get_stats()["entry_count"] == 0


async def test_noop_response_keeps_default_envelope_keys(server) -> None:
    # The no-op response uses the same 8-key default envelope (hash just None),
    # so a caller parsing the shape sees no surprise fields.
    env = await _compress(server, {"content": _NOOP})
    assert set(env) == {
        "compressed",
        "hash",
        "original_tokens",
        "compressed_tokens",
        "tokens_saved",
        "savings_percent",
        "transforms",
        "note",
    }
    # The original is returned unchanged so the caller loses nothing.
    assert env["compressed"] == _NOOP


# ── opt-in: persist=true stores the no-op ───────────────────────────────────


async def test_noop_stored_when_persist_true(server) -> None:
    env = await _compress(server, {"content": _NOOP, "persist": True})
    assert env["hash"], "persist=true must store the no-op and return its hash"
    assert server._get_local_store().get_stats()["entry_count"] == 1
    # Retrievable byte-exact.
    ret = _envelope(await server._handle_retrieve({"hash": env["hash"]}))
    assert ret["original_content"] == _NOOP


# ── a real compression is unaffected ────────────────────────────────────────


async def test_real_compression_still_stored(server) -> None:
    env = await _compress(server, {"content": _BIG})
    assert env["hash"], "a real compression is stored regardless of persist"
    assert env["tokens_saved"] > 0
    assert server._get_local_store().get_stats()["entry_count"] >= 1


# ── validation + schema ─────────────────────────────────────────────────────


async def test_non_bool_persist_is_a_parameter_error(server) -> None:
    env = await _compress(server, {"content": _BIG, "persist": "yes"})
    assert env["error"] == "persist parameter must be a boolean, got str"


async def test_schema_advertises_persist(server) -> None:
    tools = await _list_tools(server)
    props = tools[COMPRESS_TOOL_NAME].inputSchema["properties"]
    assert "persist" in props
    assert props["persist"]["type"] == "boolean"


# ── filtered path still stores its runs (persist forced internally) ─────────


async def test_filtered_runs_stored_even_when_a_run_noops(server) -> None:
    # A protected/eligible mix where an eligible run is itself tiny (would no-op
    # standalone) must still store and return a run hash — the filtered contract.
    content = "KEEP this line\n" + "tiny\n" + ("payload " * 80)
    env = await _compress(server, {"content": content, "include_patterns": ["tiny"]})
    assert env["filtered"] is True
    assert env["hashes"], "every eligible run must be stored and retrievable"
