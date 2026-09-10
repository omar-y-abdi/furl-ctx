"""furl_compress ``file_path`` ingest — feature + jail security regressions (F1).

``furl_compress`` gained a ``file_path`` input so the server reads a large
artifact from disk itself (a 33 MB trace never has to be pasted inline to be
compressed). It reuses the SAME hardened ingress as ``furl_read``
(``_read_jailed_file`` → ``_open_jailed``), with its own larger, env-overridable
ceiling (``FURL_MCP_MAX_FILE_BYTES``). These tests drive the real async handler
against a REAL in-process CCR store, asserting the structured envelopes and every
jail-escape class the ingress claims to reject.

Requires the optional ``mcp`` extra, mirroring test_mcp_server_handlers.py.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import mcp.types as mt  # noqa: E402

from furl_ctx import retrieve  # noqa: E402
from furl_ctx.cache.compression_store import reset_compression_store  # noqa: E402
from furl_ctx.ccr.mcp_server import FurlMCPServer  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Jail file access + the store/stats paths to the per-test sandbox.
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("FURL_CCR_TTL_SECONDS", raising=False)
    monkeypatch.delenv("FURL_MCP_MAX_FILE_BYTES", raising=False)
    monkeypatch.delenv("FURL_MAX_COMPRESS_BYTES", raising=False)
    reset_compression_store()
    yield
    reset_compression_store()


@pytest.fixture
def server() -> FurlMCPServer:
    return FurlMCPServer()


def _envelope(result: list[mt.TextContent]) -> dict:
    assert len(result) == 1, f"expected one TextContent, got {result!r}"
    item = result[0]
    assert isinstance(item, mt.TextContent)
    return json.loads(item.text)


# ─── happy paths ────────────────────────────────────────────────────────────


async def test_file_path_small_round_trips(server, tmp_path: Path) -> None:
    # A small file compresses and is byte-exactly retrievable — the plumbing
    # (read → jail → decode → compress → store) works end to end.
    f = tmp_path / "code.py"
    body = "def add(a, b):\n    return a + b\n" * 60
    f.write_text(body)

    env = _envelope(await server._handle_compress({"file_path": str(f)}))
    assert "error" not in env, env
    assert env.get("hash"), env
    assert retrieve(env["hash"]) == body  # no secrets → redaction is a no-op


async def test_file_path_large_trace_crosses_fast_path_and_offloads(server, tmp_path: Path) -> None:
    # THE end-to-end test the finding is about: a file large enough to cross the 8 MiB huge-content fast-path
    # switch is ingested from disk, offloaded to CCR (marker + _ccr_summary present), and recovered byte-exact.
    rows = [{"row": i, "ph": "X", "name": "RunTask", "dur": i % 997} for i in range(150_000)]
    body = json.dumps(rows)
    assert len(body) > 8 * 1024 * 1024, f"fixture only {len(body)} bytes, must exceed 8 MiB"
    big = tmp_path / "trace.json"
    big.write_text(body)

    env = _envelope(await server._handle_compress({"file_path": str(big)}))
    assert "error" not in env, env
    compressed = env["compressed"]
    assert "ccr_offload" in " ".join(env.get("transforms", [])) or "_ccr_dropped" in compressed
    assert "_ccr_summary" in compressed, "offload preview should carry the signal-aware summary"
    assert env.get("hash"), env
    assert retrieve(env["hash"]) == body  # full original recoverable byte-exact


# ─── input-source validation ────────────────────────────────────────────────


async def test_both_content_and_file_path_rejected(server, tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hi")
    env = _envelope(await server._handle_compress({"content": "inline", "file_path": str(f)}))
    assert env == {"error": "provide either 'content' or 'file_path', not both"}


async def test_neither_content_nor_file_path_keeps_legacy_error(server) -> None:
    # Back-compat: an empty call still reports the historical content error.
    env = _envelope(await server._handle_compress({}))
    assert env == {"error": "content parameter is required"}


async def test_file_path_non_string_rejected(server) -> None:
    env = _envelope(await server._handle_compress({"file_path": 12345}))
    assert env["error"].startswith("file_path parameter must be a string")


async def test_file_path_empty_string_rejected(server) -> None:
    env = _envelope(await server._handle_compress({"file_path": ""}))
    # "" is falsy → routed to the content branch's required-parameter error,
    # since file_path is absent (get returns "" which is not None but empty).
    assert env["error"] in (
        "file_path parameter must not be empty",
        "content parameter is required",
    )


# ─── jail: every escape class the shared ingress claims to reject ────────────


async def test_file_path_outside_workspace_rejected(server, tmp_path: Path) -> None:
    escapee = tmp_path.parent / "escapee_outside_jail.json"
    env = _envelope(await server._handle_compress({"file_path": str(escapee)}))
    assert env["error"] == "path outside workspace"
    assert "escapee" not in env["error"]  # attempted path never echoed back


async def test_file_path_absolute_traversal_rejected(server, tmp_path: Path) -> None:
    # An absolute path with .. that climbs out of the jail resolves outside it.
    sneaky = tmp_path / ".." / "etc_passwd_lookalike"
    env = _envelope(await server._handle_compress({"file_path": str(sneaky)}))
    assert env["error"] == "path outside workspace"


async def test_file_path_directory_reports_not_a_file(server, tmp_path: Path) -> None:
    sub = tmp_path / "adir"
    sub.mkdir()
    env = _envelope(await server._handle_compress({"file_path": str(sub)}))
    assert env["error"].startswith("Not a file:")


async def test_file_path_nonexistent_reports_not_found(server, tmp_path: Path) -> None:
    env = _envelope(await server._handle_compress({"file_path": str(tmp_path / "nope.json")}))
    assert env["error"].startswith("File not found:")


async def test_file_path_symlink_escaping_jail_rejected(server, tmp_path: Path) -> None:
    # A symlink INSIDE the jail pointing OUTSIDE it: resolve() follows it out,
    # is_relative_to then rejects — the out-of-jail target is never read.
    outside = tmp_path.parent / "secret_outside.txt"
    outside.write_text("out-of-jail secret")
    link = tmp_path / "link.json"
    link.symlink_to(outside)
    try:
        env = _envelope(await server._handle_compress({"file_path": str(link)}))
        assert env["error"] == "path outside workspace"
    finally:
        outside.unlink(missing_ok=True)


async def test_file_path_hardlink_rejected(server, tmp_path: Path) -> None:
    # A multiply-linked inode can point at an out-of-jail inode invisibly to
    # resolve(); the fstat nlink gate rejects it with an honest message.
    target = tmp_path / "orig.txt"
    target.write_text("linked twice")
    link = tmp_path / "hard.txt"
    os.link(target, link)
    env = _envelope(await server._handle_compress({"file_path": str(link)}))
    assert env["error"] == "hardlinked file rejected"


async def test_file_path_fifo_does_not_block_and_is_rejected(server, tmp_path: Path) -> None:
    # A FIFO inside the jail must NOT wedge the open (O_NONBLOCK on the final open) and is rejected as
    # non-regular. wait_for turns a regression (a blocking open) into a clean failure instead of an infinite hang.
    fifo = tmp_path / "pipe.fifo"
    os.mkfifo(fifo)
    env = _envelope(
        await asyncio.wait_for(server._handle_compress({"file_path": str(fifo)}), timeout=15)
    )
    assert env["error"].startswith("Not a file:")


# ─── the file-ingest ceiling ────────────────────────────────────────────────


async def test_file_path_oversized_rejected_with_clear_message(
    server, tmp_path: Path, monkeypatch
) -> None:
    # A file past the ingest ceiling is rejected by stat() BEFORE the body is read (never
    # allocated), and the message names the size, the limit, and the env var that raises it.
    monkeypatch.setenv("FURL_MCP_MAX_FILE_BYTES", "16")
    big = tmp_path / "big.json"
    big.write_text("x" * 64)
    env = _envelope(await server._handle_compress({"file_path": str(big)}))
    assert env["error"].startswith("File too large to compress:")
    assert "64 bytes" in env["error"]
    assert "limit 16 bytes" in env["error"]
    assert "FURL_MCP_MAX_FILE_BYTES" in env["error"]


async def test_file_path_at_ceiling_still_ingests(server, tmp_path: Path, monkeypatch) -> None:
    # Boundary: a file exactly at the ceiling is accepted (the gate is strictly
    # greater-than), so the limit is inclusive of its own value.
    monkeypatch.setenv("FURL_MCP_MAX_FILE_BYTES", "4096")
    f = tmp_path / "exact.txt"
    f.write_bytes(b"a" * 4096)
    env = _envelope(await server._handle_compress({"file_path": str(f)}))
    assert "error" not in env, env
