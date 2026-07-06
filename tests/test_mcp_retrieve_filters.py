"""MCP integration tests for filtered per-hash retrieve (NR2-2 feature b).

``furl_retrieve`` with a hash gains optional filters: ``pattern`` (regex,
line-wise, with ``context_lines``), ``fields`` (JSON-array projection), and
``line_range`` ([start, end], 1-based inclusive). Covered: each filter, their
composition, the error cases (invalid regex, out-of-range, fields-on-non-array,
fields+line-filter), and that an UNFILTERED retrieve stays byte-identical.
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


_TEXT = "alpha first line\nbeta ERROR here\ngamma third line\ndelta ERROR again\nepsilon last"
_ARRAY = json.dumps(
    [
        {"id": 1, "name": "alice", "email": "a@x.com", "secret": "s1"},
        {"id": 2, "name": "bob", "email": "b@x.com", "secret": "s2"},
        {"id": 3, "name": "carol", "email": "c@x.com", "secret": "s3"},
    ]
)


def _seed(server: FurlMCPServer, hash_key: str, content: str) -> None:
    server._get_local_store().store(original=content, compressed="c", explicit_hash=hash_key)


# ─── unfiltered path unchanged ──────────────────────────────────────────────


async def test_unfiltered_retrieve_is_unchanged(server) -> None:
    _seed(server, "a" * 12, _TEXT)
    env = _envelope(await server._handle_retrieve({"hash": "a" * 12}))
    # No filter keys → the original verbatim envelope, byte-for-byte content.
    assert env["original_content"] == _TEXT
    assert "filtered" not in env


# ─── pattern ────────────────────────────────────────────────────────────────


async def test_pattern_returns_matching_lines(server) -> None:
    _seed(server, "a" * 12, _TEXT)
    env = _envelope(await server._handle_retrieve({"hash": "a" * 12, "pattern": "ERROR"}))
    assert env["filtered"] is True
    assert env["filter_kind"] == "lines"
    # Two ERROR lines, no context → exactly those two, line-numbered.
    assert env["filtered_content"] == "2:beta ERROR here\n4:delta ERROR again"
    assert env["matched_count"] == 2
    assert env["total_count"] == 5


async def test_pattern_with_context_lines(server) -> None:
    _seed(server, "a" * 12, _TEXT)
    env = _envelope(
        await server._handle_retrieve({"hash": "a" * 12, "pattern": "gamma", "context_lines": 1})
    )
    # gamma is line 3; ±1 context → lines 2,3,4.
    assert env["filtered_content"] == ("2:beta ERROR here\n3:gamma third line\n4:delta ERROR again")


async def test_invalid_regex_returns_error(server) -> None:
    _seed(server, "a" * 12, _TEXT)
    env = _envelope(await server._handle_retrieve({"hash": "a" * 12, "pattern": "([unclosed"}))
    assert env["error"].startswith("invalid regex in pattern")


# ─── line_range ─────────────────────────────────────────────────────────────


async def test_line_range_projects_window(server) -> None:
    _seed(server, "a" * 12, _TEXT)
    env = _envelope(await server._handle_retrieve({"hash": "a" * 12, "line_range": [2, 3]}))
    assert env["filtered_content"] == "2:beta ERROR here\n3:gamma third line"


async def test_line_range_open_end(server) -> None:
    _seed(server, "a" * 12, _TEXT)
    env = _envelope(await server._handle_retrieve({"hash": "a" * 12, "line_range": [4, None]}))
    assert env["filtered_content"] == "4:delta ERROR again\n5:epsilon last"


async def test_inverted_line_range_errors(server) -> None:
    _seed(server, "a" * 12, _TEXT)
    env = _envelope(await server._handle_retrieve({"hash": "a" * 12, "line_range": [5, 2]}))
    # Exact: the interpolated bounds must survive to the caller (a message that
    # dropped or swapped start/end would pass a bare substring check).
    assert env["error"] == "line_range end (2) must be >= start (5)"


async def test_line_range_below_one_errors(server) -> None:
    _seed(server, "a" * 12, _TEXT)
    env = _envelope(await server._handle_retrieve({"hash": "a" * 12, "line_range": [0, 3]}))
    assert env["error"] == "line_range start must be >= 1, got 0"


# ─── pattern + line_range compose ───────────────────────────────────────────


async def test_pattern_and_line_range_compose(server) -> None:
    _seed(server, "a" * 12, _TEXT)
    # Range narrows to lines 1-3 FIRST, then ERROR matches within → only line 2.
    env = _envelope(
        await server._handle_retrieve({"hash": "a" * 12, "pattern": "ERROR", "line_range": [1, 3]})
    )
    assert env["filtered_content"] == "2:beta ERROR here"
    assert env["matched_count"] == 1


async def test_context_does_not_leak_past_range(server) -> None:
    _seed(server, "a" * 12, _TEXT)
    # Range 1-3, match on line 2, context 5 — context is clamped to the window,
    # so line 4 (outside the range) never appears.
    env = _envelope(
        await server._handle_retrieve(
            {
                "hash": "a" * 12,
                "pattern": "ERROR",
                "line_range": [1, 3],
                "context_lines": 5,
            }
        )
    )
    assert "4:delta" not in env["filtered_content"]
    assert env["filtered_content"] == ("1:alpha first line\n2:beta ERROR here\n3:gamma third line")


# ─── fields ─────────────────────────────────────────────────────────────────


async def test_fields_projects_json_array(server) -> None:
    _seed(server, "a" * 12, _ARRAY)
    env = _envelope(await server._handle_retrieve({"hash": "a" * 12, "fields": ["id", "name"]}))
    assert env["filter_kind"] == "fields"
    projected = json.loads(env["filtered_content"])
    assert projected == [
        {"id": 1, "name": "alice"},
        {"id": 2, "name": "bob"},
        {"id": 3, "name": "carol"},
    ]
    # The un-projected "secret"/"email" keys are gone.
    assert "secret" not in env["filtered_content"]
    assert env["matched_count"] == 3
    assert env["total_count"] == 3


async def test_fields_on_non_array_errors(server) -> None:
    _seed(server, "a" * 12, _TEXT)  # plain text, not a JSON array
    env = _envelope(await server._handle_retrieve({"hash": "a" * 12, "fields": ["id"]}))
    assert "JSON-array" in env["error"]


async def test_fields_on_json_object_errors(server) -> None:
    _seed(server, "a" * 12, json.dumps({"id": 1, "name": "solo"}))
    env = _envelope(await server._handle_retrieve({"hash": "a" * 12, "fields": ["id"]}))
    assert "JSON-array" in env["error"]
    assert "object" in env["error"]


async def test_fields_combined_with_pattern_errors(server) -> None:
    _seed(server, "a" * 12, _ARRAY)
    env = _envelope(
        await server._handle_retrieve({"hash": "a" * 12, "fields": ["id"], "pattern": "alice"})
    )
    assert "cannot be combined" in env["error"]


async def test_empty_fields_list_errors(server) -> None:
    _seed(server, "a" * 12, _ARRAY)
    env = _envelope(await server._handle_retrieve({"hash": "a" * 12, "fields": []}))
    assert "must not be empty" in env["error"]


# ─── filters + query mutually exclusive ─────────────────────────────────────


async def test_filters_with_query_errors(server) -> None:
    _seed(server, "a" * 12, _TEXT)
    env = _envelope(
        await server._handle_retrieve({"hash": "a" * 12, "query": "alpha", "pattern": "ERROR"})
    )
    assert "cannot be combined with query" in env["error"]


# ─── bad-type params ────────────────────────────────────────────────────────


async def test_non_list_fields_errors(server) -> None:
    _seed(server, "a" * 12, _ARRAY)
    env = _envelope(await server._handle_retrieve({"hash": "a" * 12, "fields": "id"}))
    assert env["error"] == "fields must be a list of strings"


async def test_context_lines_over_cap_errors(server) -> None:
    _seed(server, "a" * 12, _TEXT)
    env = _envelope(
        await server._handle_retrieve({"hash": "a" * 12, "pattern": "ERROR", "context_lines": 999})
    )
    assert env["error"] == "context_lines must be <= 50, got 999"
