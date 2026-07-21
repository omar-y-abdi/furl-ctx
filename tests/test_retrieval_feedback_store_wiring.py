"""Wiring pins: CompressionStore retrieval paths feed the feedback loop.

The honest single choke point for retrieval signals is the store's access
bump (Engine P2-13, aligned with COR-37):

* ``CompressionStore.retrieve`` — bumps ``retrieval_count`` on a real hit and
  emits one feedback signal carrying the entry's (tool_name, strategy) shape.
* ``CompressionStore._record_search_access`` — the post-scoring bump that
  ``search()`` runs ONLY when results shipped; it emits the same signal, so a
  zero-result probe never counts (COR-37 alignment).

The MCP server's ``_handle_retrieve``/``_retrieve_content`` funnel through
those two store methods, so model-driven retrievals are captured end-to-end
without a second (double-counting) emission site — pinned here against a real
in-process server.

Engine-INTERNAL verification reads must NOT masquerade as the model asking
for compressed-away content: ``retrieve(..., record_feedback_signal=False)``
is the opt-out used by the CCR-offload round-trip and the CCR-mirror backing
check, both pinned below.
"""

from __future__ import annotations

import json

import pytest

from furl_ctx.cache import retrieval_feedback as retrieval_feedback_module
from furl_ctx.cache.compression_store import (
    CompressionStore,
    reset_compression_store,
)
from furl_ctx.cache.retrieval_feedback import (
    RetrievalFeedback,
    get_retrieval_feedback,
    reset_retrieval_feedback,
    routing_shape_key,
    set_retrieval_feedback,
)

ITEMS = [
    {"id": 1, "content": "Python programming language"},
    {"id": 2, "content": "JavaScript web development"},
]


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _isolated_feedback_and_store():
    reset_retrieval_feedback()
    reset_compression_store()
    set_retrieval_feedback(RetrievalFeedback(clock=FakeClock()))
    yield
    reset_retrieval_feedback()
    reset_compression_store()


def _events_in_window() -> int:
    """Total in-window signal count across every shape.

    ``RetrievalFeedback`` no longer exposes an ``get_stats()`` observability
    method (dead code, zero production consumers); this recomputes the same
    total-in-window figure directly from the aggregator's own window/clock so
    the wiring pins below keep exercising the real wall-clock window, not a
    weaker per-shape stand-in.
    """
    fb = get_retrieval_feedback()
    cutoff = fb._clock() - fb._window_seconds  # noqa: SLF001 - test-only introspection
    with fb._lock:  # noqa: SLF001
        return sum(1 for events in fb._events.values() for ts in events if ts > cutoff)


def _seeded_store() -> tuple[CompressionStore, str]:
    store = CompressionStore(max_entries=10)
    hash_key = store.store(
        original=json.dumps(ITEMS),
        compressed="[]",
        tool_name="websearch",
        compression_strategy="smart_crusher_row_drop",
    )
    return store, hash_key


# ---------------------------------------------------------------------------
# store.retrieve
# ---------------------------------------------------------------------------


def test_store_retrieve_hit_emits_one_signal_with_entry_shape() -> None:
    store, hash_key = _seeded_store()

    entry = store.retrieve(hash_key)

    assert entry is not None
    assert _events_in_window() == 1
    # The signal carries the entry's compression metadata: tool_name plus the
    # strategy mapped to its content-type tag, so the router's
    # (tool, detected-type) lookup finds it.
    hints = get_retrieval_feedback().get_hints(routing_shape_key("websearch", "json_array"))
    assert hints.retrievals_in_window == 1


def test_store_retrieve_miss_emits_nothing() -> None:
    store, _ = _seeded_store()

    assert store.retrieve("deadbeefdeadbeefdeadbeef") is None
    assert _events_in_window() == 0


def test_store_retrieve_expired_entry_emits_nothing() -> None:
    store, hash_key = _seeded_store()
    # Age the entry past its TTL through the backend (retrieve() itself is an
    # access and would contaminate the assertion — same pattern as the COR-37
    # pins in test_compression_store_search_bump.py).
    entry = store._backend.get(hash_key)
    assert entry is not None
    entry.created_at -= entry.ttl + 60
    store._backend.set(hash_key, entry)

    assert store.retrieve(hash_key) is None
    assert _events_in_window() == 0


def test_store_retrieve_signal_opt_out_for_engine_internal_reads() -> None:
    store, hash_key = _seeded_store()

    entry = store.retrieve(hash_key, record_feedback_signal=False)

    assert entry is not None
    assert _events_in_window() == 0
    # The opt-out suppresses only the FEEDBACK signal; the store's own
    # bookkeeping (retrieval_count) keeps its pre-existing semantics.
    snapshot = store._backend.get(hash_key)
    assert snapshot is not None
    assert snapshot.retrieval_count == 1


def test_store_enable_feedback_false_suppresses_signals() -> None:
    store = CompressionStore(max_entries=10, enable_feedback=False)
    hash_key = store.store(
        original=json.dumps(ITEMS),
        compressed="[]",
        tool_name="websearch",
        compression_strategy="smart_crusher_row_drop",
    )

    assert store.retrieve(hash_key) is not None
    assert store.search(hash_key, "Python programming")
    assert _events_in_window() == 0


def test_feedback_failure_never_breaks_retrieve(monkeypatch) -> None:
    store, hash_key = _seeded_store()

    def _boom(**_kwargs) -> None:
        raise RuntimeError("feedback plane exploded")

    monkeypatch.setattr(retrieval_feedback_module, "record_retrieval_signal", _boom)

    entry = store.retrieve(hash_key)

    assert entry is not None  # feedback is advisory; retrieval must survive
    assert _events_in_window() == 0


# ---------------------------------------------------------------------------
# store.search — COR-37 alignment: only result-shipping searches emit
# ---------------------------------------------------------------------------


def test_search_with_results_emits_signal() -> None:
    store, hash_key = _seeded_store()

    results = store.search(hash_key, "Python programming")

    assert results, "sanity: the query must actually match an item"
    assert _events_in_window() == 1


def test_zero_result_search_emits_nothing() -> None:
    store, hash_key = _seeded_store()

    results = store.search(hash_key, "xylophone zeppelin quasar")

    assert results == []
    assert _events_in_window() == 0


# ---------------------------------------------------------------------------
# Engine-internal reads must not poison the loop
# ---------------------------------------------------------------------------


def test_ccr_mirror_backing_check_emits_nothing() -> None:
    # The result-cache HIT path verifies every <<ccr:HASH>> against the store
    # via retrieve(); that is the engine talking to itself, not the model
    # retrieving compressed-away content.
    from furl_ctx.cache.compression_store import get_compression_store
    from furl_ctx.transforms.content_router import ContentRouter

    ccr_hash = "a" * 12
    get_compression_store().store(
        original="dropped rows payload",
        compressed="preview",
        explicit_hash=ccr_hash,
    )
    router = ContentRouter()

    backed = router._ensure_ccr_backed(f"kept rows <<ccr:{ccr_hash}>> tail", context="")

    assert backed is True
    assert _events_in_window() == 0


def test_ccr_offload_round_trip_verify_emits_nothing() -> None:
    # The reversible offload stores the original and immediately retrieves it
    # to verify byte-exact recovery before emitting the marker — an
    # engine-internal read that must not count as a model retrieval.
    from furl_ctx.transforms.content_router import ContentRouter
    from furl_ctx.transforms.router_policy import CompressionStrategy

    router = ContentRouter()
    # One long single line: TextCrusher's segment floor passes it through,
    # nothing compresses it, and at >=4000 chars the CCR offload fires.
    content = " ".join(f"tok{i:05d}" for i in range(900))
    assert len(content) >= 4000

    result = router.compress(content)

    assert result.strategy_used is CompressionStrategy.CCR_OFFLOAD
    assert _events_in_window() == 0


# ---------------------------------------------------------------------------
# MCP retrieve surface (end-to-end through the store choke point)
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_server_fixture(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from furl_ctx.ccr.mcp_server import FurlMCPServer

    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    reset_compression_store()
    server = FurlMCPServer()
    yield server
    reset_compression_store()


async def test_mcp_full_retrieve_emits_signal(mcp_server_fixture) -> None:
    server = mcp_server_fixture
    store = server._get_local_store()
    hash_key = store.store(
        original=json.dumps(ITEMS),
        compressed="[]",
        tool_name="websearch",
        compression_strategy="smart_crusher_row_drop",
    )

    result = await server._retrieve_content(hash_key, None)

    assert "error" not in result
    assert _events_in_window() == 1


async def test_mcp_search_retrieve_emits_signal_only_on_match(mcp_server_fixture) -> None:
    server = mcp_server_fixture
    store = server._get_local_store()
    hash_key = store.store(
        original=json.dumps(ITEMS),
        compressed="[]",
        tool_name="websearch",
        compression_strategy="smart_crusher_row_drop",
    )

    no_match = await server._retrieve_content(hash_key, "xylophone zeppelin quasar")
    assert no_match.get("count") == 0
    assert _events_in_window() == 0

    match = await server._retrieve_content(hash_key, "Python programming")
    assert match.get("count", 0) >= 1
    assert _events_in_window() == 1
