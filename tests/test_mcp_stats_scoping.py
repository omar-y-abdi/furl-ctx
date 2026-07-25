"""F11 + wave-1 additions: furl_stats makes its scopes unmistakable, exposes a
session-scoped delta for the lifetime hook counters, surfaces hook_size_reroute,
clarifies that pipe no-op reasons are dynamic-only, and reports F8 effective
retrieval headroom.

Report finding 11: furl_stats mixed cumulative-lifetime counters with
this-session scope, so a tester misread lifetime no-op reasons as current
results. The blocks now carry explicit scope labels and a ``this_session`` delta
(lifetime minus a snapshot taken at server start).
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
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    monkeypatch.setenv("FURL_CCR_PROJECT_DIR", str(tmp_path / "proj"))
    reset_compression_store()
    yield
    reset_compression_store()


def _envelope(result: list[mt.TextContent]) -> dict:
    assert len(result) == 1
    return json.loads(result[0].text)


# ── F11: scope labels + session delta ───────────────────────────────────────


async def test_hook_activity_is_labeled_lifetime_with_session_delta() -> None:
    store = resolve_ccr_namespace_store()
    store.increment_counter("hook_invocations_seen", 5)

    activity = _envelope(await FurlMCPServer()._handle_stats())["store"]["hook_activity"]
    assert "LIFETIME" in activity["scope"]
    assert activity["hook_invocations_seen"] == 5
    assert "this_session" in activity
    # R3 honesty: the this_session label is a per-server DELTA that may include
    # concurrent processes (the counters are cross-process), and the scope string
    # must say both plainly rather than imply a clean single-session count.
    session_scope = activity["this_session"]["scope"]
    assert "delta" in session_scope.lower(), "this_session must be labeled a delta"
    assert "concurrent processes" in session_scope, (
        "R3: the delta may include other processes and the label must disclose it"
    )


async def test_session_delta_counts_only_activity_since_server_start() -> None:
    store = resolve_ccr_namespace_store()
    store.increment_counter("hook_invocations_seen", 5)  # before this server reads counters

    server = FurlMCPServer()  # ONE server across both reads (per-server baseline)
    first = _envelope(await server._handle_stats())["store"]["hook_activity"]
    assert first["hook_invocations_seen"] == 5  # lifetime
    assert first["this_session"]["hook_invocations_seen"] == 0  # nothing since start

    store.increment_counter("hook_invocations_seen", 3)  # after the start snapshot
    second = _envelope(await server._handle_stats())["store"]["hook_activity"]
    assert second["hook_invocations_seen"] == 8  # lifetime rose
    assert second["this_session"]["hook_invocations_seen"] == 3  # delta = 8 - 5


async def test_session_delta_rebaselines_when_a_purge_resets_the_counters() -> None:
    """A store wipe resets the LIFETIME counters under a live server.

    ``furl_purge all=true`` -> ``store.clear()`` drops the counter rows
    (``DELETE FROM ccr_counters`` on sqlite, ``_counters.clear()`` in memory)
    while this server still holds its pre-purge snapshot. Clamping the resulting
    negative delta to 0 would report GENUINE post-purge events as
    ``this_session: 0`` until activity climbed back past the stale baseline. The
    reset must be DETECTED and that counter re-baselined instead.
    """
    store = resolve_ccr_namespace_store()
    store.increment_counter("hook_invocations_seen", 5)

    server = FurlMCPServer()  # ONE server across the purge
    first = _envelope(await server._handle_stats())["store"]["hook_activity"]
    assert first["this_session"]["hook_invocations_seen"] == 0  # baseline = 5

    store.clear()  # furl_purge all=true
    store.increment_counter("hook_invocations_seen", 4)  # 4 GENUINE new events

    after = _envelope(await server._handle_stats())["store"]["hook_activity"]
    assert after["hook_invocations_seen"] == 4  # lifetime restarted at the reset
    assert after["this_session"]["hook_invocations_seen"] == 4, (
        "post-purge events must be counted, not clamped to 0 by a stale baseline"
    )

    # And the re-baseline STICKS: once activity climbs past the stale 5 the
    # counter is no longer detectably below its snapshot, so a per-read
    # detection that did not persist the new baseline would silently revert to
    # subtracting 5 here and report 2 instead of 7.
    store.increment_counter("hook_invocations_seen", 3)
    later = _envelope(await server._handle_stats())["store"]["hook_activity"]
    assert later["hook_invocations_seen"] == 7
    assert later["this_session"]["hook_invocations_seen"] == 7


async def test_session_delta_buckets_noop_reasons() -> None:
    store = resolve_ccr_namespace_store()
    server = FurlMCPServer()
    _envelope(await server._handle_stats())  # snapshot baseline (all zero)
    store.increment_counter("hook_noop:below-min-chars", 4)
    activity = _envelope(await server._handle_stats())["store"]["hook_activity"]
    assert activity["hook_noop_reasons"] == {"below-min-chars": 4}
    assert activity["this_session"]["hook_noop_reasons"] == {"below-min-chars": 4}


# ── addition (a): hook_size_reroute surfaced by name ────────────────────────


async def test_hook_size_reroute_surfaced_by_name() -> None:
    store = resolve_ccr_namespace_store()
    store.increment_counter("hook_size_reroute", 2)
    activity = _envelope(await FurlMCPServer()._handle_stats())["store"]["hook_activity"]
    assert activity["hook_size_reroute"] == 2


# ── addition (c): pipe no-op reasons are dynamic-only ───────────────────────


async def test_pipe_noop_scope_names_dynamic_only_and_points_to_banner() -> None:
    store = resolve_ccr_namespace_store()
    store.increment_counter("pipe_invocations_seen", 3)
    store.increment_counter("pipe_noop:already-wrapped", 1)
    activity = _envelope(await FurlMCPServer()._handle_stats())["store"]["hook_activity"]
    assert "pipe_noop_scope" in activity
    scope = activity["pipe_noop_scope"]
    assert "DYNAMIC" in scope
    # The static gating reasons are named as NOT counted, and the banner is the
    # authoritative signal (addition b decline).
    assert "permission-rule" in scope and "banner" in scope


async def test_pipe_scope_absent_until_pipe_runs() -> None:
    store = resolve_ccr_namespace_store()
    store.increment_counter("hook_invocations_seen", 1)
    activity = _envelope(await FurlMCPServer()._handle_stats())["store"]["hook_activity"]
    assert "pipe_noop_scope" not in activity  # no pipe activity → no pipe block


# ── F11: top-level scope legend ─────────────────────────────────────────────


async def test_scope_legend_enumerates_the_scopes() -> None:
    stats = _envelope(await FurlMCPServer()._handle_stats())
    legend = stats["scopes"]
    assert "LIFETIME" in legend and "this_session" in legend
    assert "store" in legend and "process" in legend.lower()
