"""Run one gateway behind a TLS reverse proxy with a persistent mounted data directory."""

import uvicorn

from .app import RemoteApp
from .config import Settings

uvicorn.run(
    RemoteApp(Settings.from_env()),
    host="0.0.0.0",
    port=8000,
    workers=1,
    proxy_headers=False,
    access_log=False,
    limit_concurrency=32,
    backlog=64,
    timeout_keep_alive=5,
)
