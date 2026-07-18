"""Invariant B: every advertised furl_retrieve schema param is honored by the
handler, and no honored param stays hidden.

The row-select filters (select_field/select_equals/select_min/select_max/limit)
were parsed by the handler but absent from the tool inputSchema, so no agent
could discover them without reading source. These tests assert the schema now
advertises exactly the parameter set the handler honors, and that each newly
advertised select param actually drives the row-select.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

import mcp.types as mt  # noqa: E402

from furl_ctx.cache.compression_store import reset_compression_store  # noqa: E402
from furl_ctx.ccr.mcp_server import CCR_TOOL_NAME, FurlMCPServer  # noqa: E402

# The exact parameter set _handle_retrieve honors: hash + query (read directly)
# plus every key RetrieveFilters.parse reads. The schema must advertise exactly
# this set — no hidden params, no advertised-but-ignored params.
_HONORED_RETRIEVE_PARAMS = {
    "hash",
    "query",
    "pattern",
    "context_lines",
    "line_range",
    "fields",
    "select_field",
    "select_equals",
    "select_min",
    "select_max",
    "limit",
}


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


def _envelope(result: list[mt.TextContent]) -> dict:
    assert len(result) == 1
    return json.loads(result[0].text)


def _seed_rows(server: FurlMCPServer, hash_key: str, rows: list[dict]) -> str:
    server._get_local_store().store(
        original=json.dumps(rows), compressed="c", explicit_hash=hash_key
    )
    return hash_key


async def test_select_params_are_advertised(server) -> None:
    tools = await _list_tools(server)
    props = set(tools[CCR_TOOL_NAME].inputSchema["properties"])
    assert {"select_field", "select_equals", "select_min", "select_max", "limit"} <= props


async def test_schema_advertises_exactly_the_honored_params(server) -> None:
    # Invariant B, both directions: no hidden params, no advertised-but-ignored.
    tools = await _list_tools(server)
    props = set(tools[CCR_TOOL_NAME].inputSchema["properties"])
    assert props == _HONORED_RETRIEVE_PARAMS


async def test_select_equals_advertised_as_json_scalar(server) -> None:
    # The handler rejects a container select_equals; the schema must reflect
    # exactly the scalar types accepted.
    tools = await _list_tools(server)
    schema = tools[CCR_TOOL_NAME].inputSchema["properties"]["select_equals"]
    assert schema["type"] == ["string", "number", "boolean", "null"]


async def test_advertised_select_equals_is_honored(server) -> None:
    rows = [{"id": i, "kind": "err" if i % 2 else "ok"} for i in range(6)]
    h = _seed_rows(server, "a" * 24, rows)
    env = _envelope(
        await server._handle_retrieve({"hash": h, "select_field": "kind", "select_equals": "err"})
    )
    assert env["filter_kind"] == "rows"
    assert env["matched_count"] == 3


async def test_advertised_select_range_is_honored(server) -> None:
    rows = [{"id": i, "score": i * 10} for i in range(6)]
    h = _seed_rows(server, "b" * 24, rows)
    env = _envelope(
        await server._handle_retrieve(
            {"hash": h, "select_field": "score", "select_min": 20, "select_max": 40}
        )
    )
    assert env["filter_kind"] == "rows"
    assert env["matched_count"] == 3  # 20, 30, 40


async def test_advertised_limit_is_honored(server) -> None:
    rows = [{"id": i, "kind": "err"} for i in range(10)]
    h = _seed_rows(server, "c" * 24, rows)
    env = _envelope(
        await server._handle_retrieve(
            {"hash": h, "select_field": "kind", "select_equals": "err", "limit": 3}
        )
    )
    # 10 match but limit caps the projection; matched_count reports the true
    # total while the rendered content is truncated with a marker row.
    assert env["matched_count"] == 10
    assert "_truncated" in env["filtered_content"]
