"""T7: PostToolUse hook version gating (compress_tool_output.py + the sibling
first-run note in _furl_ccr_counters.py).

Below Claude Code 2.1.163, ``updatedToolOutput`` is confirmed silently ignored
(the anthropics/claude-code#68951 class — see furl_ctx/host_version.py), so
whatever this hook produces never reaches the model. Pinned here:

* below the floor: the hook short-circuits to a passthrough BEFORE compressing
  or redacting, bucketed as a distinct noop reason so it is diagnosable; the
  first-run note names the actual detected version and the required floor
  instead of unconditionally claiming compression is active.
* the floor met, and the unknown-version case (no signal available — e.g. a
  non-native install), both preserve TODAY's behavior byte-for-byte: unknown is
  never treated as "assume broken".
* the invocations == compressions + noop_reasons invariant
  (test_hook_counters.py) still holds with the new bucket folded in.

Every test here EXPLICITLY controls CLAUDE_CODE_EXECPATH / AI_AGENT (never
inherits the ambient environment for these two) so results are deterministic
regardless of what Claude Code version, if any, is actually running the test
suite itself.
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

_COMPRESSIBLE = json.dumps([{"id": i, "status": "ok", "kind": "row"} for i in range(400)])

# Real, empirically-observed shapes (see tests/test_host_version.py) — an old
# host below the 2.1.163 floor, and a current one above it.
_BELOW_FLOOR_ENV = {"AI_AGENT": "claude-code_2-1-100_agent"}
_ABOVE_FLOOR_ENV = {"AI_AGENT": "claude-code_2-1-212_agent"}
_VERSION_ENV_VARS = ("CLAUDE_CODE_EXECPATH", "AI_AGENT")


def _run_hook(
    payload: dict,
    workspace: Path,
    project_dir: Path,
    version_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "FURL_CCR_BACKEND": "sqlite",
        "FURL_WORKSPACE_DIR": str(workspace),
        "FURL_CCR_PROJECT_DIR": str(project_dir),
    }
    for var in _VERSION_ENV_VARS:
        env.pop(var, None)  # never inherit the ambient session's version signal
    if version_env:
        env.update(version_env)
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _counters(workspace: Path, project_dir: Path) -> dict[str, int]:
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


def _compressible_payload() -> dict:
    return {"tool_name": "Bash", "tool_response": {"content": _COMPRESSIBLE}}


# --- below the floor: short-circuit, distinct bucket, no compression -----------


def test_below_floor_short_circuits_to_passthrough(tmp_path) -> None:
    ws, proj = tmp_path / "ws", tmp_path / "proj"
    proj.mkdir()
    proc = _run_hook(_compressible_payload(), ws, proj, _BELOW_FLOOR_ENV)
    assert proc.returncode == 0
    assert proc.stdout == "", "below the floor, the hook must not emit updatedToolOutput"
    assert _counters(ws, proj) == {
        "hook_invocations_seen": 1,
        "hook_noop:below-version-floor": 1,
    }


def test_below_floor_never_increments_compressions_applied(tmp_path) -> None:
    ws, proj = tmp_path / "ws", tmp_path / "proj"
    proj.mkdir()
    for _ in range(3):
        _run_hook(_compressible_payload(), ws, proj, _BELOW_FLOOR_ENV)
    counters = _counters(ws, proj)
    assert counters.get("hook_compressions_applied", 0) == 0
    assert counters["hook_noop:below-version-floor"] == 3


# --- floor met / unknown: today's behavior, unchanged ---------------------------


def test_floor_met_compresses_normally(tmp_path) -> None:
    ws, proj = tmp_path / "ws", tmp_path / "proj"
    proj.mkdir()
    proc = _run_hook(_compressible_payload(), ws, proj, _ABOVE_FLOOR_ENV)
    assert proc.returncode == 0
    assert "updatedToolOutput" in proc.stdout
    assert _counters(ws, proj) == {
        "hook_invocations_seen": 1,
        "hook_compressions_applied": 1,
    }


def test_unknown_version_preserves_default_compress_behavior(tmp_path) -> None:
    """No signal at all (both env vars absent — e.g. a non-native install):
    cannot prove either way, so this must behave EXACTLY like today (compress),
    never like "assume broken"."""
    ws, proj = tmp_path / "ws", tmp_path / "proj"
    proj.mkdir()
    proc = _run_hook(_compressible_payload(), ws, proj, version_env=None)
    assert proc.returncode == 0
    assert "updatedToolOutput" in proc.stdout
    assert _counters(ws, proj) == {
        "hook_invocations_seen": 1,
        "hook_compressions_applied": 1,
    }


# --- first-run note: below-floor wording vs. the default ------------------------


def test_first_run_note_below_floor_names_version_and_floor(tmp_path) -> None:
    ws, proj = tmp_path / "ws", tmp_path / "proj"
    proj.mkdir()
    proc = _run_hook(_compressible_payload(), ws, proj, _BELOW_FLOOR_ENV)
    assert "2.1.163" in proc.stderr, "note must name the required floor"
    assert "2.1.100" in proc.stderr, "note must name the detected current version"
    assert "hook_noop:below-version-floor" in proc.stderr
    assert "compression is active" not in proc.stderr, "must not claim it works below the floor"
    # The pipe is unaffected by this gate and must still be mentioned as running.
    assert "FURL_PRETOOL_PIPE=0" in proc.stderr


def test_first_run_note_default_when_floor_met(tmp_path) -> None:
    ws, proj = tmp_path / "ws", tmp_path / "proj"
    proj.mkdir()
    proc = _run_hook(_compressible_payload(), ws, proj, _ABOVE_FLOOR_ENV)
    assert "compression is active" in proc.stderr
    assert "anthropics/claude-code#68951" in proc.stderr
    assert "requires Claude Code" not in proc.stderr


def test_first_run_note_fires_once_even_below_floor(tmp_path) -> None:
    ws, proj = tmp_path / "ws", tmp_path / "proj"
    proj.mkdir()
    first = _run_hook(_compressible_payload(), ws, proj, _BELOW_FLOOR_ENV)
    second = _run_hook(_compressible_payload(), ws, proj, _BELOW_FLOOR_ENV)
    assert "requires Claude Code" in first.stderr
    assert second.stderr == ""


# --- invariant: invocations == compressions + noop_reasons, bucket included -----


def test_invocations_equal_outcomes_invariant_with_version_bucket(tmp_path) -> None:
    ws, proj = tmp_path / "ws", tmp_path / "proj"
    proj.mkdir()
    _run_hook(_compressible_payload(), ws, proj, _ABOVE_FLOOR_ENV)
    _run_hook(_compressible_payload(), ws, proj, _BELOW_FLOOR_ENV)
    _run_hook(_compressible_payload(), ws, proj, version_env=None)  # unknown -> compresses

    counters = _counters(ws, proj)
    invocations = counters.get("hook_invocations_seen", 0)
    compressions = counters.get("hook_compressions_applied", 0)
    noop_total = sum(v for k, v in counters.items() if k.startswith("hook_noop:"))
    assert invocations == 3
    assert compressions == 2, "floor-met and unknown both compress"
    assert counters["hook_noop:below-version-floor"] == 1
    assert invocations == compressions + noop_total


# --- disabled kill switch still wins over the version gate ----------------------


def test_disabled_kill_switch_still_checked_before_version_gate(tmp_path) -> None:
    """FURL_HOOK_ENABLED=0 must short-circuit BEFORE the version check runs, so
    the noop reason is "disabled", not the version-gate bucket, even below the
    floor -- unchanged ordering from before T7."""
    ws, proj = tmp_path / "ws", tmp_path / "proj"
    proj.mkdir()
    proc = _run_hook(
        _compressible_payload(), ws, proj, {**_BELOW_FLOOR_ENV, "FURL_HOOK_ENABLED": "0"}
    )
    assert proc.returncode == 0 and proc.stdout == ""
    assert _counters(ws, proj) == {"hook_invocations_seen": 1, "hook_noop:disabled": 1}
