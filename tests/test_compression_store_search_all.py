"""Unit tests for ``CompressionStore.search_all`` (NR2-2 feature a).

Cross-store full-text search ranks every LIVE entry against a query with BM25
and returns ``CrossStoreMatch`` records ``(hash, score, preview, tool_name)``.
Covered here at the store level (the MCP wiring is tested in
``tests/test_mcp_cross_store_search.py``):

* ranking — a discriminative-term hit outranks a weak/no-term hit;
* redaction — the preview masks credentials with the SAME rules the retrieval
  log uses, so a cross-store search can never leak a secret a per-hash
  retrieval's log would have masked;
* expiry — an expired-but-unreaped entry never appears in results;
* purity — a search bumps neither retrieval_count nor the event log.
"""

from __future__ import annotations

import json

from furl_ctx.cache.compression_store import (
    CompressionStore,
    CrossStoreMatch,
)

# Split so no verbatim secret literal sits in source (hook-safe; mirrors the
# trick in tests/test_compression_store_redaction.py).
_API_KEY = "sk" + "-" + "abcdefghijklmnopqrstuvwx"


def _store() -> CompressionStore:
    # In-memory backend, generous capacity so nothing evicts mid-test.
    return CompressionStore(max_entries=100)


def test_search_all_ranks_matching_entry_first() -> None:
    store = _store()
    store.store(original="the quick brown fox", compressed="c", explicit_hash="a" * 12)
    store.store(
        original="alpha beta gamma delta needleword epsilon",
        compressed="c",
        explicit_hash="b" * 12,
    )
    store.store(original="unrelated content here", compressed="c", explicit_hash="c" * 12)

    matches = store.search_all("needleword")

    assert matches, "a matching entry must be returned"
    assert isinstance(matches[0], CrossStoreMatch)
    assert matches[0].hash == "b" * 12
    # Only the entry containing the term qualifies (others score 0 → excluded).
    assert [m.hash for m in matches] == ["b" * 12]


def test_search_all_discriminative_term_outranks_common_term() -> None:
    store = _store()
    # "error" is common across the corpus (low IDF); the UUID is unique (high
    # IDF). A query for both must rank the UUID-bearing entry first.
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    store.store(original="error in module one", compressed="c", explicit_hash="a" * 12)
    store.store(original="error in module two", compressed="c", explicit_hash="b" * 12)
    store.store(original=f"error record {uuid} here", compressed="c", explicit_hash="c" * 12)

    matches = store.search_all(f"error {uuid}")

    assert matches[0].hash == "c" * 12, "the unique-term entry should rank first"


def test_search_all_preview_redacts_credentials() -> None:
    store = _store()
    store.store(
        original=f"config needleword token {_API_KEY} trailing",
        compressed="c",
        explicit_hash="a" * 12,
    )

    matches = store.search_all("needleword")

    assert len(matches) == 1
    preview = matches[0].preview
    assert _API_KEY not in preview, f"secret leaked into preview: {preview!r}"
    assert "[REDACTED]" in preview


def test_search_all_excludes_expired_entries() -> None:
    # Inject a controllable clock so an entry can expire without real sleep.
    now = {"t": 1000.0}
    store = CompressionStore(max_entries=100, now_fn=lambda: now["t"])
    store.store(
        original="needleword lives here",
        compressed="c",
        explicit_hash="a" * 12,
        ttl=10,
    )
    # Advance past the TTL: the row is still in the backend (unreaped) but
    # is_expired() is now True, so search_all must skip it.
    now["t"] = 1000.0 + 11

    assert store.search_all("needleword") == []


def test_search_all_blank_query_returns_empty() -> None:
    store = _store()
    store.store(original="content", compressed="c", explicit_hash="a" * 12)
    assert store.search_all("") == []
    assert store.search_all("   ") == []


def test_search_all_is_side_effect_free() -> None:
    store = _store()
    store.store(original="needleword and more text", compressed="c", explicit_hash="a" * 12)

    store.search_all("needleword")

    # A pure read: retrieval_count must stay 0 and no retrieval event logged
    # (mirrors _get_entry_for_search — nothing was actually retrieved).
    entry = store._backend.get("a" * 12)
    assert entry is not None
    assert entry.retrieval_count == 0
    assert store._retrieval_events == []


def test_search_all_respects_max_results() -> None:
    store = _store()
    for i in range(8):
        store.store(
            original=f"needleword entry number {i}",
            compressed="c",
            explicit_hash=f"{i:012d}",
        )

    matches = store.search_all("needleword", max_results=3)
    assert len(matches) == 3


def test_search_all_spans_json_and_text_entries() -> None:
    store = _store()
    store.store(
        original=json.dumps([{"id": 1, "tag": "needleword"}]),
        compressed="c",
        explicit_hash="a" * 12,
    )
    store.store(
        original="plain text with needleword inside",
        compressed="c",
        explicit_hash="b" * 12,
    )

    hashes = {m.hash for m in store.search_all("needleword")}
    assert hashes == {"a" * 12, "b" * 12}


def test_search_all_spans_cross_session_sqlite_entries(tmp_path) -> None:
    """The headline feature-(a) claim: a cross-store search reaches entries a
    DIFFERENT process/session stored, via the durable SQLite backend.

    Two ``CompressionStore`` instances over one shared db file ARE the
    cross-session case (a second SqliteBackend on the same path is exactly what
    a sibling process opens). An entry stored through the first store must be
    found by ``search_all`` on the second — proving the search spans the shared
    file, not just this process's in-memory rows.
    """
    from furl_ctx.cache.backends.sqlite import SqliteBackend

    db_path = tmp_path / "ccr.sqlite3"
    writer = CompressionStore(backend=SqliteBackend(db_path=db_path))
    writer.store(
        original="durable cross session needleword payload",
        compressed="c",
        explicit_hash="a" * 12,
    )

    # A fresh store over the SAME file stands in for another session/process.
    reader = CompressionStore(backend=SqliteBackend(db_path=db_path))
    matches = reader.search_all("needleword")

    assert [m.hash for m in matches] == ["a" * 12]
    assert "needleword" in matches[0].preview
