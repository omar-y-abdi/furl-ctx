"""End-to-end MCP tests over real JSON-RPC stdio.

Spawns the Furl MCP server as a SUBPROCESS and drives it with the MCP SDK client
(a genuine initialize → tools/list → tools/call handshake over stdio), asserting
the full max-suite works on the wire — not just via in-process handler calls.

Isolation: a per-test temp workspace + temp SQLite path via env, so nothing
touches ``~/.furl``. Deterministic and offline. Generous-but-bounded timeouts;
the ``stdio_client`` async context manager always terminates/reaps the child.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from furl_ctx._version import get_version  # noqa: E402

_TIMEOUT = 30  # seconds — generous but bounded, so a wedged child can't hang CI.
_MODULE_ARGS = ["-m", "furl_ctx.ccr.mcp_server"]
_CLI_LAUNCHER_ARGS = ["-m", "furl_ctx.cli", "mcp"]  # the `furl mcp` path


@contextlib.asynccontextmanager
async def _client(tmp_path, args=None):
    """Yield ``(session, init_result)`` for a freshly spawned server subprocess."""
    env = {
        **os.environ,
        "FURL_WORKSPACE_DIR": str(tmp_path),
        "FURL_CCR_SQLITE_PATH": str(tmp_path / "ccr.sqlite3"),
        "FURL_MCP_LEGEND": "off",
        # Opt out of per-project isolation (audit #4): this suite pins an
        # explicit FURL_CCR_SQLITE_PATH and asserts on that single global file,
        # so it must exercise the un-namespaced global store, not the per-project
        # one main() would otherwise derive from cwd.
        "FURL_CCR_PROJECT_DIR": "",
    }
    env.pop("FURL_CCR_BACKEND", None)  # exercise the real durable SQLite default
    params = StdioServerParameters(command=sys.executable, args=args or _MODULE_ARGS, env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await asyncio.wait_for(session.initialize(), _TIMEOUT)
            yield session, init


async def _call(session, name, arguments):
    return await asyncio.wait_for(session.call_tool(name, arguments), _TIMEOUT)


def _text(result) -> dict:
    assert not result.isError, f"tool call returned isError=True: {result.content!r}"
    assert result.content, "tool call returned no content"
    return json.loads(result.content[0].text)


async def test_stdio_initialize_reports_furl_version(tmp_path) -> None:
    async with _client(tmp_path) as (_session, init):
        assert init.serverInfo.name == "furl"
        # Equality against the real source of truth: the server subprocess runs
        # in this same environment, so it must report exactly the furl-ctx
        # distribution version get_version() resolves here — never the MCP
        # SDK's own version (the regression this guards: a Server constructed
        # without version= falls back to the SDK's package version).
        assert init.serverInfo.version == get_version()


async def test_stdio_tools_list_advertises_full_suite_and_schemas(tmp_path) -> None:
    async with _client(tmp_path) as (session, _init):
        listed = await asyncio.wait_for(session.list_tools(), _TIMEOUT)
        by_name = {tool.name: tool for tool in listed.tools}

        assert {
            "furl_compress",
            "furl_retrieve",
            "furl_stats",
            "furl_purge",
            "furl_search",
            "furl_list",
        } <= set(by_name)
        assert "furl_read" not in by_name  # flag-gated, default off

        retrieve_props = by_name["furl_retrieve"].inputSchema["properties"]
        assert {
            "select_field",
            "select_equals",
            "select_min",
            "select_max",
            "limit",
        } <= set(retrieve_props)
        assert retrieve_props["select_equals"]["type"] == [
            "string",
            "number",
            "boolean",
            "null",
        ]

        search_schema = by_name["furl_search"].inputSchema
        assert search_schema["required"] == ["query"]
        assert search_schema["properties"]["limit"]["maximum"] == 100

        assert {"limit", "offset"} <= set(by_name["furl_list"].inputSchema["properties"])
        assert {"hash", "all"} <= set(by_name["furl_purge"].inputSchema["properties"])


async def test_stdio_full_lifecycle_compress_retrieve_search_list_purge(tmp_path) -> None:
    blob = json.dumps([{"id": i, "kind": "err" if i % 2 else "ok"} for i in range(6)])

    async with _client(tmp_path) as (session, _init):
        # compress a blob → get a hash
        comp = _text(await _call(session, "furl_compress", {"content": blob}))
        hash_key = comp["hash"]

        # retrieve it byte-exact
        full = _text(await _call(session, "furl_retrieve", {"hash": hash_key}))
        assert full["original_content"] == blob

        # retrieve with a select_* filter (the newly advertised row-select)
        selected = _text(
            await _call(
                session,
                "furl_retrieve",
                {"hash": hash_key, "select_field": "kind", "select_equals": "err"},
            )
        )
        assert selected["filter_kind"] == "rows"
        assert selected["matched_count"] == 3

        # search finds it by content substring
        found = _text(await _call(session, "furl_search", {"query": "kind"}))
        assert any(match["hash"] == hash_key for match in found["matches"])

        # list shows it
        listed = _text(await _call(session, "furl_list", {}))
        assert any(entry["hash"] == hash_key for entry in listed["entries"])

        # purge(hash) removes exactly that hash
        purged = _text(await _call(session, "furl_purge", {"hash": hash_key}))
        assert purged["deleted_count"] == 1

        # a retrieve of the purged hash now reports a clean, documented absence
        miss = _text(await _call(session, "furl_retrieve", {"hash": hash_key}))
        assert "error" in miss
        assert miss["status"] == "missing"

        # purge(all) succeeds and leaves the store empty
        wiped = _text(await _call(session, "furl_purge", {"all": True}))
        assert wiped["purged"] == "all"
        empty = _text(await _call(session, "furl_list", {}))
        assert empty["total"] == 0


async def test_stdio_cli_launcher_serves_the_same_suite(tmp_path) -> None:
    # The `furl mcp` launcher (via the CLI module) must serve the identical
    # server over stdio — proving the console-script entry point works on the wire.
    async with _client(tmp_path, args=_CLI_LAUNCHER_ARGS) as (session, init):
        assert init.serverInfo.name == "furl"
        listed = await asyncio.wait_for(session.list_tools(), _TIMEOUT)
        names = {tool.name for tool in listed.tools}
        assert {"furl_compress", "furl_retrieve", "furl_purge", "furl_search", "furl_list"} <= names
