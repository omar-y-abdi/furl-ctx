"""Invariant A (PERF-16): no MCP handler performs store/file I/O on the event loop.

The store's sqlite backend documents that "the MCP server calls the store from
run_in_executor worker threads". Only furl_compress honored that; furl_retrieve,
the cross-store search path, furl_stats and furl_read did synchronous SQLite/
file I/O directly inside ``async def``. Each handler now delegates its blocking
work to a synchronous core via ``asyncio.to_thread``.

These tests prove it by spying a store method to record the thread it runs on
and asserting that thread is NOT the event-loop thread. Captured inside an
``async def`` test, ``threading.get_ident()`` is the loop thread; ``to_thread``
work lands on a distinct worker thread.
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("mcp")

from furl_ctx.cache.compression_store import reset_compression_store  # noqa: E402
from furl_ctx.ccr.mcp_server import FurlMCPServer  # noqa: E402

_HASH = "a" * 24


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("FURL_CCR_BACKEND", "memory")
    reset_compression_store()
    yield
    reset_compression_store()


@pytest.fixture
def server() -> FurlMCPServer:
    return FurlMCPServer()


def _spy(monkeypatch, target, attr):
    """Wrap ``target.attr`` to record the thread ident of each call."""
    real = getattr(target, attr)
    box: dict = {"ident": None, "called": False}

    def spy(*args, **kwargs):
        box["called"] = True
        box["ident"] = threading.get_ident()
        return real(*args, **kwargs)

    monkeypatch.setattr(target, attr, spy)
    return box


def _assert_off_loop(box: dict, loop_ident: int) -> None:
    assert box["called"], "the spied store method was never called"
    assert box["ident"] is not None
    assert box["ident"] != loop_ident, (
        "store/file I/O ran on the event-loop thread — PERF-16 invariant A violation"
    )


async def test_retrieve_runs_store_off_the_loop(server, monkeypatch) -> None:
    loop_ident = threading.get_ident()
    store = server._get_local_store()
    store.store(original="payload", compressed="c", explicit_hash=_HASH)
    box = _spy(monkeypatch, store, "retrieve")
    await server._handle_retrieve({"hash": _HASH})
    _assert_off_loop(box, loop_ident)


async def test_cross_store_search_runs_off_the_loop(server, monkeypatch) -> None:
    loop_ident = threading.get_ident()
    store = server._get_local_store()
    box = _spy(monkeypatch, store, "search_all")
    await server._handle_retrieve({"query": "anything"})
    _assert_off_loop(box, loop_ident)


async def test_stats_runs_store_off_the_loop(server, monkeypatch) -> None:
    loop_ident = threading.get_ident()
    store = server._get_local_store()  # ensure _local_store is set so get_stats runs
    box = _spy(monkeypatch, store, "get_stats")
    await server._handle_stats()
    _assert_off_loop(box, loop_ident)


async def test_read_runs_file_io_off_the_loop(server, monkeypatch, tmp_path) -> None:
    loop_ident = threading.get_ident()
    f = tmp_path / "readme.txt"
    f.write_text("hello world\n")
    store = server._get_local_store()
    box = _spy(monkeypatch, store, "store")  # fresh read persists via store.store
    await server._handle_read({"file_path": str(f)})
    _assert_off_loop(box, loop_ident)


async def test_purge_runs_store_off_the_loop(server, monkeypatch) -> None:
    loop_ident = threading.get_ident()
    store = server._get_local_store()
    store.store(original="x", compressed="c", explicit_hash=_HASH)
    box = _spy(monkeypatch, store, "delete")
    await server._handle_purge({"hash": _HASH})
    _assert_off_loop(box, loop_ident)


async def test_search_runs_enumeration_off_the_loop(server, monkeypatch) -> None:
    loop_ident = threading.get_ident()
    store = server._get_local_store()
    box = _spy(monkeypatch, store._backend, "items")
    await server._handle_search({"query": "x"})
    _assert_off_loop(box, loop_ident)


async def test_list_runs_enumeration_off_the_loop(server, monkeypatch) -> None:
    loop_ident = threading.get_ident()
    store = server._get_local_store()
    box = _spy(monkeypatch, store._backend, "items")
    await server._handle_list({})
    _assert_off_loop(box, loop_ident)
