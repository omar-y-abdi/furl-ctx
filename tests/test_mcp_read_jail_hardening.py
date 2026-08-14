"""SEC-5 — furl_read jail hardening regressions.

Two residuals in the fd-pinned TOCTOU defense (furl_ctx/ccr/mcp_server.py):

1. ``st_nlink > 1`` rejected legitimately hardlinked files with the
   MISLEADING "path outside workspace" message — the path was never outside
   the workspace; the inode is multiply linked. The rejection now carries its
   own honest error string.
2. ``O_NOFOLLOW`` guarded only the FINAL path component: a directory
   component swapped for a symlink between ``resolve()`` and ``open()``
   redirected the read outside the jail. ``_open_jailed`` now walks every
   component from the workspace root with ``dir_fd`` + ``O_NOFOLLOW``
   (``O_DIRECTORY`` for intermediates), so a swapped component fails its own
   open instead of being followed.

The ``_open_jailed`` unit tests below hand the walk an UNRESOLVED path whose
component is a symlink — exactly what the filesystem looks like after a
post-``resolve()`` swap (the handler-level ``resolve()``+``is_relative_to``
pre-check is pinned in test_mcp_server_handlers.py).

Requires the optional ``mcp`` extra, mirroring test_mcp_server_handlers.py.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import mcp.types as mt  # noqa: E402

from furl_ctx.cache.compression_store import reset_compression_store  # noqa: E402
from furl_ctx.ccr import mcp_server  # noqa: E402
from furl_ctx.ccr.mcp_server import FurlMCPServer, _open_jailed  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Jail furl_read (and the shared-stats/sqlite paths) to the per-test
    # sandbox, and reset the process store singleton around every test.
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    reset_compression_store()
    yield
    reset_compression_store()


@pytest.fixture
def server() -> FurlMCPServer:
    return FurlMCPServer()


def _envelope(result: list[mt.TextContent]) -> dict:
    assert len(result) == 1, f"expected one TextContent, got {result!r}"
    return json.loads(result[0].text)


# ─── honest hardlink rejection (SEC-5a) ─────────────────────────────────────


async def test_hardlinked_file_rejected_with_honest_error(server, tmp_path: Path) -> None:
    # An IN-JAIL hardlink to an IN-JAIL file: the old message claimed "path outside workspace", which
    # was simply false — the rejection is about the inode's link count, and the error must say so.
    target = tmp_path / "orig.txt"
    target.write_text("linked twice")
    link = tmp_path / "link.txt"
    os.link(target, link)

    env = _envelope(await server._handle_read({"file_path": str(link)}))

    assert env["error"] == "hardlinked file rejected"


async def test_singly_linked_file_still_reads(server, tmp_path: Path) -> None:
    # Control: an ordinary (nlink == 1) file keeps working end to end.
    f = tmp_path / "plain.txt"
    f.write_text("alpha\nbeta")
    result = await server._handle_read({"file_path": str(f)})
    assert result[0].text == "     1\talpha\n     2\tbeta"


# ─── dir_fd component walk (SEC-5b) ──────────────────────────────────────────


def test_open_jailed_rejects_symlinked_dir_component(tmp_path: Path) -> None:
    # Simulated TOCTOU: the path looks in-jail lexically, but an intermediate directory component is a
    # symlink pointing OUTSIDE the jail (the state a racer creates after resolve() ran on the honest tree).
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("out-of-jail secret")
    root = tmp_path / "jail"
    root.mkdir()
    (root / "swapped").symlink_to(outside)

    with pytest.raises(OSError) as excinfo:
        _open_jailed(root / "swapped" / "secret.txt", root)

    # O_NOFOLLOW|O_DIRECTORY on a symlink: ENOTDIR (Linux documents exactly
    # this; macOS matches). Never a FileNotFoundError — the component exists.
    assert excinfo.value.errno in (errno.ENOTDIR, errno.ELOOP)
    assert not isinstance(excinfo.value, FileNotFoundError)


def test_open_jailed_rejects_final_component_symlink(tmp_path: Path) -> None:
    # The final-component O_NOFOLLOW guard predates SEC-5; pin that the walk
    # preserved it (ELOOP, not a successful open of the target).
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("out-of-jail secret")
    root = tmp_path / "jail"
    root.mkdir()
    (root / "leaf.txt").symlink_to(outside / "secret.txt")

    with pytest.raises(OSError) as excinfo:
        _open_jailed(root / "leaf.txt", root)
    assert excinfo.value.errno == errno.ELOOP


def test_open_jailed_reads_nested_file(tmp_path: Path) -> None:
    # Happy path: a real nested file opens and reads through the pinned fd.
    root = tmp_path / "jail"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "ok.txt").write_bytes(b"hello walk")

    fd = _open_jailed(root / "a" / "b" / "ok.txt", root)
    try:
        assert os.read(fd, 32) == b"hello walk"
    finally:
        os.close(fd)


def test_open_jailed_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    # The handler maps FileNotFoundError to its "File not found" envelope —
    # the walk must keep raising exactly that for a genuinely missing leaf.
    root = tmp_path / "jail"
    (root / "a").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        _open_jailed(root / "a" / "nope.txt", root)


def test_open_jailed_root_itself_returns_directory_fd(tmp_path: Path) -> None:
    # path == root (empty relative parts): the walk hands back the root fd and the caller's S_ISREG gate
    # reports `Not a file`; the end-to-end handler test pins this behavior.
    fd = _open_jailed(tmp_path, tmp_path)
    try:
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


def test_open_jailed_fallback_path_still_reads(tmp_path: Path, monkeypatch) -> None:
    # Platforms without dir_fd support (Windows) fall back to the single direct open of the resolve()d path — the documented-residual route.
    monkeypatch.setattr(mcp_server, "_DIR_FD_WALK_SUPPORTED", False)
    f = tmp_path / "plain.txt"
    f.write_bytes(b"fallback read")
    fd = _open_jailed(f, tmp_path)
    try:
        assert os.read(fd, 32) == b"fallback read"
    finally:
        os.close(fd)
