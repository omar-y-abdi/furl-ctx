"""Stateless Streamable HTTP transport over authenticated, durable Furl workers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
from mcp.server import Server
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import Receive, Scope, Send

from furl_ctx._version import get_version
from furl_ctx.ccr.mcp_server import CSV_DECODE_LEGEND, FurlMCPServer

from .auth import IssuerUnavailable, OAuthVerifier, Principal, TokenRejected
from .config import Settings
from .workers import TenantWorkers, WorkerRejected

WRITE_TOOLS = frozenset({"furl_compress", "furl_purge"})
PUBLIC_TOOLS = WRITE_TOOLS | {"furl_retrieve", "furl_stats", "furl_search", "furl_list"}


class RemoteApp:
    def __init__(self, settings: Settings, verifier: OAuthVerifier | None = None) -> None:
        self.settings = settings
        self.resource = settings.origin + "/mcp"
        self.metadata_url = settings.origin + "/.well-known/oauth-protected-resource/mcp"
        self.client: httpx.AsyncClient | None = None
        if verifier is None:
            self.client = httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=10)
            self.verifier = OAuthVerifier(
                settings.issuer, settings.jwks_url, self.resource, self.client
            )
        else:
            self.verifier = verifier
        self.workers = TenantWorkers(settings)
        self.tools: list[Tool] = []

    async def _tools(self) -> list[Tool]:
        if not self.tools:
            for original in await FurlMCPServer().route_list_tools():
                if original.name not in PUBLIC_TOOLS:
                    continue
                tool = original.model_copy(deep=True)
                tool.inputSchema["additionalProperties"] = False
                if tool.name == "furl_compress":
                    del tool.inputSchema["properties"]["file_path"]
                    tool.description = "Compress inline content OR a host-provided file attachment. Stored content belongs only to the signed-in user and expires; use the returned hash to retrieve details. Do not pass filesystem paths. Compression may evict older stored entries at capacity."
                    tool.inputSchema["properties"]["content"]["description"] = (
                        "Inline text; use file instead for host-provided attachments. Exactly one input source."
                    )
                    tool.inputSchema["properties"]["file"]["description"] = (
                        "Host-provided attachment transferred out of model context. Do not construct signed URLs manually. Provide file OR content, not both."
                    )
                if tool.name == "furl_stats":
                    tool.description = "Inspect the signed-in user's persisted CCR store. Use the store block for cross-request totals; top-level process counters describe only this short-lived worker."
                scope = (
                    self.settings.write_scope
                    if tool.name in WRITE_TOOLS
                    else self.settings.read_scope
                )
                schemes = [{"type": "oauth2", "scopes": [scope]}]
                tool.meta = {**(tool.meta or {}), "securitySchemes": schemes}
                self.tools.append(
                    Tool.model_validate(
                        {**tool.model_dump(by_alias=True), "securitySchemes": schemes}
                    )
                )
        return self.tools

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        self.workers.start()

        async def prune() -> None:
            while True:
                await asyncio.sleep(300)
                self.workers.prune_idle()

        task = asyncio.create_task(prune())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.workers.close()
            if self.client:
                await self.client.aclose()

    def _challenge(self, scope: str, error: str | None = None) -> str:
        text = f'Bearer resource_metadata="{self.metadata_url}", scope="{scope}"'
        if error:
            text += f', error="{error}"'
        return text

    async def _reply(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        status: int,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        await JSONResponse(
            body, status_code=status, headers={"Cache-Control": "no-store", **(headers or {})}
        )(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":

            @asynccontextmanager
            async def lifecycle(_app: Any) -> AsyncIterator[None]:
                async with self.lifespan():
                    yield

            await Starlette(lifespan=lifecycle)(scope, receive, send)
            return
        request = Request(scope, receive)
        if (
            len(request.headers.getlist("host")) != 1
            or request.headers.get("host", "").lower()
            != urlsplit(self.settings.origin).netloc.lower()
        ):
            await self._reply(scope, receive, send, 421, {"error": "Unrecognized host"})
            return
        origin = request.headers.get("origin")
        if len(request.headers.getlist("origin")) > 1 or (
            origin is not None and origin != self.settings.origin
        ):
            await self._reply(scope, receive, send, 403, {"error": "Origin not allowed"})
            return
        if request.url.query:
            await self._reply(
                scope, receive, send, 400, {"error": "Query parameters are not accepted"}
            )
            return
        path = scope["path"]
        if (
            path
            in (
                "/.well-known/oauth-protected-resource",
                "/.well-known/oauth-protected-resource/mcp",
            )
            and request.method == "GET"
        ):
            await self._reply(
                scope,
                receive,
                send,
                200,
                {
                    "resource": self.resource,
                    "authorization_servers": [self.settings.issuer],
                    "scopes_supported": [self.settings.read_scope, self.settings.write_scope],
                    "bearer_methods_supported": ["header"],
                    "resource_name": "Furl",
                },
            )
            return
        if (
            path == "/.well-known/openai-apps-challenge"
            and request.method == "GET"
            and self.settings.challenge_token is not None
        ):
            await PlainTextResponse(
                self.settings.challenge_token, headers={"Cache-Control": "no-store"}
            )(scope, receive, send)
            return
        if path in ("/healthz", "/readyz") and request.method == "GET":
            try:
                if self.workers.volume_lock is None:
                    raise IssuerUnavailable("Storage not initialized")
                if path == "/readyz":
                    await self.verifier.ready()
            except IssuerUnavailable:
                await self._reply(scope, receive, send, 503, {"ready": False})
            else:
                await self._reply(scope, receive, send, 200, {"ready": True})
            return
        if path != "/mcp":
            await self._reply(scope, receive, send, 404, {"error": "Not found"})
            return
        authorization = request.headers.getlist("authorization")
        try:
            if len(authorization) != 1:
                raise TokenRejected("A bearer token is required")
            scheme, _, token = authorization[0].partition(" ")
            if scheme.lower() != "bearer":
                raise TokenRejected("A bearer token is required")
            principal = await self.verifier.verify(token)
        except TokenRejected:
            await self._reply(
                scope,
                receive,
                send,
                401,
                {"error": "Invalid or missing access token"},
                {"WWW-Authenticate": self._challenge(self.settings.read_scope, "invalid_token")},
            )
            return
        except IssuerUnavailable:
            await self._reply(
                scope,
                receive,
                send,
                503,
                {"error": "Authentication service unavailable"},
                {"Retry-After": "10"},
            )
            return
        if not principal.scopes.intersection({self.settings.read_scope, self.settings.write_scope}):
            await self._reply(
                scope,
                receive,
                send,
                403,
                {"error": "Insufficient scope"},
                {
                    "WWW-Authenticate": self._challenge(
                        self.settings.read_scope, "insufficient_scope"
                    )
                },
            )
            return
        if request.method == "POST":
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip()
                != "application/json"
            ):
                await self._reply(scope, receive, send, 415, {"error": "Use application/json"})
                return
            raw = bytearray()
            try:
                with anyio.fail_after(10):
                    async for chunk in request.stream():
                        if len(raw) + len(chunk) > 1024 * 1024:
                            await self._reply(
                                scope,
                                receive,
                                send,
                                413,
                                {"error": "Use an attachment for inputs above 1 MiB"},
                            )
                            return
                        raw.extend(chunk)
            except TimeoutError:
                await self._reply(scope, receive, send, 408, {"error": "Request body timed out"})
                return
            consumed = False
            original_receive = receive

            async def replay() -> Any:
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": bytes(raw), "more_body": False}
                return await original_receive()

            receive = replay
        await self._mcp(principal, scope, receive, send)

    async def _mcp(self, principal: Principal, scope: Scope, receive: Receive, send: Send) -> None:
        server: Server = Server("furl", version=get_version(), instructions=CSV_DECODE_LEGEND)
        tools = await self._tools()

        @server.list_tools()
        async def list_tools() -> list[Tool]:
            return tools

        @server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
            required = (
                self.settings.write_scope if name in WRITE_TOOLS else self.settings.read_scope
            )
            if required not in principal.scopes:
                return CallToolResult(
                    isError=True,
                    content=[
                        TextContent(
                            type="text",
                            text="Reconnect with the required permission to use this tool.",
                        )
                    ],
                    _meta={
                        "mcp/www_authenticate": [self._challenge(required, "insufficient_scope")]
                    },
                )
            if name not in PUBLIC_TOOLS:
                return CallToolResult(
                    isError=True, content=[TextContent(type="text", text="Unknown tool")]
                )
            try:
                return await self.workers.call(principal, name, arguments)
            except WorkerRejected as exc:
                message = str(exc)
            except Exception:
                # Worker, SDK and transport exceptions may contain arguments or signed URLs.
                message = "Tool execution failed or timed out; retry or use a smaller input. A write may already have completed; inspect your entries before retrying."
            return CallToolResult(isError=True, content=[TextContent(type="text", text=message)])

        transport = StreamableHTTPServerTransport(
            mcp_session_id=None,
            is_json_response_enabled=True,
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[urlsplit(self.settings.origin).netloc],
                allowed_origins=[self.settings.origin],
            ),
        )

        async def run(*, task_status: Any = anyio.TASK_STATUS_IGNORED) -> None:
            async with transport.connect() as (read, write):
                task_status.started()
                await server.run(
                    read, write, server.create_initialization_options(), stateless=True
                )

        async def no_cache(message: Any) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ]
            await send(message)

        async with anyio.create_task_group() as group:
            await group.start(run)
            try:
                await transport.handle_request(scope, receive, no_cache)
            finally:
                with anyio.CancelScope(shield=True):
                    await transport.terminate()
