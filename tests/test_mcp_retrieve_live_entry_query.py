"""Regression test for #18: a non-matching query falsely reported eviction.

_retrieve_content(hash, query) ran store.search; when the query matched nothing
it fell through to the cause-honest miss path, returning
"Entry no longer retrievable ... evicted under capacity pressure" with
status implying the entry was gone — even though the entry was LIVE and simply
had no item matching the query.

Fix: when search returns nothing, check store.retrieve(hash). If the entry is
still present, return a "no match" response (available, count 0) instead of a
false eviction error. A genuinely-missing hash still reports the loud miss.

Compression-neutral (retrieve plane only).
"""

from __future__ import annotations

import json
import types

import pytest

from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store
from furl_ctx.ccr.mcp_server import FurlMCPServer


@pytest.fixture(autouse=True)
def _isolate_store():
    reset_compression_store()
    yield
    reset_compression_store()


def _stub_server(store):
    return types.SimpleNamespace(
        _get_local_store=lambda: store,
        _stats=types.SimpleNamespace(record_retrieval=lambda *a, **k: None),
    )


def _store_live_entry(store, h: str) -> None:
    store.store(
        original=json.dumps([{"id": 0, "v": "needle"}]),
        compressed=f"<<ccr:{h}>>",
        original_item_count=1,
        explicit_hash=h,
    )


def _retrieve(store, h, query):
    # PERF-16 relocated the retrieve branch logic into the synchronous
    # ``_retrieve_content_sync`` core (async ``_retrieve_content`` is now a thin
    # ``asyncio.to_thread`` wrapper). Exercise the core directly — identical
    # branch logic, no event loop needed for this stubbed-store unit test.
    return FurlMCPServer._retrieve_content_sync(_stub_server(store), h, query)


def test_nonmatching_query_on_live_entry_is_not_an_eviction_error() -> None:
    store = get_compression_store(max_entries=10)
    h = "abcdef123456"
    _store_live_entry(store, h)

    result = _retrieve(store, h, "zzz_definitely_no_match_xyz")

    # #18: a live entry with no query match must NOT report an eviction error.
    assert "error" not in result, f"live entry falsely errored: {result}"
    assert result["count"] == 0
    assert result["hash"] == h
    assert "note" in result and "available" in result["note"].lower()


def test_missing_hash_still_reports_loud_miss() -> None:
    # Boundary: a genuinely-absent hash must still report the loud, cause-honest
    # miss (the fix must not suppress real misses).
    store = get_compression_store(max_entries=10)
    result = _retrieve(store, "ffffffffffff", "anything")
    assert "error" in result, "a genuinely-missing entry must still error loudly"


def test_matching_query_returns_results() -> None:
    # When search DOES return hits, they are surfaced (no error, results present).
    # Stub the store so this asserts the branch logic deterministically rather
    # than depending on BM25 scoring of a tiny corpus.
    h = "abcdef123456"
    hit = {"id": 0, "v": "needle"}
    store = types.SimpleNamespace(
        search=lambda hk, q, **kw: [hit],
        exists=lambda hk, **kw: True,
        retrieve=lambda hk: object(),
    )
    result = _retrieve(store, h, "needle")
    assert "error" not in result
    assert result["count"] == 1
    assert result["results"] == [hit]


def test_omitted_query_retrieves_full_entry() -> None:
    # Boundary: no query => the full original is returned (the non-search path),
    # unaffected by the #18 search-branch fix.
    store = get_compression_store(max_entries=10)
    h = "abcdef123456"
    _store_live_entry(store, h)
    result = _retrieve(store, h, None)
    assert "error" not in result
    assert "original_content" in result
