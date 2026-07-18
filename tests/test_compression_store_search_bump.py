"""COR-37(a) — search() bumped retrieval_count for ZERO-result queries.

``_get_entry_for_search`` recorded the access (retrieval_count += 1,
last_accessed, search_queries) BEFORE any results were known, so a probe that
matched nothing still counted as a retrieval — contradicting the MCP retrieve
path's documented rationale (``mcp_server._retrieve_content`` uses the
side-effect-free ``exists()`` for its no-match branch precisely so retrieval
metrics reflect ACTUAL retrievals).

Fix: the bump moves to after scoring and fires only when the search returned
items. The retrieval-EVENT log keeps recording zero-result probes
(items_retrieved=0) — that plane records honest observations, not retrievals
(pinned here and by test_ccr.py::test_retrieval_events_logged).
"""

from __future__ import annotations

import json

from furl_ctx.cache.compression_store import CompressionEntry, CompressionStore

ITEMS = [
    {"id": 1, "content": "Python programming language"},
    {"id": 2, "content": "JavaScript web development"},
]


def _seeded_store() -> tuple[CompressionStore, str]:
    store = CompressionStore(max_entries=10)
    hash_key = store.store(original=json.dumps(ITEMS), compressed="[]")
    return store, hash_key


def _entry_snapshot(store: CompressionStore, hash_key: str) -> CompressionEntry | None:
    # Read through the backend directly: retrieve() is itself an access
    # (bumps the counter), which would contaminate the assertion.
    return store._backend.get(hash_key)


def test_zero_result_search_does_not_bump_retrieval_count() -> None:
    store, hash_key = _seeded_store()

    results = store.search(hash_key, "xylophone zeppelin quasar")

    assert results == []
    entry = _entry_snapshot(store, hash_key)
    assert entry is not None
    assert entry.retrieval_count == 0
    assert entry.search_queries == []
    assert entry.last_accessed is None


def test_hit_search_bumps_retrieval_count_once() -> None:
    store, hash_key = _seeded_store()

    results = store.search(hash_key, "Python programming")

    assert results, "sanity: the query must actually match an item"
    entry = _entry_snapshot(store, hash_key)
    assert entry is not None
    assert entry.retrieval_count == 1
    assert "Python programming" in entry.search_queries
    assert entry.last_accessed is not None


def test_zero_result_search_still_logs_probe_event() -> None:
    # The event plane keeps recording zero-result probes — moving the bump
    # must not silence the observation log (items_retrieved stays honest at 0).
    store, hash_key = _seeded_store()

    store.search(hash_key, "xylophone zeppelin quasar")

    events = store._retrieval_events
    assert any(e.retrieval_type == "search" and e.items_retrieved == 0 for e in events)


def test_full_retrieve_still_bumps() -> None:
    # retrieve() hands back the entry — an ACTUAL retrieval — and keeps
    # bumping unconditionally on a hit.
    store, hash_key = _seeded_store()

    entry = store.retrieve(hash_key)

    assert entry is not None
    snapshot = _entry_snapshot(store, hash_key)
    assert snapshot is not None
    assert snapshot.retrieval_count == 1
