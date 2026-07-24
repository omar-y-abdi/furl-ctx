"""PostToolUse hook size reroute (F2): huge outputs take the fast CCR offload.

Report finding F2: a multi-megabyte single-line array left ZERO trace — no
marker, no counter — consistent with the hook process being killed by the 30 s
hooks.json timeout while it ran the super-linear crush path. The routing fix
(``is_mixed_content`` short-circuit) removes the misroute, and this hook-level
guard bounds worst-case wall time regardless: an output at/above
``FURL_HOOK_MAX_COMPRESS_BYTES`` is rerouted straight to the engine's O(n)
reversible CCR offload (``_ccr_summary`` preview + retrievable original), and the
run is tallied as ``hook_size_reroute`` so a processed huge blob is never silent.

Unit tests pin the two pure helpers; subprocess tests pin the end-to-end behavior
with a small env-forced threshold, and confirm a normal (below-threshold) output
still compresses without the reroute breadcrumb.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1] / "plugins" / "furl" / "hooks" / "compress_tool_output.py"
)


def _load_hook():
    """Import the hook module in-process. It does module-level
    ``os.environ.setdefault(...)``; save/restore so nothing leaks into the suite."""
    saved = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location("furl_hook_size_reroute_under_test", _HOOK)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


_hook = _load_hook()


@pytest.fixture(autouse=True)
def _restore_env():
    """Snapshot and restore the whole process environment around every test.

    ``_arm_size_reroute`` writes ``FURL_MAX_COMPRESS_BYTES`` DIRECTLY (correct for
    the real one-shot hook process, which exits right after), so a unit test that
    exercises it would otherwise leak that engine ceiling into the shared pytest
    process — monkeypatch cannot restore a var it never tracked (``delenv`` on an
    absent var records nothing). A leaked low ceiling makes later tests offload
    normal-sized content, so snapshot/restore the env here, the same pattern
    ``_load_hook`` already uses around the module import."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


# --- unit: threshold parsing --------------------------------------------------


def test_hook_max_bytes_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FURL_HOOK_MAX_COMPRESS_BYTES", raising=False)
    assert _hook._hook_max_bytes() == _hook._DEFAULT_HOOK_MAX_BYTES


@pytest.mark.parametrize("off", ["0", "off", "false", "no", "disabled", "OFF"])
def test_hook_max_bytes_disabled(monkeypatch: pytest.MonkeyPatch, off: str) -> None:
    monkeypatch.setenv("FURL_HOOK_MAX_COMPRESS_BYTES", off)
    assert _hook._hook_max_bytes() is None


def test_hook_max_bytes_explicit_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FURL_HOOK_MAX_COMPRESS_BYTES", "1234567")
    assert _hook._hook_max_bytes() == 1234567
    monkeypatch.setenv("FURL_HOOK_MAX_COMPRESS_BYTES", "not-a-number")
    assert _hook._hook_max_bytes() == _hook._DEFAULT_HOOK_MAX_BYTES


# --- unit: arming the reroute -------------------------------------------------


def test_arm_reroute_below_threshold_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FURL_HOOK_MAX_COMPRESS_BYTES", "10000")
    monkeypatch.delenv("FURL_MAX_COMPRESS_BYTES", raising=False)
    assert _hook._arm_size_reroute(9_999) is False
    assert os.environ.get("FURL_MAX_COMPRESS_BYTES") is None


def test_arm_reroute_lowers_engine_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FURL_HOOK_MAX_COMPRESS_BYTES", "10000")
    monkeypatch.delenv("FURL_MAX_COMPRESS_BYTES", raising=False)
    assert _hook._arm_size_reroute(10_000) is True
    assert os.environ["FURL_MAX_COMPRESS_BYTES"] == "10000"


def test_arm_reroute_keeps_lower_explicit_engine_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user who set a LOWER engine ceiling wants to offload even earlier — keep
    it (min), never raise it."""
    monkeypatch.setenv("FURL_HOOK_MAX_COMPRESS_BYTES", "10000")
    monkeypatch.setenv("FURL_MAX_COMPRESS_BYTES", "4000")
    assert _hook._arm_size_reroute(20_000) is True
    assert os.environ["FURL_MAX_COMPRESS_BYTES"] == "4000"


def test_arm_reroute_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FURL_HOOK_MAX_COMPRESS_BYTES", "0")
    monkeypatch.delenv("FURL_MAX_COMPRESS_BYTES", raising=False)
    assert _hook._arm_size_reroute(10_000_000) is False
    assert os.environ.get("FURL_MAX_COMPRESS_BYTES") is None


# --- subprocess: end-to-end ---------------------------------------------------


def _run_hook(payload: dict, project_dir: Path, extra: dict | None = None):
    env = {
        **os.environ,
        "FURL_CCR_BACKEND": "sqlite",
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


def _counters(project_dir: Path) -> dict[str, int]:
    prev = os.environ.get("FURL_CCR_PROJECT_DIR")
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
        if prev is None:
            os.environ.pop("FURL_CCR_PROJECT_DIR", None)
        else:
            os.environ["FURL_CCR_PROJECT_DIR"] = prev


# A compressible array well over the small forced threshold below.
_BIG_ARRAY = json.dumps(
    [{"id": i, "status": "ok", "name": f"Event Number {i} Fired Here"} for i in range(400)]
)


def test_over_threshold_output_reroutes_to_offload_and_records_counter(tmp_path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    payload = {"tool_name": "Bash", "tool_response": {"content": _BIG_ARRAY}}
    proc = _run_hook(payload, proj, extra={"FURL_HOOK_MAX_COMPRESS_BYTES": "3000"})
    assert proc.returncode == 0
    assert "updatedToolOutput" in proc.stdout
    # The rerouted output is the reversible CCR offload: a queryable summary plus
    # the retrieval marker (NOT the columnar table). The mirrored Bash shape wraps
    # the compressed text back into ``{"content": <str>}``, so serialize to assert.
    out = json.loads(proc.stdout)["hookSpecificOutput"]["updatedToolOutput"]
    out_text = json.dumps(out)
    assert "<<ccr:" in out_text and "_ccr_summary" in out_text
    counters = _counters(proj)
    # The reroute rides ALONGSIDE the compression counter (it emits a marker) and
    # leaves its own distinct breadcrumb — never a no-op.
    assert counters.get("hook_size_reroute") == 1
    assert counters.get("hook_compressions_applied") == 1
    assert counters.get("hook_invocations_seen") == 1
    assert not any(k.startswith("hook_noop:") for k in counters)


def test_below_threshold_output_compresses_without_reroute(tmp_path) -> None:
    """With the default (5 MB) threshold, a normal compressible array takes the
    ordinary columnar path — no reroute breadcrumb is recorded."""
    proj = tmp_path / "proj"
    proj.mkdir()
    payload = {"tool_name": "Bash", "tool_response": {"content": _BIG_ARRAY}}
    proc = _run_hook(payload, proj)  # default threshold
    assert proc.returncode == 0
    assert "updatedToolOutput" in proc.stdout
    counters = _counters(proj)
    assert counters.get("hook_compressions_applied") == 1
    assert "hook_size_reroute" not in counters
