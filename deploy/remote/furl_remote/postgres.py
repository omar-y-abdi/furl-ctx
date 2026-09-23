"""Persist the real worker's SQLite state across disposable serverless instances.

A PostgreSQL row lock spans restore, execution and snapshot commit. The child sees
only its private scratch directory, never the database credentials. No successful
result escapes before COMMIT, including results from nominally read-only tools.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

import anyio
import psycopg
from mcp.types import CallToolResult

from .auth import Principal
from .config import Settings
from .workers import TenantWorkers, WorkerRejected

# Matches the existing stdio worker's fixed namespace; no user input is a path.
DB_NAME = "ccr-ns-" + hashlib.sha256(b"furl-remote-v1\0\0").hexdigest()[:16] + ".sqlite3"


def snapshot(path: Path, limit: int) -> bytes:
    """Use SQLite's backup API, not a raw copy that could omit committed WAL pages."""
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as source:
        pages = source.execute("PRAGMA page_count").fetchone()[0]
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
        if pages * page_size > limit:
            raise WorkerRejected("Storage budget reached; purge your entries first")
        with tempfile.NamedTemporaryFile(dir=path.parent) as target_file:
            with closing(sqlite3.connect(target_file.name)) as target:
                source.backup(target)
            data = Path(target_file.name).read_bytes()
    if len(data) > limit:
        raise WorkerRejected("Storage budget reached; purge your entries first")
    return data


class PostgresWorkers(TenantWorkers):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        if not settings.database_url:
            raise ValueError("FURL_REMOTE_DATABASE_URL is required")
        if settings.tenant_max_bytes > 64 * 1024 * 1024:
            raise ValueError("Serverless snapshots are limited to 64 MiB per tenant")
        self.database_url = settings.database_url
        self.started = False

    def start(self) -> None:
        self.settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.settings.data_dir.stat().st_mode & 0o077:
            raise ValueError("The scratch directory must be private (chmod 700)")
        self.started = True

    def close(self) -> None:
        self.started = False

    def prune_idle(self) -> None:
        # Scratch is deleted per request. Expired database rows are pruned on allocation.
        pass

    async def _connect(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            self.database_url,
            autocommit=True,
            connect_timeout=5,
            prepare_threshold=None,
        )

    async def health(self) -> None:
        if not self.started:
            raise WorkerRejected("Service storage is not initialized")
        try:
            with anyio.fail_after(10):
                async with await self._connect() as conn:
                    await conn.execute(
                        "SELECT tenant_key, snapshot, expires_at FROM furl_remote.tenants LIMIT 0"
                    )
                    ready = await (
                        await conn.execute(
                            "SELECT current_setting('transaction_read_only')='off' "
                            "AND has_table_privilege(current_user,'furl_remote.tenants','SELECT') "
                            "AND has_table_privilege(current_user,'furl_remote.tenants','INSERT') "
                            "AND has_table_privilege(current_user,'furl_remote.tenants','UPDATE') "
                            "AND has_table_privilege(current_user,'furl_remote.tenants','DELETE')"
                        )
                    ).fetchone()
                    if ready != (True,):
                        raise WorkerRejected("Durable storage is not writable")
        except (psycopg.Error, TimeoutError):
            raise WorkerRejected("Durable storage is unavailable") from None

    async def _prepare(self, conn: psycopg.AsyncConnection, key: str) -> None:
        if await (
            await conn.execute("SELECT 1 FROM furl_remote.tenants WHERE tenant_key=%s", (key,))
        ).fetchone():
            return
        # Only new tenant allocation uses this short global transaction. Existing
        # users keep their independent row locks while running the worker.
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            await conn.execute("SELECT pg_advisory_xact_lock(2063648590, 1)")
            if await (
                await conn.execute("SELECT 1 FROM furl_remote.tenants WHERE tenant_key=%s", (key,))
            ).fetchone():
                return
            await conn.execute(
                "DELETE FROM furl_remote.tenants WHERE tenant_key IN "
                "(SELECT tenant_key FROM furl_remote.tenants WHERE expires_at <= now() "
                "FOR UPDATE SKIP LOCKED)"
            )
            row = await (await conn.execute("SELECT count(*) FROM furl_remote.tenants")).fetchone()
            if row is None or row[0] >= self.settings.max_tenants:
                raise WorkerRejected("Service capacity reached; contact the operator")
            await conn.execute(
                "INSERT INTO furl_remote.tenants(tenant_key, expires_at) "
                "VALUES (%s, now() + %s * interval '1 second') ON CONFLICT DO NOTHING",
                (key, self.settings.ttl_seconds),
            )

    async def call(
        self, principal: Principal, name: str, arguments: dict[str, Any]
    ) -> CallToolResult:
        if not self.started:
            raise WorkerRejected("Service storage is not initialized")
        if self.slots.locked():
            raise WorkerRejected("Service busy; retry the call")
        async with self.slots:
            with anyio.fail_after(self.settings.call_timeout + 25):
                async with await self._connect() as conn:
                    await self._prepare(conn, principal.tenant_key)
                    async with conn.transaction():
                        await conn.execute("SET LOCAL lock_timeout = '5s'")
                        await conn.execute("SET LOCAL synchronous_commit = on")
                        await conn.execute(
                            "SELECT set_config('idle_in_transaction_session_timeout', %s, true)",
                            (str(int((self.settings.call_timeout + 15) * 1000)),),
                        )
                        row = await (
                            await conn.execute(
                                "SELECT snapshot, expires_at > now() FROM furl_remote.tenants "
                                "WHERE tenant_key=%s FOR UPDATE",
                                (principal.tenant_key,),
                            )
                        ).fetchone()
                        if row is None:
                            raise WorkerRejected("Tenant expired during allocation; retry")
                        with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as temp:
                            workspace = Path(temp)
                            db = workspace / DB_NAME
                            if row[1] and row[0]:
                                if len(row[0]) > self.settings.tenant_max_bytes:
                                    raise WorkerRejected(
                                        "Stored snapshot exceeds the storage budget"
                                    )
                                db.write_bytes(row[0])
                                db.chmod(0o600)
                            result = await self._run(workspace, name, arguments)
                            data = await anyio.to_thread.run_sync(
                                snapshot, db, self.settings.tenant_max_bytes
                            )
                            await conn.execute(
                                "UPDATE furl_remote.tenants SET snapshot=%s, "
                                "expires_at=now() + %s * interval '1 second' WHERE tenant_key=%s",
                                (data, self.settings.ttl_seconds, principal.tenant_key),
                            )
                    # The transaction context has committed; only now is retrieval durable.
                    return result
