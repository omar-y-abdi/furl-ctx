"""Store-concurrency-honesty — the MCP response paths tell the truth on fall-open.

When a durable write loses the whole lock-contention retry budget it falls open
to THIS process's volatile tier: the entry IS retrievable right now (same
server), it simply is not durable. The handlers must say exactly that — return
the volatile hash with a precise caveat and name the likely cause — instead of
dropping the hash and implying loss ("no retrieval hash", "not guaranteed").

Pins the corrected semantics of ``_compress_content`` (single unit) and
``_compress_filtered`` (per-run aggregate, previously a ``KeyError`` on veto),
and guards the healthy-store shape.

Requires the optional ``mcp`` extra, mirroring test_mcp_durability_veto.py.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

from furl_ctx.cache.compression_store import (  # noqa: E402
    CompressionStore,
    reset_compression_store,
)
from furl_ctx.ccr.mcp_server import FurlMCPServer  # noqa: E402
from tests._fixtures import make_fail_open_sqlite_backend  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    reset_compression_store()
    yield
    reset_compression_store()


@pytest.fixture
def server() -> FurlMCPServer:
    return FurlMCPServer()


def _fail_open_store(tmp_path) -> CompressionStore:
    # Tiny, fast retry budget: the fail-open fixture never heals, so the veto is
    # reached quickly and deterministically.
    return CompressionStore(
        backend=make_fail_open_sqlite_backend(tmp_path / "veto.sqlite3"),
        enable_feedback=False,
        durable_retry_attempts=1,
        durable_retry_base_backoff_seconds=0.001,
        durable_retry_max_backoff_seconds=0.001,
    )


def _healthy_store(tmp_path) -> CompressionStore:
    from furl_ctx.cache.backends.sqlite import SqliteBackend

    return CompressionStore(backend=SqliteBackend(db_path=tmp_path / "ok.sqlite3"))


# ── _compress_content (single unit) ───────────────────────────────────────────


def test_compress_content_veto_returns_retrievable_volatile_hash(server, tmp_path, monkeypatch):
    store = _fail_open_store(tmp_path)
    monkeypatch.setattr(server, "_get_local_store", lambda: store)

    out = server._compress_content("plain content for the volatile fallback honesty test")

    # The hash is RETURNED and it really resolves right now (same process).
    assert "hash" in out and out["hash"], "veto must surface the volatile retrieval hash"
    entry = store.retrieve(out["hash"])
    assert entry is not None, "the returned hash must resolve in-process this moment"

    assert out["durably_stored"] is False
    note = out["note"]
    lowered = note.lower()
    assert out["hash"] in note, "the note must name the hash the caller can retrieve now"
    assert "retrievable now" in lowered
    assert "restart" in lowered
    assert "other furl processes" in lowered
    assert "another furl mcp server process" in lowered
    assert "LIBRARY.md" in note
    # Must NOT resurrect the old dishonest phrasing.
    assert "not guaranteed" not in lowered
    assert "no retrieval hash" not in lowered
    assert "unrecoverable" not in lowered
    # The compressed form is still returned for the caller to use or discard.
    assert "compressed" in out and out["original_tokens"] > 0


# ── _compress_filtered (per-run aggregate) ────────────────────────────────────


async def test_compress_filtered_veto_is_crash_free_and_flags_volatile(
    server, tmp_path, monkeypatch
):
    store = _fail_open_store(tmp_path)
    monkeypatch.setattr(server, "_get_local_store", lambda: store)

    content = "\n".join(f"DATA row {i} with detail to compress" for i in range(60))
    # include_patterns routes through _compress_filtered (per-run hashes). Before
    # the fix this raised KeyError('hash') on the vetoed run.
    result = await server._handle_compress({"content": content, "include_patterns": ["DATA"]})
    env = json.loads(result[0].text)

    assert env["filtered"] is True
    assert env["hashes"], "filtered veto must still surface per-run hashes (no crash, no drop)"
    assert env["compressed_runs"] == len(env["hashes"])
    assert env["durably_stored"] is False
    assert env["volatile_hashes"], "the volatile runs must be flagged"
    # Every surfaced hash resolves in-process right now.
    for run_hash in env["hashes"]:
        assert store.retrieve(run_hash) is not None
    lowered = env["note"].lower()
    assert "volatile" in lowered and "restart" in lowered
    assert "LIBRARY.md" in env["note"]
    assert "not guaranteed" not in lowered and "unrecoverable" not in lowered


# ── healthy store: happy-path shape unchanged ─────────────────────────────────


def test_compress_content_healthy_shape_unchanged(server, tmp_path, monkeypatch):
    store = _healthy_store(tmp_path)
    monkeypatch.setattr(server, "_get_local_store", lambda: store)

    content = "plain durable control content"
    out = server._compress_content(content)

    assert "hash" in out
    assert "durably_stored" not in out, "healthy success response shape must be unchanged"
    assert "volatile_hashes" not in out
    entry = store.retrieve(out["hash"])
    assert entry is not None and entry.original_content == content
