"""Real JWT, ASGI/MCP and subprocess/storage isolation contracts."""

import asyncio
import importlib.util
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://identity.example.test/"
ORIGIN = "https://furl.example.test"


def modules():
    assert importlib.util.find_spec("furl_remote"), "The authenticated HTTP deployment is missing"
    from furl_remote.app import RemoteApp, Settings
    from furl_remote.auth import OAuthVerifier, TokenRejected

    return RemoteApp, Settings, OAuthVerifier, TokenRejected


@pytest.fixture
def signing():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="test-key", alg="RS256", use="sig")

    def token(subject="alice", scopes="furl:read furl:write", key_id="test-key", **changes):
        claims = {
            "iss": ISSUER,
            "sub": subject,
            "aud": ORIGIN + "/mcp",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "scope": scopes,
        }
        claims.update(changes)
        return jwt.encode(claims, private, algorithm="RS256", headers={"kid": key_id})

    return token, jwk


def setup(tmp_path, signing):
    App, Settings, Verifier, _ = modules()

    async def keys(request):
        assert str(request.url) == ISSUER + "jwks"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"keys": [signing[1]]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(keys))
    verifier = Verifier(ISSUER, ISSUER + "jwks", ORIGIN + "/mcp", client)
    settings = Settings(ORIGIN, ISSUER, ISSUER + "jwks", tmp_path / "private-data")
    return App(settings, verifier), client


async def rpc(client, token, method, params=None, ident=1):
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": ident, "method": method}
    if params is not None:
        body["params"] = params
    return await client.post("/mcp", json=body, headers=headers)


def payload(response):
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_auth_discovery_and_live_protocol(tmp_path, signing):
    app, keys = setup(tmp_path, signing)
    async with keys, app.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
            rejected = await c.post("/mcp", json={})
            assert rejected.status_code == 401
            assert "resource_metadata=" in rejected.headers["www-authenticate"]
            meta = await c.get("/.well-known/oauth-protected-resource/mcp")
            assert meta.json()["resource"] == ORIGIN + "/mcp"
            assert meta.json()["authorization_servers"] == [ISSUER]
            assert (await c.get("/.well-known/oauth-authorization-server")).status_code == 404
            result = await rpc(
                c,
                signing[0](),
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "reviewer", "version": "1"},
                },
            )
            assert result.json()["result"]["serverInfo"]["name"] == "furl"
            assert "mcp-session-id" not in result.headers
            tools = (await rpc(c, signing[0](), "tools/list")).json()["result"]["tools"]
            assert len(tools) == 6
            assert (
                "file_path"
                not in next(t for t in tools if t["name"] == "furl_compress")["inputSchema"][
                    "properties"
                ]
            )
            for tool in tools:
                assert all(
                    isinstance(tool["annotations"][k], bool)
                    for k in ("readOnlyHint", "destructiveHint", "openWorldHint")
                )
                assert tool["_meta"]["securitySchemes"][0]["type"] == "oauth2"
            assert not list((tmp_path / "private-data" / "tenants").iterdir()), (
                "Discovery must not create a tenant/store"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"aud": "wrong"},
        {"iss": "https://other.test/"},
        {"exp": 1},
        {"sub": ""},
        {"scope": []},
        {"iat": int(time.time()) + 3600},
    ],
)
async def test_wrong_claims_fail_closed(tmp_path, signing, changes):
    app, keys = setup(tmp_path, signing)
    async with keys, app.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
            assert (await rpc(c, signing[0](**changes), "tools/list")).status_code == 401
            assert not list((tmp_path / "private-data" / "tenants").iterdir())


@pytest.mark.asyncio
async def test_scopes_path_args_and_host_rejected_before_worker(tmp_path, signing):
    app, keys = setup(tmp_path, signing)
    async with keys, app.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
            for args in ({"file_path": "/etc/passwd"}, {"content": "test", "tenant": "bob"}):
                r = await rpc(
                    c, signing[0](), "tools/call", {"name": "furl_compress", "arguments": args}
                )
                assert r.json()["result"]["isError"]
            r = await rpc(
                c,
                signing[0](scopes="furl:read"),
                "tools/call",
                {"name": "furl_purge", "arguments": {"all": True}},
            )
            assert r.json()["result"]["isError"]
            assert "mcp/www_authenticate" in r.json()["result"]["_meta"]
            assert (await c.post("/mcp", headers={"host": "evil.test"})).status_code == 421
            assert (
                await c.post("/mcp", headers={"origin": "https://evil.test"})
            ).status_code == 403
            assert (await c.post("/mcp?access_token=" + signing[0]())).status_code == 400
            assert not list((tmp_path / "private-data" / "tenants").iterdir())


@pytest.mark.asyncio
async def test_bounded_http_body(tmp_path, signing):
    app, keys = setup(tmp_path, signing)
    async with keys, app.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:

            async def chunks():
                for _ in range(17):
                    yield b" " * 65536

            r = await c.post(
                "/mcp",
                content=chunks(),
                headers={
                    "Authorization": "Bearer " + signing[0](),
                    "Content-Type": "application/json",
                },
            )
            assert r.status_code == 413


@pytest.mark.asyncio
async def test_real_workers_isolate_all_store_operations_and_survive_restart(
    tmp_path, signing, monkeypatch
):
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "must-not-reach-worker")
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path / "forbidden-parent-store"))
    app, keys = setup(tmp_path, signing)
    original = "\n".join(
        f"2026-09-14 INFO alice-private-record-{i:04d} status=OK latency=10ms" for i in range(1100)
    )
    async with keys, app.lifespan():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, timeout=120
        ) as c:
            stored = payload(
                await rpc(
                    c,
                    signing[0](),
                    "tools/call",
                    {"name": "furl_compress", "arguments": {"content": original}},
                )
            )
            hash_key = stored["hash"]
            assert hash_key
            listed = payload(
                await rpc(c, signing[0](), "tools/call", {"name": "furl_list", "arguments": {}})
            )
            assert hash_key in json.dumps(listed)
            assert "alice-private" in json.dumps(
                payload(
                    await rpc(
                        c,
                        signing[0](),
                        "tools/call",
                        {"name": "furl_search", "arguments": {"query": "alice-private"}},
                    )
                )
            )
            stats = payload(
                await rpc(c, signing[0](), "tools/call", {"name": "furl_stats", "arguments": {}})
            )
            assert stats["store"]["live_entries"] > 0
            for name, args in [
                ("furl_retrieve", {"hash": hash_key}),
                ("furl_search", {"query": "alice-private"}),
                ("furl_list", {}),
                ("furl_stats", {}),
            ]:
                result = payload(
                    await rpc(
                        c,
                        signing[0](subject="bob"),
                        "tools/call",
                        {"name": name, "arguments": args},
                    )
                )
                assert "alice-private" not in json.dumps(
                    {k: v for k, v in result.items() if k != "query"}
                )
                if name == "furl_retrieve":
                    assert "error" in result
                if name == "furl_stats":
                    assert result["store"]["live_entries"] == 0
            bob_stored = payload(
                await rpc(
                    c,
                    signing[0](subject="bob"),
                    "tools/call",
                    {"name": "furl_compress", "arguments": {"content": original}},
                )
            )
            assert bob_stored["hash"] == hash_key, (
                "Equal content deliberately collides across isolated users"
            )
            payload(
                await rpc(
                    c,
                    signing[0](subject="bob"),
                    "tools/call",
                    {"name": "furl_purge", "arguments": {"all": True}},
                )
            )
            restored = payload(
                await rpc(
                    c,
                    signing[0](),
                    "tools/call",
                    {"name": "furl_retrieve", "arguments": {"hash": hash_key}},
                )
            )
            assert "alice-private" in json.dumps(restored)
    app2, keys2 = setup(tmp_path, signing)
    async with keys2, app2.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url=ORIGIN) as c:
            restored = payload(
                await rpc(
                    c,
                    signing[0](),
                    "tools/call",
                    {"name": "furl_retrieve", "arguments": {"hash": hash_key}},
                )
            )
            assert "alice-private" in json.dumps(restored)
            payload(
                await rpc(
                    c,
                    signing[0](),
                    "tools/call",
                    {"name": "furl_purge", "arguments": {"hash": hash_key}},
                )
            )
            assert "error" in payload(
                await rpc(
                    c,
                    signing[0](),
                    "tools/call",
                    {"name": "furl_retrieve", "arguments": {"hash": hash_key}},
                )
            )
    assert not (tmp_path / "forbidden-parent-store").exists()


@pytest.mark.asyncio
async def test_malformed_numeric_and_signature_tokens_return_401(tmp_path, signing):
    app, keys = setup(tmp_path, signing)
    bad_tokens = [
        signing[0](exp=float("inf")),
        signing[0](iat=float("nan")),
        "not-a-token",
        jwt.encode(
            {
                "iss": ISSUER,
                "aud": ORIGIN + "/mcp",
                "sub": "alice",
                "exp": time.time() + 300,
                "iat": time.time(),
            },
            "deliberately-wrong-test-secret-not-used-in-production",
            algorithm="HS256",
            headers={"kid": "test-key"},
        ),
    ]
    async with keys, app.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
            for token in bad_tokens:
                response = await rpc(c, token, "tools/list")
                assert response.status_code == 401
                assert token not in response.text


@pytest.mark.asyncio
async def test_jwks_failures_bounded_and_invalid_kids_do_not_amplify(tmp_path, signing):
    _, _, Verifier, Rejected = modules()
    calls = []

    async def keys(request):
        calls.append(request)
        return httpx.Response(200, json={"keys": [signing[1]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(keys)) as c:
        verifier = Verifier(ISSUER, ISSUER + "jwks", ORIGIN + "/mcp", c)
        await verifier.verify(signing[0]())
        for _ in range(5):
            bad = signing[0](key_id="unknown")
            with pytest.raises(Rejected):
                await verifier.verify(bad)
        assert len(calls) == 1

    async def unavailable(request):
        return httpx.Response(200, content=b"x" * 65537)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as c:
        verifier = Verifier(ISSUER, ISSUER + "jwks", ORIGIN + "/mcp", c)
        from furl_remote.auth import IssuerUnavailable

        with pytest.raises(IssuerUnavailable):
            await verifier.verify(signing[0]())


@pytest.mark.asyncio
async def test_volume_lock_environment_budget_and_idle_cleanup(tmp_path, signing, monkeypatch):
    import os
    from dataclasses import replace

    app, keys = setup(tmp_path, signing)
    from furl_remote.auth import Principal
    from furl_remote.workers import TenantWorkers, WorkerRejected

    async with keys, app.lifespan():
        second = TenantWorkers(app.settings)
        with pytest.raises(ValueError, match="one gateway"):
            second.start()
        principal = Principal(ISSUER, "../../hostile-subject", frozenset({"furl:write"}))
        path = app.workers._workspace(principal.tenant_key)
        assert path.parent == app.workers.root
        monkeypatch.setenv("CONTROL_PLANE_API_KEY", "secret")
        monkeypatch.setenv("FURL_CCR_SQLITE_PATH", "/wrong/path")
        env = app.workers.environment(path)
        assert "CONTROL_PLANE_API_KEY" not in env and "FURL_CCR_SQLITE_PATH" not in env
        assert env["FURL_WORKSPACE_DIR"] == str(path)
        assert env["FURL_MCP_READ"] == "0"
        old = time.time() - app.settings.ttl_seconds - 1
        os.utime(path / ".last-used", (old, old))
        async with app.workers._exclusive(principal.tenant_key):
            app.workers.prune_idle()
            assert path.exists(), "Cleanup must not race an active tenant"
        app.workers.prune_idle()
        assert not path.exists()
        app.workers.settings = replace(app.settings, max_tenants=1)
        app.workers._workspace(principal.tenant_key)
        with pytest.raises(WorkerRejected, match="capacity"):
            app.workers._workspace("a" * 64)
        app.workers.slots = asyncio.Semaphore(0)
        with pytest.raises(WorkerRejected, match="busy"):
            await asyncio.wait_for(app.workers.call(principal, "furl_list", {}), timeout=0.2)


@pytest.mark.asyncio
async def test_parallel_users_keep_separate_private_workspaces(tmp_path, signing):
    app, keys = setup(tmp_path, signing)
    async with keys, app.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
            results = await asyncio.gather(
                *[
                    rpc(
                        c,
                        signing[0](subject=user),
                        "tools/call",
                        {
                            "name": "furl_compress",
                            "arguments": {
                                "content": "\n".join(
                                    f"{user}-record id={i} result=ok" for i in range(1200)
                                )
                            },
                        },
                    )
                    for user in ("alice", "bob")
                ]
            )
            for result in results:
                assert payload(result)["hash"]
            for user, other in (("alice", "bob"), ("bob", "alice")):
                response = payload(
                    await rpc(
                        c,
                        signing[0](subject=user),
                        "tools/call",
                        {"name": "furl_search", "arguments": {"query": other + "-record"}},
                    )
                )
                assert response["matches"] == []
            assert len(list(app.workers.root.iterdir())) == 2


@pytest.mark.asyncio
async def test_broken_database_never_becomes_an_empty_store_or_volatile_success(tmp_path, signing):
    import hashlib

    from furl_remote.auth import Principal

    app, keys = setup(tmp_path, signing)
    async with keys, app.lifespan():
        workspace = app.workers._workspace(Principal(ISSUER, "alice", frozenset()).tenant_key)
        # A real open failure in the child forces the engine's in-memory fallback.
        digest = hashlib.sha256(b"furl-remote-v1\x00\x00").hexdigest()[:16]
        (workspace / f"ccr-ns-{digest}.sqlite3").mkdir()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
            for name, arguments in [
                ("furl_list", {}),
                ("furl_compress", {"content": "repeat data " * 1500}),
            ]:
                response = await rpc(
                    c, signing[0](), "tools/call", {"name": name, "arguments": arguments}
                )
                assert response.json()["result"]["isError"]
                assert "<<ccr:" not in response.text


@pytest.mark.asyncio
async def test_issuer_is_part_of_tenant_identity_and_paths_are_portable(tmp_path, signing):
    from furl_remote.auth import Principal

    app, keys = setup(tmp_path, signing)
    async with keys, app.lifespan():
        alice = Principal(ISSUER, "alice", frozenset())
        assert alice.tenant_key != Principal("https://other.test/", "alice", frozenset()).tenant_key
        env = app.workers.environment(app.workers._workspace(alice.tenant_key))
        assert env["FURL_CCR_NAMESPACE"] == "furl-remote-v1"


@pytest.mark.asyncio
async def test_domain_challenge_is_operator_supplied_plain_text_only(tmp_path, signing):
    from dataclasses import replace

    app, keys = setup(tmp_path, signing)
    async with keys, app.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
            path = "/.well-known/openai-apps-challenge"
            assert (await c.get(path)).status_code == 404
            app.settings = replace(app.settings, challenge_token="reviewer-test-token")
            result = await c.get(path)
            assert result.status_code == 200
            assert result.text == "reviewer-test-token"
            assert result.headers["content-type"].startswith("text/plain")
            assert (await c.post(path)).status_code == 404
            with pytest.raises(ValueError):
                replace(app.settings, challenge_token="not-a-token\n<script>")
