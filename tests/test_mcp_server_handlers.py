"""Integration tests for the MCP tool-handler plane (headroom/ccr/mcp_server.py).

The user-facing handler surface — ``call_tool`` routing plus the four
``_handle_*`` entries — was the biggest real coverage hole (28%). These tests
drive each handler through its public async entry against a REAL in-process CCR
store (no mocked store, no mocked compressor), asserting the structured JSON
envelopes that an MCP host actually receives.

Coverage map (mcp_server.py):
    call_tool routing / unknown-tool ......... :500, :520
    _handle_compress missing-param ........... :543, :550
    _handle_retrieve missing-param ........... :560, :567
    _handle_stats combined savings ........... :587, :621
    _handle_read missing/not-found/not-file .. :627, :638, :647, :654

Requires the optional ``mcp`` extra (``pip install 'mcp>=1.0.0'``); the file is
skipped wholesale where the SDK is absent, so the coverage gain only reproduces
where ``mcp`` is installed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import mcp.types as mt  # noqa: E402

from headroom.cache.compression_store import reset_compression_store  # noqa: E402
from headroom.ccr import mcp_server  # noqa: E402
from headroom.ccr.mcp_server import (  # noqa: E402
    CCR_TOOL_NAME,
    COMPRESS_TOOL_NAME,
    READ_TOOL_NAME,
    STATS_TOOL_NAME,
    HeadroomMCPServer,
)


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    # The CCR store is a process-singleton; reset around every test so a
    # round-trip in one test cannot leak entries into another's stats/retrieve.
    # Also redirect the shared-stats file to a per-test tmp path so
    # record_compression() never writes to ~/.headroom (keep file mods scoped
    # to the test sandbox). Stats tests override this with their own path.
    monkeypatch.setattr(mcp_server, "SHARED_STATS_FILE", tmp_path / "shared_stats.jsonl")
    reset_compression_store()
    yield
    reset_compression_store()


@pytest.fixture
def server() -> HeadroomMCPServer:
    return HeadroomMCPServer()


def _envelope(result: list[mt.TextContent]) -> dict:
    """Decode the single-TextContent JSON envelope an MCP host receives."""
    assert len(result) == 1, f"expected one TextContent, got {result!r}"
    item = result[0]
    assert isinstance(item, mt.TextContent)
    assert item.type == "text"
    return json.loads(item.text)


def _get_call_tool(server: HeadroomMCPServer):
    """Recover the registered user ``call_tool`` closure (mcp_server.py:500).

    The SDK wraps our handler in its own ``handler`` closure under
    ``request_handlers[CallToolRequest]``; the user closure is captured as a
    cell named ``call_tool``. Reaching it lets us exercise the ROUTING/unknown
    -tool branches (:508-522) that bound ``_handle_*`` calls cannot reach.
    """
    sdk_handler = server.server.request_handlers[mt.CallToolRequest]
    for cell in sdk_handler.__closure__ or []:
        candidate = cell.cell_contents
        if callable(candidate) and getattr(candidate, "__name__", "") == "call_tool":
            return candidate
    raise AssertionError("user call_tool closure not found in SDK handler")


# ─── _handle_compress / _handle_retrieve missing-param envelopes ────────────


async def test_compress_missing_content_returns_error_envelope(server) -> None:
    # :550 — no `content` key → structured error, not a crash.
    env = _envelope(await server._handle_compress({}))
    assert env == {"error": "content parameter is required"}


async def test_compress_empty_content_is_falsy_and_errors(server) -> None:
    # Boundary: "" is falsy, so the guard fires exactly as for an absent key.
    env = _envelope(await server._handle_compress({"content": ""}))
    assert env == {"error": "content parameter is required"}


async def test_retrieve_missing_hash_returns_error_envelope(server) -> None:
    # :567 — no `hash` key → structured error.
    env = _envelope(await server._handle_retrieve({}))
    assert env == {"error": "hash parameter is required"}


@pytest.mark.parametrize(
    "bad_hash",
    [
        "abc",  # too short for any width
        "a" * 13,  # off-by-one over width 12
        "a" * 23,  # off-by-one under width 24
        "g" * 12,  # right width, non-hex char
        "<<ccr:" + "a" * 6,  # marker text, not a bare key
    ],
)
async def test_retrieve_malformed_hash_rejected_before_store(server, bad_hash) -> None:
    # The width+charset spoofing guard (is_valid_ccr_hash) rejects a present but
    # malformed key with a loud 400, matching the tool-call parse path — the key
    # never reaches the store. Distinct from the missing-hash and the
    # valid-but-unknown ("0"*24 → loud miss) envelopes.
    env = _envelope(await server._handle_retrieve({"hash": bad_hash}))
    assert env == {
        "error": "invalid hash format (expected 12 or 24 lowercase-hex chars)"
    }


# ─── _handle_read missing / not-found / not-a-file envelopes ────────────────


async def test_read_missing_file_path_returns_error_envelope(server) -> None:
    # :638 — `_handle_read` validates file_path itself (independent of the
    # HEADROOM_MCP_READ routing flag at :514), so a bound call reaches it.
    env = _envelope(await server._handle_read({}))
    assert env == {"error": "file_path parameter is required"}


async def test_read_nonexistent_path_reports_not_found(server) -> None:
    # :647 — a path that does not resolve to anything on disk.
    env = _envelope(await server._handle_read({"file_path": "/no/such/path/zzz.txt"}))
    assert env["error"].startswith("File not found:")
    assert "zzz.txt" in env["error"]


async def test_read_directory_reports_not_a_file(server, tmp_path: Path) -> None:
    # :654 — a real path that exists but is a directory, not a file.
    env = _envelope(await server._handle_read({"file_path": str(tmp_path)}))
    assert env["error"].startswith("Not a file:")


async def test_read_real_file_returns_numbered_content(server, tmp_path: Path) -> None:
    # Real-I/O happy path: a file on disk → line-numbered content (Read-tool
    # shape), with each line prefixed by a right-justified 1-based line number.
    f = tmp_path / "sample.txt"
    f.write_text("alpha\nbeta\ngamma")
    result = await server._handle_read({"file_path": str(f)})
    assert len(result) == 1
    text = result[0].text
    assert text == "     1\talpha\n     2\tbeta\n     3\tgamma"


# ─── clean compress → retrieve round-trip (real store) ──────────────────────


async def test_compress_then_retrieve_round_trip(server) -> None:
    payload = "def add(a, b):\n    return a + b\n" * 40

    comp = _envelope(await server._handle_compress({"content": payload}))
    # Compress envelope contract: a retrieval hash + token accounting.
    assert set(comp).issuperset(
        {"compressed", "hash", "original_tokens", "compressed_tokens", "tokens_saved"}
    )
    hash_key = comp["hash"]
    assert isinstance(hash_key, str) and hash_key

    ret = _envelope(await server._handle_retrieve({"hash": hash_key}))
    # Retrieving by the issued hash returns the byte-for-byte original.
    assert ret["hash"] == hash_key
    assert ret["source"] == "local"
    assert ret["original_content"] == payload


async def test_retrieve_unknown_hash_reports_loud_miss(server) -> None:
    # Boundary: a hash that was never stored must error loudly (cause-honest
    # miss), never a silent empty success.
    env = _envelope(await server._handle_retrieve({"hash": "0" * 24}))
    assert "error" in env
    assert env["hash"] == "0" * 24


# ─── _handle_stats combined-savings aggregation + all_input boundary ────────


def _write_shared_events(path: Path, events: list[dict]) -> None:
    foreign_pid = 10_000_000  # not this process → counted as a sub-agent
    now = time.time()
    lines = []
    for evt in events:
        record = {"timestamp": now, "pid": foreign_pid, **evt}
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n")


async def test_stats_aggregates_subagent_savings(server, tmp_path, monkeypatch) -> None:
    # Drive the cross-process aggregation branch (:603-623): a sub-agent
    # (foreign pid) compressed 1000 → 100 tokens. With this session's own
    # stats at zero, all_input = 1000, all_saved = 900 → 90.0%.
    stats_file = tmp_path / "session_stats.jsonl"
    _write_shared_events(
        stats_file,
        [{"type": "compress", "input_tokens": 1000, "output_tokens": 100}],
    )
    monkeypatch.setattr(mcp_server, "SHARED_STATS_FILE", stats_file)

    env = _envelope(await server._handle_stats())

    assert "combined" in env, "sub-agent events must surface a combined block"
    combined = env["combined"]
    assert combined["total_tokens_saved"] == 900
    assert combined["savings_percent"] == 90.0
    assert env["sub_agents"]["compressions"] == 1
    assert env["sub_agents"]["tokens_saved"] == 900


async def test_stats_all_input_zero_boundary_returns_zero(
    server, tmp_path, monkeypatch
) -> None:
    # :621 boundary — a sub-agent event with input_tokens == 0 enters the
    # aggregation block (other_events non-empty) but yields all_input == 0,
    # so savings_percent must take the `else 0` branch (no ZeroDivisionError).
    stats_file = tmp_path / "session_stats.jsonl"
    _write_shared_events(
        stats_file,
        [{"type": "compress", "input_tokens": 0, "output_tokens": 0}],
    )
    monkeypatch.setattr(mcp_server, "SHARED_STATS_FILE", stats_file)

    env = _envelope(await server._handle_stats())

    assert "combined" in env
    assert env["combined"]["savings_percent"] == 0
    assert env["combined"]["total_tokens_saved"] == 0


async def test_stats_no_subagent_events_has_no_combined_block(
    server, tmp_path, monkeypatch
) -> None:
    # Boundary complement: with NO foreign events, the aggregation block is
    # skipped entirely — no `combined`/`sub_agents` keys, only base stats.
    stats_file = tmp_path / "session_stats.jsonl"
    stats_file.write_text("")  # empty → no events
    monkeypatch.setattr(mcp_server, "SHARED_STATS_FILE", stats_file)

    env = _envelope(await server._handle_stats())

    assert "combined" not in env
    assert "sub_agents" not in env
    assert env["compressions"] == 0


# ─── call_tool routing + unknown-tool (single closure-driven test) ──────────


async def test_call_tool_routes_and_rejects_unknown(server) -> None:
    call_tool = _get_call_tool(server)

    # Unknown tool → routing falls through to the unknown-tool envelope (:520).
    unknown = _envelope(await call_tool("nonexistent_tool", {}))
    assert unknown == {"error": "Unknown tool: nonexistent_tool"}

    # Known tool → routed to its handler (:512), returns the stats envelope.
    stats = _envelope(await call_tool(STATS_TOOL_NAME, {}))
    assert "compressions" in stats and "savings_percent" in stats

    # End-to-end through the dispatcher: compress then retrieve round-trips.
    comp = _envelope(await call_tool(COMPRESS_TOOL_NAME, {"content": "round trip me"}))
    again = _envelope(await call_tool(CCR_TOOL_NAME, {"hash": comp["hash"]}))
    assert again["original_content"] == "round trip me"


def test_read_tool_name_is_distinct_constant() -> None:
    # Guard the four tool-name constants are distinct (a copy/paste collision
    # would silently misroute call_tool).
    names = {COMPRESS_TOOL_NAME, CCR_TOOL_NAME, STATS_TOOL_NAME, READ_TOOL_NAME}
    assert len(names) == 4
