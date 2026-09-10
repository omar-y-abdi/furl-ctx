"""Cluster G regression: CCR capacity eviction must lose data LOUDLY.

The break-eval flagged "FIFO eviction -> unbacked sentinels" as a silent-loss
defect (Cluster G). Independent verification showed the *eviction* is real
(concern #1: a 1000-entry-cap overflow drops the oldest whole-blob entry) but
the *loss is already loud* (concern #2): every model-facing retrieval path runs
through ``FurlMCPServer._retrieve_content``, which calls ``get_entry_status``
and returns an explicit ``error`` payload to the model on a miss — never a silent
empty/None. The "no silent loss" invariant therefore already held; G as a
*silent*-loss defect does not reproduce.

The one genuine residual was DIAGNOSTIC: a capacity-evicted entry reported
``status="missing"`` with the message *"Entry not found (CCR TTL: 300 seconds)"*,
misattributing a capacity eviction to the TTL. These tests lock BOTH facts:

  1. a capacity-evicted retrieval is LOUD (explicit error reaches the model),
     for the bulk whole-blob hash AND a real granular ``#rows`` offload; and
  2. the miss message is CAUSE-HONEST — it names eviction/capacity, not TTL alone.

True cross-call retention (so the data is actually still there) is the open
"free lunch" tracked in CCR-RETENTION.md — a separate concern from loudness.
"""

from __future__ import annotations

import json
import types

import pytest

from furl_ctx.cache.compression_store import (
    DEFAULT_CCR_TTL_SECONDS,
    format_retrieval_miss_detail,
    get_compression_store,
    reset_compression_store,
)
from furl_ctx.ccr.marker_grammar import hashes_in_text
from furl_ctx.ccr.mcp_server import FurlMCPServer
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig


@pytest.fixture(autouse=True)
def _isolate_store():
    """Each test gets a fresh global store; never leak entries across tests."""
    reset_compression_store()
    yield
    reset_compression_store()


def _model_sees(store: object, hash_key: str) -> tuple[bool, dict]:
    """Retrieve as the model does (through the live MCP retrieve surface).

    Exercise the real method without the MCP SDK by binding a duck-typed
    ``self`` (the miss path only needs ``_get_local_store`` + no proxy). A
    miss is signalled by an ``error`` key in the returned payload; success
    by its absence.
    """
    stub = types.SimpleNamespace(check_proxy=False, _get_local_store=lambda: store)
    # PERF-16 relocated the retrieve branch logic into the synchronous ``_retrieve_content_sync`` core (async
    # ``_retrieve_content`` is now a thin ``asyncio.to_thread`` wrapper); drive the core directly with the stub self.
    payload = FurlMCPServer._retrieve_content_sync(stub, hash_key, None)
    return ("error" not in payload), payload


def test_format_miss_detail_is_cause_honest_for_eviction() -> None:
    # A 'missing' status is what a capacity eviction produces (the entry is gone
    # from the backend, indistinguishable from never-stored without tracking).
    missing = {
        "hash": "deadbeef",
        "status": "missing",
        "default_ttl_seconds": DEFAULT_CCR_TTL_SECONDS,
        "max_entries": 1000,
    }
    msg = format_retrieval_miss_detail(missing)
    lo = msg.lower()
    # Cause-honest: names eviction + capacity, and points at the durable remedy.
    assert "evict" in lo, f"miss message must name eviction as a cause: {msg!r}"
    assert "capacity" in lo, f"miss message must mention capacity: {msg!r}"
    assert "1000" in msg, f"miss message should cite the configured capacity: {msg!r}"
    # Must NOT misattribute a capacity eviction to the TTL alone (the old bug).
    assert msg != f"Entry not found (CCR TTL: {DEFAULT_CCR_TTL_SECONDS} seconds)"


def test_format_miss_detail_keeps_exact_cause_for_real_expiry() -> None:
    # An actually-expired entry has an exact cause; keep the precise TTL+age wording.
    expired = {
        "hash": "cafe",
        "status": "expired",
        "ttl_seconds": 300,
        "default_ttl_seconds": 300,
        "age_seconds": 412.0,
    }
    msg = format_retrieval_miss_detail(expired)
    assert "expired" in msg.lower()
    assert "300" in msg and "412" in msg


def test_capacity_evicted_bulk_retrieval_is_loud_and_cause_honest() -> None:
    # Small-cap global store so an overflow deterministically evicts the oldest.
    store = get_compression_store(max_entries=4)
    victim = "aaaaaaaaaaaa"  # 12-hex, the SmartCrusher marker form
    store.store(
        original=json.dumps([{"id": 0, "v": "needle"}]),
        compressed=f"<<ccr:{victim}>>",
        original_item_count=1,
        explicit_hash=victim,
    )
    # Overflow the cap with newer entries -> FIFO evicts the victim.
    for i in range(8):
        h = f"{i:012x}"
        store.store(
            original=json.dumps([{"id": i + 1}]),
            compressed=f"<<ccr:{h}>>",
            original_item_count=1,
            explicit_hash=h,
        )

    assert store.retrieve(victim) is None, "precondition: victim was evicted (concern #1)"

    success, payload = _model_sees(store, victim)
    # concern #2: the miss is LOUD — explicit error reaches the model, no silent None.
    assert success is False
    assert "error" in payload and payload["hash"] == victim
    err = payload["error"].lower()
    assert "evict" in err and "capacity" in err, f"miss must be cause-honest: {payload['error']!r}"


def test_capacity_evicted_granular_offload_retrieval_is_loud() -> None:
    # Drive a REAL granular row-drop offload through the engine, then evict it DETERMINISTICALLY and confirm the
    # model-facing miss is loud. The bare hash the model retrieves is backed by a single whole-blob entry (all rows)
    cap = 8
    store = get_compression_store(max_entries=cap)
    router = ContentRouter(ContentRouterConfig())
    items = [
        {"id": i, "user": f"u{i % 9}", "msg": f"event {i} payload {'x' * 12}", "ok": True}
        for i in range(240)
    ]
    # Scan with the PRODUCTION grammar (``hashes_in_text``), not a hand-rolled regex. A local ``[a-f0-9]{6,}(?:[ ,>])``
    # diverges from DOUBLE_ANGLE_DELIM in both directions. ``hashes_in_text`` enforces the strict {12, 24} consumer widths.
    res = router.compress(json.dumps(items, ensure_ascii=False))
    bare = hashes_in_text(res.compressed)
    assert bare, (
        "the granular row-drop offload emitted no <<ccr:...>> sentinel for the "
        "240-item fixture: the engine's routing/offload path regressed (the "
        "behaviour under test) — a failure, not a reason to skip"
    )
    victim = bare[0]

    # The emitted marker MUST be backed by a live entry under its own hash, or the model could never resolve it.
    assert store.retrieve(victim) is not None, (
        f"granular offload emitted <<ccr:{victim} ...>> but stored no retrievable "
        f"entry under that hash — the marker is a dead pointer (a marker/store-key "
        f"regression)"
    )

    # Force deterministic oldest-first eviction with newer filler entries. Prefix filler hashes with `f` so equal timestamps
    # sort after realistic victims; otherwise a timestamp tie can evict fillers and leave the intended victim alive.
    for i in range(2 * cap):
        filler = f"f{i:023x}"
        store.store(
            original=json.dumps([{"evict_filler": i}]),
            compressed=f"<<ccr:{filler}>>",
            original_item_count=1,
            explicit_hash=filler,
        )

    assert store.retrieve(victim) is None, (
        f"granular victim <<ccr:{victim} ...>> survived {2 * cap} strictly-newer "
        f"stores into a max_entries={cap} store: capacity eviction "
        f"(oldest-created-first) regressed — a failure, not a reason to skip"
    )

    success, payload = _model_sees(store, victim)
    # concern #2: the miss is LOUD — explicit error reaches the model, no silent None.
    assert success is False, "evicted granular offload must miss loudly, not silently"
    assert "error" in payload and payload["hash"] == victim
    err = payload["error"].lower()
    assert "evict" in err and "capacity" in err, (
        f"granular miss must be cause-honest: {payload['error']!r}"
    )


def test_mcp_server_retrieve_miss_is_loud_and_cause_honest() -> None:
    # The model-facing retrieval surface (MCP tool) must report a miss as an explicit `error`, cause-honest, never a silent empty result.

    store = get_compression_store(max_entries=4)
    victim = "bbbbbbbbbbbb"
    store.store(
        original=json.dumps([{"id": 0, "v": "needle"}]),
        compressed=f"<<ccr:{victim}>>",
        original_item_count=1,
        explicit_hash=victim,
    )
    for i in range(8):
        h = f"{i:012x}"
        store.store(
            original=json.dumps([{"id": i + 1}]),
            compressed=f"<<ccr:{h}>>",
            original_item_count=1,
            explicit_hash=h,
        )
    assert store.retrieve(victim) is None, "precondition: victim evicted"

    stub = types.SimpleNamespace(check_proxy=False, _get_local_store=lambda: store)
    result = FurlMCPServer._retrieve_content_sync(stub, victim, None)

    assert "error" in result and result["hash"] == victim, "MCP miss must be loud"
    err = result["error"].lower()
    assert "evict" in err and "capacity" in err, (
        f"MCP miss must be cause-honest: {result['error']!r}"
    )


def test_fixture_actually_fires_eviction() -> None:
    """Integration pin: the router's OWN overflow evicts a real granular victim.

    ``test_capacity_evicted_granular_offload_retrieval_is_loud`` drives eviction
    deterministically through direct ``store.store`` fillers, which makes THIS the
    only remaining coverage that the router's real overflow path evicts at all.
    Both preconditions are assertions — a missing sentinel or a surviving victim
    points at the engine (offload routing / store eviction), never a reason to skip.

    (A third companion asserting only the sentinel precondition was pruned: it was
    byte-identical to the main test's opening lines plus the same assert, so it
    added no detection power in either direction.)
    """
    store = get_compression_store(max_entries=8)
    router = ContentRouter(ContentRouterConfig())
    items = [
        {"id": i, "user": f"u{i % 9}", "msg": f"event {i} payload {'x' * 12}", "ok": True}
        for i in range(240)
    ]
    res = router.compress(json.dumps(items, ensure_ascii=False))
    # Production grammar, not a hand-rolled regex — see the main test for why a
    # local `[a-f0-9]{6,}(?:[ ,>])` is wrong in both directions.
    bare = hashes_in_text(res.compressed)
    assert bare, (
        "engine emitted no CCR sentinel for the 240-item fixture — the engine's "
        "granular row-drop/offload routing regressed; look at the offload path, "
        "not the fixture"
    )
    victim = bare[0]

    for c in range(20):
        rows = [{"c": c, "j": j, "d": f"r_{c}_{j}_{'y' * 7}"} for j in range(60)]
        router.compress(json.dumps(rows, ensure_ascii=False))

    assert store.retrieve(victim) is None, (
        "granular victim was NOT evicted after 20 router-overflow batches into a "
        "max_entries=8 store: capacity eviction (oldest-created-first) regressed in "
        "the store/router — the loudness check would be unobservable"
    )
