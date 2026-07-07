"""Observability: FURL_COST_RATE_USD_PER_MTOK rate + FURL_HOOK_VERBOSE annotation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from furl_ctx.ccr.mcp_server import _cost_rate_per_mtok

_HOOK = (
    Path(__file__).resolve().parents[1] / "plugins" / "furl" / "hooks" / "compress_tool_output.py"
)


def test_cost_rate_default(monkeypatch) -> None:
    monkeypatch.delenv("FURL_COST_RATE_USD_PER_MTOK", raising=False)
    assert _cost_rate_per_mtok() == 3.0


def test_cost_rate_override(monkeypatch) -> None:
    monkeypatch.setenv("FURL_COST_RATE_USD_PER_MTOK", "15")
    assert _cost_rate_per_mtok() == 15.0


def test_cost_rate_invalid_and_negative_fall_back(monkeypatch) -> None:
    monkeypatch.setenv("FURL_COST_RATE_USD_PER_MTOK", "banana")
    assert _cost_rate_per_mtok() == 3.0
    monkeypatch.setenv("FURL_COST_RATE_USD_PER_MTOK", "-5")
    assert _cost_rate_per_mtok() == 3.0


def test_hook_verbose_writes_one_line_stderr_annotation() -> None:
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_response": {
                "content": json.dumps([{"id": i, "status": "ok"} for i in range(400)])
            },
        }
    )
    proc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "FURL_HOOK_VERBOSE": "1", "FURL_CCR_BACKEND": "memory"},
    )
    assert proc.returncode == 0
    assert "furl: Bash" in proc.stderr  # the one-line annotation
    assert "updatedToolOutput" in proc.stdout  # compression still shipped
