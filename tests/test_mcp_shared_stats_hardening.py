"""SEC-6 / SEC-7 — shared session-stats file: locking + live paths.

SEC-6: ``_read_shared_events`` used to read under ``LOCK_SH``, release, then
prune-rewrite under a SEPARATE ``open(..., "w")`` + ``LOCK_EX`` — an event
appended by another MCP process between the two locks was silently lost, and
the ``"w"`` open truncated the file BEFORE its lock was even acquired (not
atomic even against a correctly-locked appender). The fix is one
``open(..., "r+")`` handle under a single ``LOCK_EX``: read, filter,
``seek(0)``, ``truncate()``, write.

SEC-7: ``SHARED_STATS_DIR``/``SHARED_STATS_FILE`` were frozen at import,
contradicting paths.py's explicit no-caching contract (the furl_read jail
re-reads ``FURL_WORKSPACE_DIR`` per call; the stats paths did not, so the two
could disagree about the workspace). They are now functions —
``shared_stats_dir()`` / ``shared_stats_file()`` — called at each use site.

These tests use the real module functions and real ``flock`` (no mocked
store); the interleave test injects a concurrent append exactly at the first
lock release, which is the gap the old two-lock flow left open.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from furl_ctx.ccr import mcp_server

_needs_flock = pytest.mark.skipif(
    not mcp_server._HAS_FCNTL, reason="flock-based tests require fcntl (Unix)"
)


def _event(marker: str, *, age_seconds: float = 0.0) -> dict:
    return {"type": "compress", "timestamp": time.time() - age_seconds, "marker": marker}


def _seed(stats_file: Path, events: list[dict]) -> None:
    stats_file.parent.mkdir(parents=True, exist_ok=True)
    stats_file.write_text("".join(json.dumps(e) + "\n" for e in events))


def _markers_on_disk(stats_file: Path) -> set[str]:
    return {
        json.loads(line)["marker"] for line in stats_file.read_text().splitlines() if line.strip()
    }


# ─── SEC-7: stats paths re-read the environment per call ────────────────────


def test_shared_stats_paths_follow_env_mid_session(tmp_path: Path, monkeypatch) -> None:
    ws_a = tmp_path / "wsA"
    ws_b = tmp_path / "wsB"

    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(ws_a))
    assert mcp_server.shared_stats_dir() == ws_a
    assert mcp_server.shared_stats_file() == ws_a / "session_stats.jsonl"

    # Re-point the workspace mid-session: the very next call must follow — no import-frozen snapshot (the module no-caching contract).
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(ws_b))
    assert mcp_server.shared_stats_dir() == ws_b
    assert mcp_server.shared_stats_file() == ws_b / "session_stats.jsonl"


def test_append_and_read_follow_workspace_env(tmp_path: Path, monkeypatch) -> None:
    ws_a = tmp_path / "wsA"
    ws_b = tmp_path / "wsB"

    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(ws_a))
    mcp_server._append_shared_event(_event("in-a"))
    assert (ws_a / "session_stats.jsonl").is_file()

    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(ws_b))
    mcp_server._append_shared_event(_event("in-b"))

    markers_b = {e.get("marker") for e in mcp_server._read_shared_events()}
    assert markers_b == {"in-b"}, "reads must target the CURRENT workspace"

    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(ws_a))
    markers_a = {e.get("marker") for e in mcp_server._read_shared_events()}
    assert markers_a == {"in-a"}


# ─── SEC-6: single-lock read + prune-rewrite ─────────────────────────────────


class _FirstUnlockHook:
    """Real flock, plus a one-shot callback fired right after the FIRST unlock.

    Under the OLD two-lock flow the first unlock is the ``LOCK_SH`` release
    after the read — the exact start of the lost-update window — so the
    callback lands its append precisely where a concurrent MCP process could.
    Under the FIXED single-lock flow the first unlock only happens after the
    rewrite completed, so the same append must land after it and survive.
    """

    def __init__(self, real_fcntl, callback) -> None:
        self.LOCK_SH = real_fcntl.LOCK_SH
        self.LOCK_EX = real_fcntl.LOCK_EX
        self.LOCK_UN = real_fcntl.LOCK_UN
        self._real = real_fcntl
        self._callback = callback
        self._fired = False

    def flock(self, fd, op) -> None:
        self._real.flock(fd, op)
        if op == self.LOCK_UN and not self._fired:
            self._fired = True
            self._callback()


@_needs_flock
def test_append_landing_at_first_unlock_survives_prune(tmp_path: Path, monkeypatch) -> None:
    import fcntl as real_fcntl

    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    stats_file = tmp_path / "session_stats.jsonl"
    # A stale event forces the prune-rewrite; a fresh one must be kept.
    _seed(stats_file, [_event("stale", age_seconds=99_999), _event("fresh")])

    def concurrent_append() -> None:
        # A correctly-locked appender (the _append_shared_event protocol) that
        # wins the lock the moment the reader releases it.
        with open(stats_file, "a") as f:
            real_fcntl.flock(f, real_fcntl.LOCK_EX)
            f.write(json.dumps(_event("appended")) + "\n")
            f.flush()
            real_fcntl.flock(f, real_fcntl.LOCK_UN)

    monkeypatch.setattr(mcp_server, "fcntl", _FirstUnlockHook(real_fcntl, concurrent_append))

    events = mcp_server._read_shared_events()

    returned = {e.get("marker") for e in events}
    assert "fresh" in returned and "stale" not in returned

    surviving = _markers_on_disk(stats_file)
    assert "appended" in surviving, (
        "an event appended by a correctly-locked writer during the "
        "read-prune-rewrite was destroyed by the rewrite (SEC-6 lost update)"
    )
    assert "fresh" in surviving
    assert "stale" not in surviving, "the prune itself must still happen"


@_needs_flock
def test_concurrent_appends_never_lost_during_prune(tmp_path: Path, monkeypatch) -> None:
    # Real-concurrency property: appender threads (real flock via _append_shared_event) race reader
    # threads running the prune-rewrite. Whatever the interleave, every appended event must survive.
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    stats_file = tmp_path / "session_stats.jsonl"
    _seed(stats_file, [_event(f"stale-{i}", age_seconds=99_999) for i in range(5)])

    n_threads, n_events = 3, 8
    expected = {f"t{t}-{i}" for t in range(n_threads) for i in range(n_events)}
    start = threading.Barrier(n_threads + 2)

    def appender(t: int) -> None:
        start.wait()
        for i in range(n_events):
            mcp_server._append_shared_event(_event(f"t{t}-{i}"))

    def reader() -> None:
        start.wait()
        for _ in range(10):
            mcp_server._read_shared_events()

    threads = [threading.Thread(target=appender, args=(t,)) for t in range(n_threads)]
    threads += [threading.Thread(target=reader), threading.Thread(target=reader)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)
        assert not th.is_alive(), "locking deadlocked"

    final = {e.get("marker") for e in mcp_server._read_shared_events()}
    lost = expected - final
    assert not lost, f"events lost across concurrent append/prune: {sorted(lost)}"
    assert not any(m.startswith("stale-") for m in final if m)
