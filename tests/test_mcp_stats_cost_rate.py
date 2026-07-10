"""furl_stats cost-rate honesty under a custom FURL_COST_RATE_USD_PER_MTOK.

Closes a known gap: the COMBINED ``estimated_cost_saved_usd`` (main session +
sub-agents) had no test that a custom blended $/Mtok rate flows through it. The
combined block only appears when the shared stats file carries events from
another process, so this seeds a cross-pid compress event and asserts the
combined cost uses the custom rate, not the ~$3 default.
"""

from __future__ import annotations

import json
import os
import time

import pytest

pytest.importorskip("mcp")

import mcp.types as mt  # noqa: E402

from furl_ctx.cache.compression_store import reset_compression_store  # noqa: E402
from furl_ctx.ccr import mcp_server as ms  # noqa: E402
from furl_ctx.ccr.mcp_server import FurlMCPServer  # noqa: E402

_CUSTOM_RATE = 12.0  # 4x the ~$3 default, so a wrong rate is unmistakable.
_OTHER_INPUT = 100_000
_OTHER_OUTPUT = 20_000
_OTHER_SAVED = _OTHER_INPUT - _OTHER_OUTPUT  # 80_000


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("FURL_CCR_BACKEND", "memory")
    reset_compression_store()
    yield
    reset_compression_store()


def _envelope(result: list[mt.TextContent]) -> dict:
    assert len(result) == 1
    return json.loads(result[0].text)


def _seed_cross_pid_compress_event() -> None:
    """Append a compress event attributed to a DIFFERENT process pid."""
    path = ms.shared_stats_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "type": "compress",
        "input_tokens": _OTHER_INPUT,
        "output_tokens": _OTHER_OUTPUT,
        "timestamp": time.time(),
        "pid": os.getpid() + 1,  # not this process → counted as a sub-agent
    }
    with open(path, "a") as handle:
        handle.write(json.dumps(event) + "\n")


async def test_combined_cost_uses_custom_rate(monkeypatch) -> None:
    monkeypatch.setenv("FURL_COST_RATE_USD_PER_MTOK", str(_CUSTOM_RATE))
    _seed_cross_pid_compress_event()

    stats = _envelope(await FurlMCPServer()._handle_stats())

    assert "combined" in stats, "combined block requires cross-pid events"
    assert "sub_agents" in stats
    assert stats["sub_agents"]["tokens_saved"] == _OTHER_SAVED

    expected = round(_OTHER_SAVED * _CUSTOM_RATE / 1_000_000, 4)
    assert stats["combined"]["estimated_cost_saved_usd"] == expected

    # And it is provably NOT the default-rate value.
    default_value = round(_OTHER_SAVED * ms._DEFAULT_COST_RATE_USD_PER_MTOK / 1_000_000, 4)
    assert stats["combined"]["estimated_cost_saved_usd"] != default_value


async def test_combined_cost_falls_back_to_default_on_invalid_rate(monkeypatch) -> None:
    monkeypatch.setenv("FURL_COST_RATE_USD_PER_MTOK", "not-a-number")
    _seed_cross_pid_compress_event()

    stats = _envelope(await FurlMCPServer()._handle_stats())

    expected = round(_OTHER_SAVED * ms._DEFAULT_COST_RATE_USD_PER_MTOK / 1_000_000, 4)
    assert stats["combined"]["estimated_cost_saved_usd"] == expected
