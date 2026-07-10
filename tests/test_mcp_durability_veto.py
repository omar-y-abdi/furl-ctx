"""Review F2/F3 — the MCP handlers must not advertise retrieval for
non-durable writes.

The MCP server's default backend is the durable SqliteBackend. When a write
falls open to volatile in-process memory (backend degraded, or the write lost
the sqlite lock race):

* F2 ``furl_compress`` emitted ``hash`` + a "Use furl_retrieve ... later" note
  for a volatile-only entry — a sub-agent process or a restart then misses,
  which is the exact cross-process durability the sqlite backend exists to
  provide (its own docstring), broken silently. The caller keeps the LOSSY
  compressed form trusting the promise.
* F3 ``furl_read`` cached the volatile-only hash in ``_file_cache``, so a later
  unchanged-read response advertised ``furl_retrieve(hash=...)`` backed by
  nothing durable. (Minor: the fresh read itself always ships full content.)

Fix: ``require_durable=True`` at both seams. furl_compress returns the
compressed form WITHOUT the hash/retrieval note, flagged in plain words;
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
    store = CompressionStore(backend=make_fail_open_sqlite_backend(tmp_path / "veto.sqlite3"))
    monkeypatch.setattr(server, "_get_local_store", lambda: store)
    return store


def _with_healthy_store(server: FurlMCPServer, tmp_path, monkeypatch) -> CompressionStore:
    from furl_ctx.cache.backends.sqlite import SqliteBackend

    store = CompressionStore(backend=SqliteBackend(db_path=tmp_path / "ok.sqlite3"))
    monkeypatch.setattr(server, "_get_local_store", lambda: store)
    return store


# ── F2: furl_compress ────────────────────────────────────────────────────────


def test_compress_veto_drops_hash_and_flags_not_durable(server, tmp_path, monkeypatch) -> None:
    _with_fail_open_store(server, tmp_path, monkeypatch)

    out = server._compress_content("plain content for the durability veto test")

    assert "hash" not in out, (
        "furl_compress advertised a retrieval hash for a volatile-only write (F2)"
    )
    assert out.get("durably_stored") is False, "veto response must carry the plain-words flag"
    assert "not durably stored" in out["note"].lower(), f"note must say so plainly: {out['note']!r}"
    assert "furl_retrieve" not in out["note"], (
        "the veto note must not promise furl_retrieve for an unbacked original"
    )
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
