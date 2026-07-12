"""content_kind threading — the originating tool reaches stored CCR metadata.

Finding 2: ``furl_list``/``furl_retrieve`` document a ``content_kind`` ("the
originating tool, when known"), but every entry — including hook-generated ones
from real Bash calls — showed ``content_kind: null``. The tool name was never
threaded from the entry points down to ``store.store()``: the router's CCR
offload and SmartCrusher have no per-message tool attribution for a single
wrapped tool output, so they wrote ``tool_name=None``.

The fix threads the originating tool request-scoped through ``compress()`` (a
ContextVar the store reads as the default when a writer supplies none), plus
explicit labels on the MCP ``furl_compress`` store call. These tests pin:

* ``compress(tool_name=...)`` labels the entries it writes;
* the store ContextVar fallback fills an unattributed write but never overrides
  an explicit ``tool_name``;
* a hook-stored Bash entry surfaces ``content_kind="Bash"`` through BOTH
  ``furl_list`` and ``furl_retrieve``;
* MCP ``furl_compress`` entries are labelled ``"mcp:furl_compress"``;
* with no tool name threaded, ``content_kind`` stays ``None`` (unchanged).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_HOOK = _REPO / "plugins" / "furl" / "hooks" / "compress_tool_output.py"


def _big_log() -> str:
    # Low-redundancy, line-oriented, >4000 chars -> router:ccr_offload path,
    # whose store write carries no tool attribution (the finding's null case).
    return (
        "\n".join(
            f"{i:04d} event={i * 7 % 97} host=node-{i % 13} latency={i * 3 % 211}ms status=ok payload"
            for i in range(220)
        )
        + "\n"
    )


@pytest.fixture(autouse=True)
def _fresh_store() -> object:
    from furl_ctx.cache.compression_store import reset_compression_store

    reset_compression_store()
    yield
    reset_compression_store()


# ─── compress() request-scoped labelling ─────────────────────────────────────


def test_compress_labels_entries_with_tool_name() -> None:
    from furl_ctx import compress
    from furl_ctx.cache.compression_store import get_compression_store

    compress([{"role": "tool", "content": _big_log()}], tool_name="Bash")
    store = get_compression_store()
    entries = list(store._backend.items())
    assert entries, "expected a stored CCR entry"
    assert all(entry.tool_name == "Bash" for _hash, entry in entries)


def test_multi_message_parallel_compress_preserves_content_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review F3: the router compresses multiple cache-miss messages in a
    # ThreadPoolExecutor (FURL_COMPRESS_WORKERS, default 4). A plain
    # executor.submit does NOT carry ContextVars into the worker thread, so the
    # request-scoped tool name bound by compress() was lost — every entry from
    # a multi-message call stored content_kind=None while single-message (the
    # plugin path, compressed inline on the calling thread) worked. Submissions
    # now run under contextvars.copy_context(), so the binding propagates.
    from furl_ctx import compress
    from furl_ctx.cache.compression_store import get_compression_store

    monkeypatch.setenv("FURL_COMPRESS_WORKERS", "4")
    log_a = _big_log()
    log_b = "\n".join(
        f"{i:04d} req={i * 11 % 89} route=/api/v{i % 7} elapsed={i * 5 % 173}ms code=200 body"
        for i in range(220)
    )
    compress(
        [
            {"role": "tool", "content": log_a},
            {"role": "tool", "content": log_b},
        ],
        tool_name="Bash",
    )
    store = get_compression_store()
    entries = list(store._backend.items())
    assert len(entries) >= 2, f"expected both messages stored, got {len(entries)}"
    kinds = sorted({entry.tool_name for _hash, entry in entries}, key=str)
    assert kinds == ["Bash"], f"content_kind lost across worker threads: {kinds}"


def test_compress_without_tool_name_stays_null() -> None:
    from furl_ctx import compress
    from furl_ctx.cache.compression_store import get_compression_store

    compress([{"role": "tool", "content": _big_log()}])  # no tool_name
    store = get_compression_store()
    entries = list(store._backend.items())
    assert entries
    assert all(entry.tool_name is None for _hash, entry in entries)


# ─── store ContextVar fallback semantics ─────────────────────────────────────


def test_store_contextvar_fills_unattributed_write() -> None:
    from furl_ctx.cache.compression_store import CompressionStore, _request_tool_name

    store = CompressionStore()
    token = _request_tool_name.set("Bash")
    try:
        # A writer that supplies no tool_name inherits the bound request tool.
        h1 = store.store(original="a" * 40, compressed="preview-1")
        assert store._backend.get(h1).tool_name == "Bash"
        # An explicit tool_name is NEVER overridden by the fallback.
        h2 = store.store(original="b" * 40, compressed="preview-2", tool_name="Read")
        assert store._backend.get(h2).tool_name == "Read"
    finally:
        _request_tool_name.reset(token)

    # Outside any bound context the default is None (unchanged behavior).
    h3 = store.store(original="c" * 40, compressed="preview-3")
    assert store._backend.get(h3).tool_name is None


# ─── hook path: content_kind via furl_list AND furl_retrieve ─────────────────


def _run_hook(tmp_path: Path, stdout_text: str) -> Path:
    env = dict(os.environ)
    db = tmp_path / "ccr.sqlite3"
    env["FURL_CCR_BACKEND"] = "sqlite"
    env["FURL_CCR_SQLITE_PATH"] = str(db)
    env["FURL_CCR_PROJECT_DIR"] = ""  # disable namespacing -> deterministic path
    env.pop("FURL_CCR_NAMESPACE", None)
    env["FURL_HOOK_MIN_CHARS"] = "500"
    env.pop("FURL_REDACT_PATTERNS", None)
    payload = {
        "tool_name": "Bash",
        "tool_response": {"stdout": stdout_text},
        "cwd": str(tmp_path),
        "hook_event_name": "PostToolUse",
    }
    proc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip(), "hook should have compressed and emitted a replacement"
    return db


def _server_on(db: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    pytest.importorskip("mcp")
    from furl_ctx.cache.backends.sqlite import SqliteBackend
    from furl_ctx.cache.compression_store import CompressionStore
    from furl_ctx.ccr.mcp_server import FurlMCPServer

    # No namespace active -> _get_local_store returns the store we attach.
    monkeypatch.delenv("FURL_CCR_NAMESPACE", raising=False)
    monkeypatch.delenv("FURL_CCR_PROJECT_DIR", raising=False)
    server = FurlMCPServer()
    server._local_store = CompressionStore(backend=SqliteBackend(db_path=str(db)))
    return server


def test_hook_entry_surfaces_content_kind_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _run_hook(tmp_path, _big_log())
    server = _server_on(db, monkeypatch)

    # furl_list: the entry shows content_kind="Bash".
    listing = server._list_entries(50, 0)
    kinds = [entry["content_kind"] for entry in listing["entries"]]
    assert kinds, "furl_list returned no entries"
    assert "Bash" in kinds, f"expected a Bash entry, got {kinds}"

    # furl_retrieve: the same entry surfaces content_kind="Bash".
    for entry in listing["entries"]:
        if entry["content_kind"] == "Bash":
            retrieved = server._retrieve_content_sync(entry["hash"], None)
            assert retrieved.get("content_kind") == "Bash"
            break
    else:  # pragma: no cover - guarded by the assertion above
        pytest.fail("no Bash entry to retrieve")


# ─── MCP furl_compress labelling ─────────────────────────────────────────────


def test_mcp_compress_entries_labelled_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    from furl_ctx.cache.compression_store import reset_compression_store
    from furl_ctx.ccr.mcp_server import FurlMCPServer

    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    monkeypatch.setenv("FURL_CCR_SQLITE_PATH", str(tmp_path / "ccr.sqlite3"))
    monkeypatch.delenv("FURL_CCR_PROJECT_DIR", raising=False)
    monkeypatch.delenv("FURL_CCR_NAMESPACE", raising=False)
    reset_compression_store()

    server = FurlMCPServer()
    server._compress_content(_big_log())

    store = server._get_local_store()
    entries = list(store._backend.items())
    assert entries, "furl_compress should have stored an entry"
    assert all(entry.tool_name == "mcp:furl_compress" for _hash, entry in entries)
    reset_compression_store()
