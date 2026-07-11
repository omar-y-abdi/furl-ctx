"""furl_stats reports a live, store-derived section — not just process counters.

Round-5 finding B. The process counters in ``SessionStats`` see only what THIS
MCP server process compressed; they are structurally blind to the PostToolUse
hook (a separate subprocess) and to sub-agents, which write to the SAME shared
sqlite store. An evaluator watched ``furl_stats`` sit at 0 while the store held
real entries from prior/other work — false reassurance.

The fix adds a live ``store`` section computed from the shared store every call.
These tests seed the store DIRECTLY (this server process compresses nothing) and
assert the section reflects the seeded entries while the process counters stay 0.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from furl_ctx.cache.compression_store import reset_compression_store  # noqa: E402
from furl_ctx.ccr.mcp_server import FurlMCPServer  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    # Sandbox the workspace (the sqlite default backend + shared-stats file live
    # under it), clean backend env, fresh singleton around each test.
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("FURL_CCR_BACKEND", raising=False)
    reset_compression_store()
    yield
    reset_compression_store()


@pytest.fixture
def server() -> FurlMCPServer:
    return FurlMCPServer()


def _seed(server: FurlMCPServer, count: int) -> tuple[int, int, int, int]:
    """Store ``count`` entries directly in the server's store (no compression by
    this process). Returns (orig_bytes, comp_bytes, orig_tokens, comp_tokens)."""
    store = server._get_local_store()
    orig_bytes = comp_bytes = orig_tokens = comp_tokens = 0
    for i in range(count):
        original = "X" * (100 + i) + f" needle{i}"
        compressed = f"<<ccr:{i:012x}>>"
        ot, ct = 50 + i, 5
        store.store(
            original=original,
            compressed=compressed,
            original_tokens=ot,
            compressed_tokens=ct,
            explicit_hash=f"{i:012x}",
        )
        orig_bytes += len(original)
        comp_bytes += len(compressed)
        orig_tokens += ot
        comp_tokens += ct
    return orig_bytes, comp_bytes, orig_tokens, comp_tokens


def test_store_section_reflects_entries_this_process_never_compressed(server) -> None:
    orig_bytes, comp_bytes, orig_tokens, comp_tokens = _seed(server, 3)

    stats = server._compute_stats()

    # This server process compressed nothing — the process counters must say so.
    assert stats["compressions"] == 0
    assert stats["total_tokens_saved"] == 0

    # ...yet the live store section reflects the seeded entries.
    store = stats["store"]
    assert store["live_entries"] == 3
    assert store["entries"] == 3  # kept alias; == live count
    assert store["total_original_bytes"] == orig_bytes
    assert store["total_compressed_bytes"] == comp_bytes
    assert store["total_original_tokens"] == orig_tokens
    assert store["total_compressed_tokens"] == comp_tokens
    assert store["estimated_tokens_saved"] == max(0, orig_tokens - comp_tokens)
    assert store["estimated_tokens_saved"] > 0
    # Age fields are present when the store is non-empty.
    assert "oldest_entry_age_seconds" in store
    assert "newest_entry_age_seconds" in store


def test_both_scopes_are_labeled_so_neither_lies(server) -> None:
    _seed(server, 1)
    stats = server._compute_stats()
    # Process counters carry a this-process-scope label.
    assert "process_scope" in stats
    assert "THIS" in stats["process_scope"]
    # The store block carries a shared/cross-process scope label.
    assert "scope" in stats["store"]
    assert "shared CCR store" in stats["store"]["scope"]


def test_empty_store_reports_zero_live_entries_without_age(server) -> None:
    stats = server._compute_stats()
    store = stats["store"]
    assert store["live_entries"] == 0
    assert store["total_original_tokens"] == 0
    assert store["estimated_tokens_saved"] == 0
    # No entries → no oldest/newest age (avoid inventing an age for nothing).
    assert "oldest_entry_age_seconds" not in store
    assert "newest_entry_age_seconds" not in store


async def test_store_section_ships_through_handle_stats_envelope(server) -> None:
    """The store section reaches the host through the real _handle_stats JSON."""
    import json

    _seed(server, 2)
    result = await server._handle_stats()
    env = json.loads(result[0].text)
    assert env["store"]["live_entries"] == 2
    assert env["compressions"] == 0
