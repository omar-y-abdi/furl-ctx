"""Unit tests for the furl_search (substring) and furl_list MCP tools.

Drives the real ``_handle_search`` / ``_handle_list`` handlers against a real
in-process CCR store. Covers validation, case-insensitive substring matching,
preview-around-match with credential redaction, newest-first ordering,
limit/offset paging, and invariant D (bounded output: limit is a positive int
capped at 100, echoed back).
"""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("mcp")

import mcp.types as mt  # noqa: E402

from furl_ctx.cache.compression_store import reset_compression_store  # noqa: E402
from furl_ctx.ccr.mcp_server import FurlMCPServer, _humanize_ttl_remaining  # noqa: E402

# Hook-safe split literal (no verbatim secret in source).
_API_KEY = "sk" + "-" + "abcdefghijklmnopqrstuvwx"


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


def _seed(server, hash_key, content, *, created_at=None, tool_name=None) -> None:
    store = server._get_local_store()
    store.store(original=content, compressed="c", explicit_hash=hash_key, tool_name=tool_name)
    if created_at is not None:
        # Deterministic ordering without real sleeps: pin created_at directly on
        # the stored entry (memory backend hands back the live object).
        store._backend.get(hash_key).created_at = created_at


# ─── furl_search: validation ───────────────────────────────────────────────


async def test_search_missing_query_errors(server) -> None:
    env = _envelope(await server._handle_search({}))
    assert env["error"] == "query parameter is required"


async def test_search_empty_query_errors(server) -> None:
    env = _envelope(await server._handle_search({"query": "   "}))
    assert "non-empty" in env["error"]


async def test_search_non_string_query_errors(server) -> None:
    env = _envelope(await server._handle_search({"query": 5}))
    assert env["error"].startswith("query parameter must be a string")


async def test_search_limit_zero_errors(server) -> None:
    env = _envelope(await server._handle_search({"query": "x", "limit": 0}))
    assert env["error"] == "limit must be >= 1, got 0"


async def test_search_limit_non_int_errors(server) -> None:
    env = _envelope(await server._handle_search({"query": "x", "limit": "5"}))
    assert env["error"].startswith("limit must be an integer")


async def test_search_limit_bool_is_rejected(server) -> None:
    # bool is a subclass of int; True must not sneak through as limit=1.
    env = _envelope(await server._handle_search({"query": "x", "limit": True}))
    assert env["error"].startswith("limit must be an integer")


# ─── furl_search: matching + preview ───────────────────────────────────────


async def test_search_is_case_insensitive_substring(server) -> None:
    _seed(server, "a" * 24, "Alpha NEEDLEword body")
    _seed(server, "b" * 24, "beta needleWORD x")
    _seed(server, "c" * 24, "gamma unrelated")

    env = _envelope(await server._handle_search({"query": "needleword"}))
    assert env["count"] == 2
    found = {m["hash"] for m in env["matches"]}
    assert found == {"a" * 24, "b" * 24}


async def test_search_hit_carries_hash_preview_created_at_size(server) -> None:
    content = "prefix needle suffix"
    _seed(server, "a" * 24, content)
    env = _envelope(await server._handle_search({"query": "needle"}))
    hit = env["matches"][0]
    assert hit["hash"] == "a" * 24
    assert "needle" in hit["preview"]
    assert isinstance(hit["created_at"], (int, float))
    assert hit["size"] == len(content)


async def test_search_preview_redacts_secret(server) -> None:
    _seed(server, "a" * 24, f"config needle {_API_KEY} trailing text")
    env = _envelope(await server._handle_search({"query": "needle"}))
    preview = env["matches"][0]["preview"]
    assert _API_KEY not in preview, f"secret leaked into preview: {preview!r}"
    assert "[REDACTED]" in preview


async def test_search_no_match_reports_empty(server) -> None:
    _seed(server, "a" * 24, "some stored content")
    env = _envelope(await server._handle_search({"query": "zzz_nomatch"}))
    assert env["count"] == 0
    assert env["matches"] == []
    assert "note" in env


async def test_search_limit_is_capped_at_100(server) -> None:
    _seed(server, "a" * 24, "the letter e appears here")
    env = _envelope(await server._handle_search({"query": "e", "limit": 500}))
    assert env["limit"] == 100  # invariant D: bounded output


async def test_search_returned_hash_round_trips(server) -> None:
    _seed(server, "a" * 24, "findme needle payload body")
    env = _envelope(await server._handle_search({"query": "needle"}))
    h = env["matches"][0]["hash"]
    full = _envelope(await server._handle_retrieve({"hash": h}))
    assert full["original_content"] == "findme needle payload body"


async def test_search_respects_limit_count(server) -> None:
    base = time.time()
    for i in range(5):
        _seed(server, f"{i:024d}", f"row {i} needle", created_at=base + i)
    env = _envelope(await server._handle_search({"query": "needle", "limit": 3}))
    assert env["count"] == 3


# ─── furl_list ─────────────────────────────────────────────────────────────


async def test_list_empty_store(server) -> None:
    env = _envelope(await server._handle_list({}))
    assert env["total"] == 0
    assert env["entries"] == []
    assert "note" in env


async def test_list_is_newest_first(server) -> None:
    base = time.time()
    _seed(server, "a" * 24, "oldest", created_at=base + 1)
    _seed(server, "b" * 24, "middle", created_at=base + 2)
    _seed(server, "c" * 24, "newest", created_at=base + 3)
    env = _envelope(await server._handle_list({}))
    assert [e["hash"] for e in env["entries"]] == ["c" * 24, "b" * 24, "a" * 24]
    assert env["total"] == 3


async def test_list_paging_with_limit_and_offset(server) -> None:
    base = time.time()
    _seed(server, "a" * 24, "oldest", created_at=base + 1)
    _seed(server, "b" * 24, "middle", created_at=base + 2)
    _seed(server, "c" * 24, "newest", created_at=base + 3)
    env = _envelope(await server._handle_list({"limit": 1, "offset": 1}))
    assert env["count"] == 1
    assert env["entries"][0]["hash"] == "b" * 24  # second newest
    assert env["total"] == 3
    assert env["offset"] == 1


async def test_list_entry_carries_size_kind_preview(server) -> None:
    _seed(server, "a" * 24, "file body here", tool_name="furl_read")
    _seed(server, "b" * 24, "compressed body")  # no tool_name
    env = _envelope(await server._handle_list({}))
    by_hash = {e["hash"]: e for e in env["entries"]}
    assert by_hash["a" * 24]["content_kind"] == "furl_read"
    assert by_hash["a" * 24]["size"] == len("file body here")
    assert by_hash["b" * 24]["content_kind"] is None


async def test_list_preview_redacts_secret(server) -> None:
    _seed(server, "a" * 24, f"leading {_API_KEY} tail")
    env = _envelope(await server._handle_list({}))
    preview = env["entries"][0]["preview"]
    assert _API_KEY not in preview
    assert "[REDACTED]" in preview


async def test_list_limit_capped_at_100(server) -> None:
    _seed(server, "a" * 24, "x")
    env = _envelope(await server._handle_list({"limit": 9999}))
    assert env["limit"] == 100


async def test_list_bad_offset_errors(server) -> None:
    env = _envelope(await server._handle_list({"offset": -1}))
    assert env["error"] == "offset must be >= 0, got -1"


async def test_list_bad_limit_errors(server) -> None:
    env = _envelope(await server._handle_list({"limit": 0}))
    assert env["error"] == "limit must be >= 1, got 0"


# ─── furl_list: expires_in (TTL visibility) ────────────────────────────────


async def test_list_entry_carries_humanized_expires_in(server) -> None:
    # Pin created_at (aged 30 min) and ttl (24h) directly on the live entry so
    # the remaining ~23.5h floors to a stable "23h" independent of test timing.
    now = time.time()
    _seed(server, "a" * 24, "body", created_at=now - 1800)
    server._get_local_store()._backend.get("a" * 24).ttl = 86400
    env = _envelope(await server._handle_list({}))
    entry = env["entries"][0]
    assert entry["expires_in"] == "23h"


async def test_list_expires_in_reflects_short_ttl(server) -> None:
    # A short custom TTL surfaces in minutes, not hours.
    now = time.time()
    _seed(server, "b" * 24, "body", created_at=now)
    server._get_local_store()._backend.get("b" * 24).ttl = 1530  # 25.5 min
    env = _envelope(await server._handle_list({}))
    assert env["entries"][0]["expires_in"] == "25m"


# ─── furl_list: age + ttl beside expires_in (round-6 misread fix) ──────────


async def test_list_entry_carries_humanized_age_and_ttl(server) -> None:
    # A 30-min-old entry on the plugin's 24h TTL: age and ttl land beside
    # expires_in so all three read together at a glance.
    now = time.time()
    _seed(server, "a" * 24, "body", created_at=now - 1800)
    server._get_local_store()._backend.get("a" * 24).ttl = 86400
    env = _envelope(await server._handle_list({}))
    entry = env["entries"][0]
    assert entry["age"] == "30m"
    assert entry["ttl"] == "24h"
    assert entry["expires_in"] == "23h"


async def test_list_age_and_ttl_disambiguate_equal_expires_in(server) -> None:
    """The round-6 misread: a nearly-fresh short-TTL entry and an old long-TTL
    entry can show the SAME expires_in — pre-fix they were indistinguishable,
    so a healthy 24h store read as if everything died in an hour. age + ttl
    split the two cases at a glance."""
    now = time.time()
    # ~1 min old, 90-min TTL → expires_in "1h".
    _seed(server, "f" * 24, "fresh short-ttl entry", created_at=now - 60)
    server._get_local_store()._backend.get("f" * 24).ttl = 5400
    # 22.5 h old, 24 h TTL → the SAME expires_in "1h".
    _seed(server, "0" * 24, "old long-ttl entry", created_at=now - 81000)
    server._get_local_store()._backend.get("0" * 24).ttl = 86400

    env = _envelope(await server._handle_list({}))
    by_hash = {e["hash"]: e for e in env["entries"]}
    fresh, old = by_hash["f" * 24], by_hash["0" * 24]

    # The ambiguity: identical time-left readings...
    assert fresh["expires_in"] == old["expires_in"] == "1h"
    # ...disambiguated by age and ttl.
    assert fresh["age"] == "1m"
    assert fresh["ttl"] == "1h"
    assert old["age"] == "22h"
    assert old["ttl"] == "24h"


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (86400, "24h"),  # the plugin's 24h default, fresh
        (82800, "23h"),
        (84600, "23h"),  # 23.5h floors to 23h ("at least")
        (3600, "1h"),
        (3599, "59m"),
        (90, "1m"),
        (60, "1m"),  # exact minute boundary
        (59, "59s"),
        (30, "30s"),
        (0, "0s"),
        (-5, "0s"),  # defensive clamp — never negative
    ],
)
def test_humanize_ttl_remaining(seconds: float, expected: str) -> None:
    assert _humanize_ttl_remaining(seconds) == expected
