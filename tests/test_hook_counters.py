"""PostToolUse hook observability counters + the once-per-store #68951 note.

The hook (a subprocess per tool call) tallies every run into the shared per-project
sqlite store so ``furl_stats`` can show cross-process activity. Invariant pinned
here: every run past payload-parse records exactly one outcome, so
``invocations_seen == compressions_applied + sum(noop_reasons)``. The first
DURABLY-recorded invocation also writes a one-line #68951 heads-up to stderr —
once per store, and never on the volatile in-memory backend (so library/unit runs
stay byte-silent on a no-op, preserving test_hook_payload_shapes' quiet contract).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_HOOK = (
    Path(__file__).resolve().parents[1] / "plugins" / "furl" / "hooks" / "compress_tool_output.py"
)

_COMPRESSIBLE = json.dumps([{"id": i, "status": "ok"} for i in range(400)])


def _run_hook(payload: dict, workspace: Path, project_dir: Path, extra: dict | None = None):
    env = {
        **os.environ,
        "FURL_CCR_BACKEND": "sqlite",
        "FURL_WORKSPACE_DIR": str(workspace),
        "FURL_CCR_PROJECT_DIR": str(project_dir),
    }
    if extra:
        env.update(extra)
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _counters(workspace: Path, project_dir: Path) -> dict[str, int]:
    """Read the counters the hook wrote, by opening the SAME per-project sqlite
    file directly (the path the hook derived from FURL_CCR_PROJECT_DIR)."""
    prev = {k: os.environ.get(k) for k in ("FURL_WORKSPACE_DIR", "FURL_CCR_PROJECT_DIR")}
    os.environ["FURL_WORKSPACE_DIR"] = str(workspace)
    os.environ["FURL_CCR_PROJECT_DIR"] = str(project_dir)
    try:
        from furl_ctx.cache.backends.sqlite import SqliteBackend
        from furl_ctx.cache.compression_store import (
            CompressionStore,
            _ccr_namespace_db_path,
            _namespace_key,
        )

        key = _namespace_key(None, None)
        assert key is not None
        store = CompressionStore(backend=SqliteBackend(db_path=_ccr_namespace_db_path(key)))
        try:
            return store.get_counters()
        finally:
            store.close()
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_compression_records_invocation_and_compression(tmp_path) -> None:
    ws, proj = tmp_path / "ws", tmp_path / "proj"
    proj.mkdir()
    payload = {"tool_name": "Bash", "tool_response": {"content": _COMPRESSIBLE}}
    proc = _run_hook(payload, ws, proj)
    assert proc.returncode == 0
    assert "updatedToolOutput" in proc.stdout
    assert _counters(ws, proj) == {
        "hook_invocations_seen": 1,
        "hook_compressions_applied": 1,
    }


def test_noop_records_bucketed_reason(tmp_path) -> None:
    ws, proj = tmp_path / "ws", tmp_path / "proj"
    proj.mkdir()
    payload = {
        "tool_name": "Bash",
        "tool_response": {"stdout": "tiny", "stderr": "", "interrupted": False},
    }
    proc = _run_hook(payload, ws, proj)
    assert proc.returncode == 0 and proc.stdout == ""
    assert _counters(ws, proj) == {
        "hook_invocations_seen": 1,
        "hook_noop:below-min-chars": 1,
    }


def test_first_run_note_fires_exactly_once(tmp_path) -> None:
    ws, proj = tmp_path / "ws", tmp_path / "proj"
    proj.mkdir()
    payload = {
        "tool_name": "Bash",
        "tool_response": {"stdout": "tiny", "stderr": "", "interrupted": False},
    }
    first = _run_hook(payload, ws, proj)
    second = _run_hook(payload, ws, proj)
    assert "anthropics/claude-code#68951" in first.stderr, "first durable run must warn once"
    # S1: the note now also tells the user the PreToolUse pipe is active by
    # default and how to opt out — the actionable half of the heads-up.
    assert "FURL_PRETOOL_PIPE=0" in first.stderr, "note must name the pipe opt-out"
    assert "#68951" not in second.stderr, "the note must not repeat"
    assert _counters(ws, proj)["hook_invocations_seen"] == 2


def test_memory_backend_stays_byte_silent_on_noop(tmp_path) -> None:
    """The in-memory backend is not durable, so the note never fires — the
    quiet-on-no-op contract (test_hook_payload_shapes) is preserved."""
    payload = {"tool_name": "Bash", "tool_response": {"foo": "bar"}}
    proc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, "FURL_CCR_BACKEND": "memory"},
    )
    assert proc.returncode == 0 and proc.stdout == ""
    assert proc.stderr == ""


def test_invocations_equal_outcomes_invariant(tmp_path) -> None:
    ws, proj = tmp_path / "ws", tmp_path / "proj"
    proj.mkdir()
    # A mix: one compression, one below-min-chars, one excluded tool.
    _run_hook({"tool_name": "Bash", "tool_response": {"content": _COMPRESSIBLE}}, ws, proj)
    _run_hook(
        {
            "tool_name": "Bash",
            "tool_response": {"stdout": "tiny", "stderr": "", "interrupted": False},
        },
        ws,
        proj,
    )
    _run_hook(
        {"tool_name": "mcp__x__furl_compress", "tool_response": {"content": "x" * 5000}}, ws, proj
    )
    counters = _counters(ws, proj)
    invocations = counters.get("hook_invocations_seen", 0)
    compressions = counters.get("hook_compressions_applied", 0)
    noop_total = sum(v for k, v in counters.items() if k.startswith("hook_noop:"))
    assert invocations == 3
    assert invocations == compressions + noop_total
