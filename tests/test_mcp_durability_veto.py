"""Review F2/F3 — the MCP handlers must not advertise retrieval for
non-durable writes.

The MCP server's default backend is the durable SqliteBackend. When a write
falls open to volatile in-process memory (backend degraded, or the write lost
the sqlite lock race):

* F2 ``furl_compress`` emitted ``hash`` + a bare "Use furl_retrieve ... later"
  note for a volatile-only entry — a sub-agent process or a restart then misses,
  which is the exact cross-process durability the sqlite backend exists to
  provide (its own docstring), advertised as if durable. The caller keeps the
  LOSSY compressed form trusting an unqualified promise.
* F3 ``furl_read`` cached the volatile-only hash in ``_file_cache``, so a later
  unchanged-read response advertised ``furl_retrieve(hash=...)`` backed by
  nothing durable. (Minor: the fresh read itself always ships full content.)

Fix: ``require_durable=True`` at both seams. On veto furl_compress now RETURNS
the volatile hash (the entry is retrievable from THIS process right now) with a
precise caveat — retrievable now, but not after a restart and not from other
processes — and names the likely cause (a sibling MCP server); it never implies
total loss when retrieval works this moment (store-concurrency-honesty).
furl_read still serves the full content but skips the cache entry (no
re-retrieve convenience is advertised).

Requires the optional ``mcp`` extra, mirroring test_mcp_server_handlers.py.
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
    # Jail furl_read (and the shared-stats/sqlite paths) to the per-test
    # sandbox, and reset the process store singleton around every test.
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    reset_compression_store()
    yield
    reset_compression_store()


@pytest.fixture
def server() -> FurlMCPServer:
    return FurlMCPServer()


def _with_fail_open_store(server: FurlMCPServer, tmp_path, monkeypatch) -> CompressionStore:
    # Tiny retry budget: the fail-open fixture never heals, so the veto is reached
    # fast and deterministically (the durable-retry backoff would otherwise add a
    # few tenths of a second per test).
    store = CompressionStore(
        backend=make_fail_open_sqlite_backend(tmp_path / "veto.sqlite3"),
        durable_retry_attempts=1,
        durable_retry_base_backoff_seconds=0.001,
        durable_retry_max_backoff_seconds=0.001,
    )
    monkeypatch.setattr(server, "_get_local_store", lambda: store)
    return store


def _with_healthy_store(server: FurlMCPServer, tmp_path, monkeypatch) -> CompressionStore:
    from furl_ctx.cache.backends.sqlite import SqliteBackend

    store = CompressionStore(backend=SqliteBackend(db_path=tmp_path / "ok.sqlite3"))
    monkeypatch.setattr(server, "_get_local_store", lambda: store)
    return store


# ── F2: furl_compress ────────────────────────────────────────────────────────


def test_compress_veto_returns_volatile_hash_and_flags_not_durable(
    server, tmp_path, monkeypatch
) -> None:
    # store-concurrency-honesty REVISES the original F2 fix: dropping the hash was
    # itself dishonest — the entry is in the volatile tier and retrievable RIGHT
    # NOW. The veto now RETURNS that hash with a precise caveat instead of
    # implying total loss.
    store = _with_fail_open_store(server, tmp_path, monkeypatch)

    out = server._compress_content("plain content for the durability veto test")

    assert "hash" in out and out["hash"], "veto must surface the volatile retrieval hash"
    assert store.retrieve(out["hash"]) is not None, "the returned hash resolves in-process now"
    assert out.get("durably_stored") is False, "still flagged NON-durable in plain words"
    note = out["note"]
    lowered = note.lower()
    assert out["hash"] in note, "note must name the hash the caller can retrieve now"
    assert "retrievable now" in lowered and "restart" in lowered, (
        f"note must state the volatile reality precisely: {note!r}"
    )
    assert "another furl mcp server process" in lowered, "note must name the likely cause"
    assert "LIBRARY.md" in note, "note must point at the runbook"
    # Must NOT resurrect the old phrasing that implied loss when retrieval works.
    assert "not guaranteed" not in lowered
    assert "unrecoverable" not in lowered
    # The compressed form is still returned — the caller decides; nothing lost
    # (the caller still holds the original it sent).
    assert "compressed" in out
    assert out["original_tokens"] > 0


def test_compress_healthy_store_still_returns_hash_and_note(server, tmp_path, monkeypatch) -> None:
    store = _with_healthy_store(server, tmp_path, monkeypatch)

    content = "plain content for the durable control test"
    out = server._compress_content(content)

    assert "hash" in out, "healthy durable store must return the retrieval hash"
    assert "durably_stored" not in out, "success response shape is unchanged"
    assert "furl_retrieve" in out["note"] or "hash=" in out["note"]
    entry = store.retrieve(out["hash"])
    assert entry is not None and entry.original_content == content


# ── F3: furl_read ────────────────────────────────────────────────────────────


async def test_read_veto_serves_content_but_skips_cache(server, tmp_path, monkeypatch) -> None:
    _with_fail_open_store(server, tmp_path, monkeypatch)
    f = tmp_path / "sample.txt"
    f.write_text("line one\nline two\n")

    result = await server._handle_read({"file_path": str(f)})

    # Full content still served (numbered) — nothing is lost on a fresh read.
    assert len(result) == 1
    assert "line one" in result[0].text and "line two" in result[0].text
    # But NO cache entry: a later unchanged read must not advertise a
    # furl_retrieve hash whose only backing is volatile process memory.
    assert not server._file_cache, (
        "furl_read cached a volatile-only hash — a later read would advertise "
        "furl_retrieve for an unbacked entry (F3)"
    )


async def test_read_healthy_store_caches_and_advertises_hash(server, tmp_path, monkeypatch) -> None:
    store = _with_healthy_store(server, tmp_path, monkeypatch)
    f = tmp_path / "sample.txt"
    f.write_text("alpha\nbeta\n")

    first = await server._handle_read({"file_path": str(f)})
    assert "alpha" in first[0].text
    assert server._file_cache, "healthy durable store must cache the hash"

    # Second (unchanged) read returns the cached envelope with a resolvable hash.
    second = await server._handle_read({"file_path": str(f)})
    envelope = json.loads(second[0].text)
    assert envelope["status"] == "cached"
    entry = store.retrieve(envelope["hash"])
    assert entry is not None and entry.original_content == "alpha\nbeta\n"
