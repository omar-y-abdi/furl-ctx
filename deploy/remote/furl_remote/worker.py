"""Resource-limited stdio child; the HTTP bearer token never reaches this process."""

import os
import resource
import runpy

resource.setrlimit(resource.RLIMIT_CPU, (120, 125))
resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024**2, 128 * 1024**2))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.umask(0o077)
# A short-lived worker must never claim a volatile store will survive its exit.
from furl_ctx.cache.compression_store import resolve_ccr_namespace_store  # noqa: E402

store = resolve_ccr_namespace_store()
if store is None:
    raise SystemExit("A persistent namespace is required")
health = store.get_stats()["backend"]
if health.get("backend_type") != "sqlite" or health.get("degraded"):
    raise SystemExit("Persistent storage is unavailable")
runpy.run_module("furl_ctx.ccr.mcp_server", run_name="__main__")
