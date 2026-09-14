"""Run only inside the CI-built image, with networking disabled and no real users."""

import asyncio
import json
from pathlib import Path

from furl_remote.auth import Principal
from furl_remote.config import Settings
from furl_remote.workers import TenantWorkers


def body(result):
    assert not result.isError, result
    return json.loads(result.content[0].text)


async def main():
    settings = Settings(
        "https://furl.example.test",
        "https://issuer.example.test/",
        "https://issuer.example.test/jwks",
        Path("/var/lib/furl"),
    )
    workers = TenantWorkers(settings)
    workers.start()
    alice = Principal(settings.issuer, "container-smoke-alice", frozenset())
    bob = Principal(settings.issuer, "container-smoke-bob", frozenset())
    try:
        stored = body(
            await workers.call(alice, "furl_compress", {"content": "CI container record\n" * 4000})
        )
        key = stored["hash"]
        assert key
        workers.close()
        workers = TenantWorkers(settings)
        workers.start()
        retrieved = body(await workers.call(alice, "furl_retrieve", {"hash": key}))
        assert "CI container record" in json.dumps(retrieved)
        assert "error" in body(await workers.call(bob, "furl_retrieve", {"hash": key}))
        body(await workers.call(alice, "furl_purge", {"hash": key}))
        assert "error" in body(await workers.call(alice, "furl_retrieve", {"hash": key}))
        print(
            "Container: offline native compression, tokenizer cache, restart, tenant isolation and purge passed"
        )
    finally:
        workers.close()


if __name__ == "__main__":
    asyncio.run(main())
