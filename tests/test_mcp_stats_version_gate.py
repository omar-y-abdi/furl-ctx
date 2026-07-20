"""T7: ``furl_stats``' ``store.post_tool_use_compression`` block.

Answers "can this host even receive a PostToolUse replacement" directly and
independently of the ``hook_activity`` counters (which the PostToolUse hook, a
separate cheap per-tool-call subprocess, produces best-effort). This call site
is an on-demand diagnostic tool, not a hot path, so it can afford the
``--version`` subprocess fallback furl_ctx.host_version offers, giving a more
thorough answer than the hook's own env-var-only check does.

Every test explicitly controls CLAUDE_CODE_EXECPATH / AI_AGENT (never inherits
the ambient environment) so results are deterministic regardless of what Claude
Code version, if any, is actually running the test suite.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from furl_ctx.cache.compression_store import reset_compression_store  # noqa: E402
from furl_ctx.ccr.mcp_server import FurlMCPServer  # noqa: E402

_VERSION_ENV_VARS = ("CLAUDE_CODE_EXECPATH", "AI_AGENT")


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("FURL_CCR_BACKEND", raising=False)
    for var in _VERSION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    reset_compression_store()
    yield
    reset_compression_store()


@pytest.fixture
def server() -> FurlMCPServer:
    return FurlMCPServer()


def test_above_floor_reports_can_deliver_true(server, monkeypatch) -> None:
    monkeypatch.setenv("AI_AGENT", "claude-code_2-1-212_agent")
    block = server._compute_stats()["store"]["post_tool_use_compression"]
    assert block["can_deliver"] is True
    assert block["host_version"] == "2.1.212"
    assert block["min_version_required"] == "2.1.163"


def test_below_floor_reports_can_deliver_false(server, monkeypatch) -> None:
    monkeypatch.setenv("AI_AGENT", "claude-code_2-1-100_agent")
    block = server._compute_stats()["store"]["post_tool_use_compression"]
    assert block["can_deliver"] is False
    assert block["host_version"] == "2.1.100"


def test_exact_floor_version_reports_can_deliver_true(server, monkeypatch) -> None:
    monkeypatch.setenv("AI_AGENT", "claude-code_2-1-163_agent")
    block = server._compute_stats()["store"]["post_tool_use_compression"]
    assert block["can_deliver"] is True


def test_unknown_version_reports_none_not_a_guess(server, monkeypatch) -> None:
    """Neither env var set AND the subprocess fallback fails (no 'claude' on a
    scrubbed PATH): must report None/null, never coerce to True or False."""
    monkeypatch.setenv("PATH", "/nonexistent-path-for-this-test")
    block = server._compute_stats()["store"]["post_tool_use_compression"]
    assert block["can_deliver"] is None
    assert block["host_version"] is None


async def test_block_ships_through_handle_stats_json_envelope(server, monkeypatch) -> None:
    """The block reaches the host through the real _handle_stats JSON, matching
    tests/test_mcp_stats_store_derived.py's envelope-round-trip pattern."""
    import json

    monkeypatch.setenv("AI_AGENT", "claude-code_2-1-212_agent")
    result = await server._handle_stats()
    env = json.loads(result[0].text)
    block = env["store"]["post_tool_use_compression"]
    assert block["can_deliver"] is True
    assert "note" in block


def test_block_shape_is_scoped_to_post_tool_use_only(server, monkeypatch) -> None:
    """The block carries exactly these four keys -- no pipe_* fields leak in
    here; the PreToolUse pipe does not depend on updatedToolOutput at all and
    is reported separately, in hook_activity's pipe_* fields."""
    monkeypatch.setenv("AI_AGENT", "claude-code_2-1-100_agent")
    block = server._compute_stats()["store"]["post_tool_use_compression"]
    assert set(block.keys()) == {"note", "host_version", "min_version_required", "can_deliver"}
