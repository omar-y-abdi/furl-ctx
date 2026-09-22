"""Real PostgreSQL + real stdio workers: no mocked durability or MCP responses."""

import asyncio
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from furl_remote.auth import Principal
from furl_remote.workers import WorkerRejected
from test_remote import payload, rpc, setup
from test_remote import signing as signing_fixture

signing = signing_fixture

DSN = os.environ.get("FURL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="Set FURL_TEST_DATABASE_URL to a disposable PostgreSQL database"
)


@pytest.fixture(autouse=True)
async def database():
    import psycopg

    sql = Path(__file__).resolve().parents[1] / "migrations" / "001_postgres.sql"
    assert sql.is_file(), "The durable serverless store must be deployable"
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute(sql.read_text())
        await conn.execute("TRUNCATE furl_remote.tenants")
        yield conn
        await conn.execute("TRUNCATE furl_remote.tenants")


def worker(tmp_path, signing):
    from furl_remote.postgres import PostgresWorkers

    app, keys = setup(tmp_path, signing)
    settings = replace(
        app.settings, database_url=DSN, max_workers=1, tenant_max_bytes=64 * 1024 * 1024
    )
    app.workers = PostgresWorkers(settings)
    return app, keys


async def call(client, token, name, **arguments):
    return payload(await rpc(client, token, "tools/call", {"name": name, "arguments": arguments}))


def original(tag):
    return "\n".join(f"2026-09-22 INFO {tag}-{i:04d} status=OK latency=10ms" for i in range(1100))


@pytest.mark.asyncio
async def test_actual_mcp_survives_cold_instance_and_never_crosses_tenants(tmp_path, signing):
    first, keys = worker(tmp_path / "first", signing)
    content = original("alice-private")
    async with keys, first.lifespan():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url=first.settings.origin
        ) as c:
            assert (await c.get("/healthz")).status_code == 200
            assert (await c.post("/mcp", json={})).status_code == 401
            stored = await call(c, signing[0](), "furl_compress", content=content)
            assert stored["hash"]
    assert not list((tmp_path / "first").rglob("*.sqlite3")), (
        "No authority may remain in scratch space"
    )
    second, keys = worker(tmp_path / "second", signing)
    async with keys, second.lifespan():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url=second.settings.origin
        ) as c:
            retrieved = await call(c, signing[0](), "furl_retrieve", hash=stored["hash"])
            assert "alice-private" in json.dumps(retrieved), retrieved
            bob = await call(c, signing[0](subject="bob"), "furl_retrieve", hash=stored["hash"])
            assert "error" in bob
            assert stored["hash"] in json.dumps(await call(c, signing[0](), "furl_list"))
            assert "alice-private" in json.dumps(
                await call(c, signing[0](), "furl_search", query="alice-private")
            )
            assert (await call(c, signing[0](), "furl_stats"))["store"]["live_entries"] > 0
            await call(c, signing[0](), "furl_purge", all=True)
    third, keys = worker(tmp_path / "third", signing)
    async with keys, third.lifespan():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=third), base_url=third.settings.origin
        ) as c:
            assert "error" in await call(c, signing[0](), "furl_retrieve", hash=stored["hash"])


@pytest.mark.asyncio
async def test_parallel_instances_do_not_lose_same_tenant_updates(tmp_path, signing):
    a, ka = worker(tmp_path / "a", signing)
    b, kb = worker(tmp_path / "b", signing)
    principal = Principal(a.settings.issuer, "alice", frozenset({"furl:read", "furl:write"}))
    async with ka, kb, a.lifespan(), b.lifespan():
        results = await asyncio.gather(
            *(
                app.workers.call(principal, "furl_compress", {"content": original(tag)})
                for app, tag in ((a, "first"), (b, "second"))
            )
        )
        hashes = [json.loads(r.content[0].text)["hash"] for r in results]
        result = await b.workers.call(principal, "furl_list", {})
        assert all(key in result.model_dump_json() for key in hashes)


@pytest.mark.asyncio
async def test_failed_worker_rolls_back_and_cleans_scratch(
    tmp_path, signing, database, monkeypatch
):
    app, keys = worker(tmp_path, signing)
    principal = Principal(app.settings.issuer, "alice", frozenset({"furl:write"}))
    async with keys, app.lifespan():
        await app.workers.call(principal, "furl_compress", {"content": original("committed")})
        before = (
            await (
                await database.execute(
                    "SELECT snapshot FROM furl_remote.tenants WHERE tenant_key=%s",
                    (principal.tenant_key,),
                )
            ).fetchone()
        )[0]

        async def crash(workspace, name, arguments):
            (workspace / "partial.sqlite3").write_bytes(b"must not replace committed state")
            raise RuntimeError("worker interrupted")

        monkeypatch.setattr(app.workers, "_run", crash)
        with pytest.raises(RuntimeError, match="interrupted"):
            await app.workers.call(principal, "furl_compress", {"content": original("partial")})
        after = (
            await (
                await database.execute(
                    "SELECT snapshot FROM furl_remote.tenants WHERE tenant_key=%s",
                    (principal.tenant_key,),
                )
            ).fetchone()
        )[0]
        assert after == before
        assert not list(app.settings.data_dir.rglob("*.sqlite3"))


@pytest.mark.asyncio
async def test_expiry_is_enforced_without_warm_instance(tmp_path, signing, database):
    app, keys = worker(tmp_path, signing)
    principal = Principal(app.settings.issuer, "alice", frozenset({"furl:write"}))
    async with keys, app.lifespan():
        stored = await app.workers.call(
            principal, "furl_compress", {"content": original("expired")}
        )
        key = json.loads(stored.content[0].text)["hash"]
        await database.execute(
            "UPDATE furl_remote.tenants SET expires_at=now()-interval '1 second'"
        )
        result = await app.workers.call(principal, "furl_retrieve", {"hash": key})
        assert "error" in json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_health_checks_database_and_worker_cannot_see_credentials(
    tmp_path, signing, database, monkeypatch
):
    app, keys = worker(tmp_path, signing)
    monkeypatch.setenv("FURL_REMOTE_DATABASE_URL", DSN)
    monkeypatch.setenv("PGPASSWORD", "must-not-reach-child")
    env = app.workers.environment(tmp_path)
    assert "FURL_REMOTE_DATABASE_URL" not in env and "PGPASSWORD" not in env
    async with keys, app.lifespan():
        await database.execute("ALTER TABLE furl_remote.tenants RENAME TO unavailable")
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=app.settings.origin
            ) as c:
                assert (await c.get("/healthz")).status_code == 503
        finally:
            await database.execute("ALTER TABLE furl_remote.unavailable RENAME TO tenants")


def test_snapshot_includes_wal_and_rejects_oversize(tmp_path):
    from furl_remote.postgres import snapshot

    db = tmp_path / "store.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE proof(value TEXT)")
    conn.execute("INSERT INTO proof VALUES ('only-in-wal')")
    conn.commit()
    data = snapshot(db, 1024 * 1024)
    restored = tmp_path / "restored.sqlite3"
    restored.write_bytes(data)
    with sqlite3.connect(restored) as other:
        assert other.execute("SELECT value FROM proof").fetchone()[0] == "only-in-wal"
    with pytest.raises(WorkerRejected, match="budget"):
        snapshot(db, 100)
    conn.close()


@pytest.mark.asyncio
async def test_commit_failure_never_returns_a_retrieval_receipt(tmp_path, signing, database):
    app, keys = worker(tmp_path, signing)
    async with keys, app.lifespan():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=app.settings.origin
        ) as c:
            old = await call(c, signing[0](), "furl_compress", content=original("committed"))
            before = (
                await (
                    await database.execute("SELECT snapshot FROM furl_remote.tenants")
                ).fetchone()
            )[0]
            await database.execute("""
                CREATE FUNCTION furl_remote.reject_commit() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'internal-database-secret'; END $$;
                CREATE CONSTRAINT TRIGGER reject_commit AFTER UPDATE ON furl_remote.tenants
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION furl_remote.reject_commit();
            """)
            try:
                result = await rpc(
                    c,
                    signing[0](),
                    "tools/call",
                    {"name": "furl_compress", "arguments": {"content": original("uncommitted")}},
                )
                assert result.json()["result"]["isError"]
                assert '"hash"' not in result.text
                assert "internal-database-secret" not in result.text
                after = (
                    await (
                        await database.execute("SELECT snapshot FROM furl_remote.tenants")
                    ).fetchone()
                )[0]
                assert after == before
            finally:
                await database.execute(
                    "DROP TRIGGER reject_commit ON furl_remote.tenants; DROP FUNCTION furl_remote.reject_commit()"
                )
            assert "committed" in json.dumps(
                await call(c, signing[0](), "furl_retrieve", hash=old["hash"])
            )


@pytest.mark.asyncio
async def test_concurrent_first_calls_respect_tenant_capacity(tmp_path, signing):
    a, ka = worker(tmp_path / "a", signing)
    b, kb = worker(tmp_path / "b", signing)
    a.workers.settings = replace(a.workers.settings, max_tenants=1)
    b.workers.settings = replace(b.workers.settings, max_tenants=1)
    alice = Principal(a.settings.issuer, "alice", frozenset())
    async with ka, kb, a.lifespan(), b.lifespan():
        await asyncio.gather(
            a.workers.call(alice, "furl_list", {}), b.workers.call(alice, "furl_list", {})
        )
        bob = Principal(a.settings.issuer, "bob", frozenset())
        with pytest.raises(WorkerRejected, match="capacity"):
            await b.workers.call(bob, "furl_list", {})


@pytest.mark.asyncio
async def test_corrupt_snapshot_fails_closed_without_overwrite(tmp_path, signing, database):
    app, keys = worker(tmp_path, signing)
    alice = Principal(app.settings.issuer, "alice", frozenset())
    async with keys, app.lifespan():
        await database.execute(
            "INSERT INTO furl_remote.tenants VALUES(%s,%s,now()+interval '1 day')",
            (alice.tenant_key, b"corrupt-db"),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=app.settings.origin
        ) as c:
            response = await rpc(
                c, signing[0](), "tools/call", {"name": "furl_list", "arguments": {}}
            )
            assert response.json()["result"]["isError"]
        stored = (
            await (
                await database.execute(
                    "SELECT snapshot FROM furl_remote.tenants WHERE tenant_key=%s",
                    (alice.tenant_key,),
                )
            ).fetchone()
        )[0]
        assert stored == b"corrupt-db"
        assert not list(app.settings.data_dir.rglob("*.sqlite3"))


@pytest.mark.asyncio
async def test_cancelled_worker_releases_database_lock(tmp_path, signing, monkeypatch):
    import anyio

    app, keys = worker(tmp_path, signing)
    alice = Principal(app.settings.issuer, "alice", frozenset())
    async with keys, app.lifespan():
        run = app.workers._run
        entered = asyncio.Event()

        async def blocked(workspace, name, arguments):
            entered.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(app.workers, "_run", blocked)
        with anyio.move_on_after(0.2) as scope:
            await app.workers.call(alice, "furl_list", {})
        assert scope.cancel_called and entered.is_set()
        monkeypatch.setattr(app.workers, "_run", run)
        with anyio.fail_after(5):
            result = await app.workers.call(alice, "furl_list", {})
        assert not result.isError
        assert not list(app.settings.data_dir.rglob("*.sqlite3"))


@pytest.mark.asyncio
@pytest.mark.slow
async def test_33_mib_staged_attachment_survives_cold_retrieval(tmp_path, signing, monkeypatch):
    """Exercise native file compression/storage, not the external host's URL handoff."""
    first, keys = worker(tmp_path / "first", signing)
    run = first.workers._run
    size = 33 * 1024 * 1024
    line = b"2026-09-22 INFO large-private-record status=OK latency=10ms\n"

    async def staged(workspace, name, arguments):
        if name == "furl_compress":
            path = workspace / "attachment.log"
            path.write_bytes((line * ((size + len(line) - 1) // len(line)))[:size])
            arguments = {"file_path": str(path)}
        return await run(workspace, name, arguments)

    monkeypatch.setattr(first.workers, "_run", staged)
    principal = Principal(first.settings.issuer, "large-fixture", frozenset())
    async with keys, first.lifespan():
        stored = await first.workers.call(principal, "furl_compress", {})
        assert not stored.isError
        key = json.loads(stored.content[0].text)["hash"]
    second, keys = worker(tmp_path / "second", signing)
    async with keys, second.lifespan():
        result = await second.workers.call(
            principal, "furl_retrieve", {"hash": key, "line_range": [1, 3]}
        )
        assert not result.isError and "large-private-record" in result.model_dump_json()
    assert not list(tmp_path.rglob("*.sqlite3"))


@pytest.mark.asyncio
async def test_over_budget_compression_preserves_prior_committed_snapshot(
    tmp_path, signing, database, monkeypatch
):
    app, keys = worker(tmp_path, signing)
    app.workers.settings = replace(app.workers.settings, tenant_max_bytes=1024 * 1024)
    alice = Principal(app.settings.issuer, "alice", frozenset())
    async with keys, app.lifespan():
        stored = await app.workers.call(alice, "furl_compress", {"content": original("kept")})
        key = json.loads(stored.content[0].text)["hash"]
        before = (
            await (await database.execute("SELECT snapshot FROM furl_remote.tenants")).fetchone()
        )[0]
        run = app.workers._run

        async def staged(workspace, name, arguments):
            path = workspace / "attachment.log"
            line = b"2026-09-22 INFO over-budget status=OK latency=10ms\n"
            path.write_bytes(line * (33 * 1024 * 1024 // len(line)))
            return await run(workspace, name, {"file_path": str(path)})

        monkeypatch.setattr(app.workers, "_run", staged)
        with pytest.raises(WorkerRejected, match="budget"):
            await app.workers.call(alice, "furl_compress", {})
        monkeypatch.setattr(app.workers, "_run", run)
        after = (
            await (await database.execute("SELECT snapshot FROM furl_remote.tenants")).fetchone()
        )[0]
        assert after == before
        retrieved = await app.workers.call(alice, "furl_retrieve", {"hash": key})
        assert not retrieved.isError and "kept" in retrieved.model_dump_json()
