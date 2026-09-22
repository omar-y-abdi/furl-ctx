"""Vercel ASGI entrypoint. Missing operator configuration never enables anonymous tools."""

import logging
import os
from pathlib import Path

from furl_remote.app import RemoteApp
from furl_remote.config import Settings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(Path(__file__).with_name("tokenizers")))


async def unavailable(request: Request) -> JSONResponse:
    return JSONResponse(
        {"error": "Service is not configured"},
        status_code=503,
        headers={"Cache-Control": "no-store"},
    )


try:
    settings = Settings.from_env()
    if not settings.database_url:
        raise ValueError("Vercel requires a PostgreSQL store")
    application: ASGIApp = RemoteApp(settings)
except (KeyError, ValueError):
    # Do not log exception text: configuration URLs can contain credentials.
    logging.getLogger(__name__).error(
        "Configure FURL_REMOTE_ORIGIN, FURL_REMOTE_ISSUER, FURL_REMOTE_JWKS_URL and "
        "FURL_REMOTE_DATABASE_URL (PostgreSQL with TLS); see deploy/remote/README.md"
    )
    application = Starlette(
        routes=[
            Route(
                "/{path:path}",
                unavailable,
                methods=["GET", "POST", "DELETE", "OPTIONS", "PUT", "PATCH"],
            )
        ]
    )


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    await application(scope, receive, send)
