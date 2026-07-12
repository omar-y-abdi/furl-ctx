"""furl_stats surfaces the cross-process hook/pipe counters and labels its scopes.

The hook (a subprocess) tallies into the shared per-project sqlite store; furl_stats
must read those counters back so a user can tell "the hook ran but my context still
shows raw output" (#68951) from "the hook never fired". These tests pin that the
counters appear under the cross-process ``store`` block, that no-op reasons are
grouped, that the opt-in pipe counters appear only once the pipe has run, and that
the two scopes carry a clarifying label.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

import mcp.types as mt  # noqa: E402

from furl_ctx.cache.compression_store import (  # noqa: E402
    reset_compression_store,
    resolve_ccr_namespace_store,
)
from furl_ctx.ccr.mcp_server import FurlMCPServer  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_sqlite_namespace(tmp_path, monkeypatch):
    # A durable per-project store so the counters are cross-process (the whole
    # point) and the server + this test resolve the SAME namespace store object.
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    monkeypatch.setenv("FURL_CCR_PROJECT_DIR", str(tmp_path / "proj"))
    reset_compression_store()
    yield
    reset_compression_store()


def _envelope(result: list[mt.TextContent]) -> dict:
    assert len(result) == 1
    return json.loads(result[0].text)


async def test_hook_counters_surface_in_store_block() -> None:
    store = resolve_ccr_namespace_store()
    assert store is not None
    store.increment_counter("hook_invocations_seen", 5)
    store.increment_counter("hook_compressions_applied", 2)
    store.increment_counter("hook_noop:below-min-chars", 3)

    stats = _envelope(await FurlMCPServer()._handle_stats())

    activity = stats["store"]["hook_activity"]
    assert activity["hook_invocations_seen"] == 5
    assert activity["hook_compressions_applied"] == 2
    assert activity["hook_noop_reasons"] == {"below-min-chars": 3}
    # #68951 diagnostic pointer travels with the numbers.
    assert "68951" in activity["note"]


async def test_pipe_counters_hidden_until_pipe_runs() -> None:
    store = resolve_ccr_namespace_store()
    assert store is not None
    store.increment_counter("hook_invocations_seen", 1)

    stats = _envelope(await FurlMCPServer()._handle_stats())
    activity = stats["store"]["hook_activity"]
    assert "pipe_invocations_seen" not in activity  # opt-in path idle → not shown

    store.increment_counter("pipe_invocations_seen", 4)
    store.increment_counter("pipe_compressions_applied", 1)
    stats2 = _envelope(await FurlMCPServer()._handle_stats())
    activity2 = stats2["store"]["hook_activity"]
    assert activity2["pipe_invocations_seen"] == 4
    assert activity2["pipe_compressions_applied"] == 1


async def test_scopes_are_labeled() -> None:
    stats = _envelope(await FurlMCPServer()._handle_stats())
    # One up-front contrast plus the existing per-block labels.
    assert "scopes" in stats
    assert "store" in stats["scopes"] and "process" in stats["scopes"].lower()
    assert "process_scope" in stats
    assert "store" in stats and "scope" in stats["store"]
