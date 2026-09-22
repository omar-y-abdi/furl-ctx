"""Serialize each tenant's real stdio calls against a private persistent workspace."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import shutil
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TextIO

import mcp
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

import furl_ctx

from .auth import Principal
from .config import Settings


class WorkerRejected(RuntimeError):
    pass


class TenantWorkers:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.data_dir / "tenants"
        self.slots = asyncio.Semaphore(settings.max_workers)
        self.locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self.volume_lock: TextIO | None = None

    def start(self) -> None:
        self.settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.settings.data_dir.stat().st_mode & 0o077:
            raise ValueError("The data directory must be private (chmod 700)")
        lock_path = self.settings.data_dir / ".gateway.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.volume_lock = os.fdopen(fd, "r+")
        try:
            fcntl.flock(self.volume_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.volume_lock.close()
            self.volume_lock = None
            raise ValueError("Run exactly one gateway process per data volume") from None
        if self.root.is_symlink():
            self.close()
            raise ValueError("Tenant root must not be a symbolic link")
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.prune_idle()

    def close(self) -> None:
        if self.volume_lock:
            self.volume_lock.close()
            self.volume_lock = None

    def prune_idle(self) -> None:
        now = time.time()
        for path in self.root.iterdir():
            if not re.fullmatch(r"[a-f0-9]{64}", path.name) or path.name in self.locks:
                continue
            if path.is_symlink() or not path.is_dir():
                continue
            marker = path / ".last-used"
            touched = marker.stat().st_mtime if marker.exists() else path.stat().st_mtime
            if now - touched > self.settings.ttl_seconds:
                shutil.rmtree(path)

    def _workspace(self, key: str) -> Path:
        path = self.root / key
        if path.is_symlink():
            raise WorkerRejected("Unsafe tenant workspace")
        if not path.exists():
            self.prune_idle()
            if sum(1 for p in self.root.iterdir() if p.is_dir()) >= self.settings.max_tenants:
                raise WorkerRejected("Service capacity reached; contact the operator")
            path.mkdir(mode=0o700)
        (path / ".last-used").touch(mode=0o600)
        return path

    def environment(self, workspace: Path) -> dict[str, str]:
        env = {
            k: os.environ[k]
            for k in ("PATH", "LD_LIBRARY_PATH", "LANG", "LC_ALL", "TIKTOKEN_CACHE_DIR")
            if k in os.environ
        }
        env.update(
            {
                "HOME": str(workspace),
                # Vercel's _vendor is injected into the parent sys.path, not the
                # interpreter's site-packages. Use trusted package locations;
                # never forward an arbitrary caller-supplied PYTHONPATH.
                "PYTHONPATH": os.pathsep.join(
                    dict.fromkeys(
                        (
                            str(Path(__file__).resolve().parents[1]),
                            str(Path(mcp.__file__).resolve().parents[1]),
                            str(Path(furl_ctx.__file__).resolve().parents[1]),
                        )
                    )
                ),
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "FURL_WORKSPACE_DIR": str(workspace),
                "FURL_CCR_PROJECT_DIR": str(workspace),
                "FURL_CCR_NAMESPACE": "furl-remote-v1",
                "FURL_CCR_BACKEND": "sqlite",
                "FURL_CCR_SPILL": "0",
                "FURL_MCP_READ": "0",
                "FURL_CCR_TTL_SECONDS": str(self.settings.ttl_seconds),
                "FURL_MCP_ALLOWED_FILE_HOSTS": self.settings.attachment_hosts,
                "FURL_MCP_MAX_FILE_BYTES": str(40 * 1024 * 1024),
            }
        )
        return env

    @asynccontextmanager
    async def _exclusive(self, key: str) -> AsyncIterator[None]:
        lock, refs = self.locks.get(key, (asyncio.Lock(), 0))
        self.locks[key] = (lock, refs + 1)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=5)
            try:
                yield
            finally:
                lock.release()
        finally:
            current, refs = self.locks[key]
            if refs == 1:
                del self.locks[key]
            else:
                self.locks[key] = (current, refs - 1)

    async def call(
        self, principal: Principal, name: str, arguments: dict[str, Any]
    ) -> CallToolResult:
        if self.volume_lock is None:
            raise WorkerRejected("Service storage is not initialized")
        # Fail fast instead of accumulating a process queue under load.
        if self.slots.locked():
            raise WorkerRejected("Service busy; retry the call")
        await self.slots.acquire()
        try:
            async with self._exclusive(principal.tenant_key):
                workspace = self._workspace(principal.tenant_key)
                if name == "furl_compress":
                    size = sum(p.stat().st_size for p in workspace.rglob("*") if p.is_file())
                    if size >= self.settings.tenant_max_bytes:
                        raise WorkerRejected("Storage budget reached; purge your entries first")
                return await self._run(workspace, name, arguments)
        finally:
            self.slots.release()

    async def health(self) -> None:
        if self.volume_lock is None:
            raise WorkerRejected("Service storage is not initialized")

    async def _run(self, workspace: Path, name: str, arguments: dict[str, Any]) -> CallToolResult:
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "furl_remote.worker"],
            cwd=str(workspace),
            env=self.environment(workspace),
        )
        # Keep AnyIO cancel scopes and stdio cleanup in one task on timeout/disconnect.
        import anyio

        with anyio.fail_after(self.settings.call_timeout):
            with open(os.devnull, "w") as errors:
                async with stdio_client(server, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(name, arguments)
        if name == "furl_compress":
            for item in result.content:
                if isinstance(item, TextContent):
                    try:
                        envelope = json.loads(item.text)
                    except ValueError:
                        continue
                    if isinstance(envelope, dict) and envelope.get("durably_stored") is False:
                        raise WorkerRejected(
                            "Storage did not persist the result. Keep the original; no durable retrieval result is confirmed. Inspect your entries before retrying."
                        )
        if len(result.model_dump_json().encode()) > 1024 * 1024:
            raise WorkerRejected("Result exceeds 1 MiB; retrieve a narrower slice")
        return result
