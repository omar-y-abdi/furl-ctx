"""Adversarial regression for PR #216's transient cascade-verification race."""

from __future__ import annotations

import pytest

from furl_ctx.cache.backends.memory import InMemoryBackend
from furl_ctx.cache.compression_store import CompressionEntry, CompressionStore

PARENT = "d" * 24
CHILD = "c" * 24


class _OneShotVerificationSpill:
    """Readable in preflight, unavailable for exactly the post-delete proof read."""

    durable = False
    max_rows = None

    def __init__(self) -> None:
        self.data: dict[str, CompressionEntry] = {}
        self.reads: dict[str, int] = {}

    def get(self, hash_key: str) -> CompressionEntry | None:
        count = self.reads.get(hash_key, 0) + 1
        self.reads[hash_key] = count
        if hash_key == PARENT and count == 2:
            raise RuntimeError("one-shot post-delete verification outage")
        return self.data.get(hash_key)

    def set(self, hash_key: str, entry: CompressionEntry) -> None:
        self.data[hash_key] = entry

    def delete(self, hash_key: str) -> bool:
        return self.data.pop(hash_key, None) is not None

    def items(self) -> list[tuple[str, CompressionEntry]]:
        return list(self.data.items())

    def created_at_index(self) -> list[tuple[float, str]]:
        return [(entry.created_at, key) for key, entry in self.data.items()]


def test_transient_post_delete_uncertainty_stays_sticky_in_purge_result() -> None:
    """A later healthy read must not rewrite an indeterminate cascade into success."""
    pytest.importorskip("mcp")
    from furl_ctx.ccr.mcp_server import FurlMCPServer

    spill = _OneShotVerificationSpill()
    store = CompressionStore(
        backend=InMemoryBackend(),
        spill=spill,  # type: ignore[arg-type]
        enable_feedback=False,
    )
    store.store("child", "child view", explicit_hash=CHILD)
    store.store("parent", f"parent <<ccr:{CHILD}>>", explicit_hash=PARENT)

    parent = store._backend.get(PARENT)
    assert parent is not None
    assert store._backend.delete(PARENT) is True
    spill.data[PARENT] = parent

    # Setup collision checks consumed spill reads. Start the fault schedule at
    # the destructive operation: read #1 is cascade preflight, read #2 is the
    # post-delete verification that must make the outcome indeterminate.
    spill.reads.clear()

    server = object.__new__(FurlMCPServer)
    server._local_store = store
    _deleted, _nested, survivors, _shared = server._purge_one(PARENT)

    assert PARENT in survivors
    assert store.exists_any_tier(CHILD) is True
