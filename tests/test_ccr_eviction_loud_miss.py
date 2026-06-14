"""Cluster G regression: CCR capacity eviction must lose data LOUDLY.

The break-eval flagged "FIFO eviction -> unbacked sentinels" as a silent-loss
defect (Cluster G). Independent verification showed the *eviction* is real
(concern #1: a 1000-entry-cap overflow drops the oldest whole-blob entry) but
the *loss is already loud* (concern #2): every model-facing retrieval path runs
through ``CCRResponseHandler._execute_retrieval``, which calls
``get_entry_status`` and returns an explicit ``success=False`` error to the
model on a miss — never a silent empty/None. The "no silent loss" invariant
therefore already held; G as a *silent*-loss defect does not reproduce.

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
import re

import pytest

from headroom.cache.compression_store import (
    DEFAULT_CCR_TTL_SECONDS,
    format_retrieval_miss_detail,
    get_compression_store,
    reset_compression_store,
)
from headroom.ccr.response_handler import CCRResponseHandler, CCRToolCall
from headroom.transforms.content_router import ContentRouter, ContentRouterConfig


@pytest.fixture(autouse=True)
def _isolate_store():
    """Each test gets a fresh global store; never leak entries across tests."""
    reset_compression_store()
    yield
    reset_compression_store()


def _model_sees(hash_key: str) -> tuple[bool, dict]:
    """Retrieve as the model does (through the handler) and parse the payload."""
    handler = CCRResponseHandler()
    result = handler._execute_retrieval(CCRToolCall(tool_call_id="t", hash_key=hash_key))
    return result.success, json.loads(result.content)


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
    store.store(original=json.dumps([{"id": 0, "v": "needle"}]), compressed="<<ccr:%s>>" % victim,
                original_item_count=1, explicit_hash=victim)
    # Overflow the cap with newer entries -> FIFO evicts the victim.
    for i in range(8):
        h = f"{i:012x}"
        store.store(original=json.dumps([{"id": i + 1}]), compressed=f"<<ccr:{h}>>",
                    original_item_count=1, explicit_hash=h)

    assert store.retrieve(victim) is None, "precondition: victim was evicted (concern #1)"

    success, payload = _model_sees(victim)
    # concern #2: the miss is LOUD — explicit error reaches the model, no silent None.
    assert success is False
    assert "error" in payload and payload["hash"] == victim
    err = payload["error"].lower()
    assert "evict" in err and "capacity" in err, f"miss must be cause-honest: {payload['error']!r}"


def test_capacity_evicted_granular_offload_retrieval_is_loud() -> None:
    # Drive a REAL granular `#rows` offload through the engine, then evict it and
    # confirm the model-facing miss is loud. The bare hash the model retrieves is
    # backed by a single whole-blob entry (all rows), so eviction is all-or-nothing
    # — never a silent partial subset.
    store = get_compression_store(max_entries=8)
    router = ContentRouter(ContentRouterConfig())
    items = [
        {"id": i, "user": f"u{i % 9}", "msg": f"event {i} payload {'x' * 12}", "ok": True}
        for i in range(240)
    ]
    res = router.compress(json.dumps(items, ensure_ascii=False))
    bare = re.findall(r"<<ccr:([a-f0-9]{6,})(?:[ ,>])", res.compressed)
    if not bare:
        pytest.skip("engine emitted no CCR sentinel for this input on this build")
    victim = bare[0]

    # Overflow the small cap so the granular whole-blob entry is evicted.
    for c in range(20):
        rows = [{"c": c, "j": j, "d": f"r_{c}_{j}_{'y' * 7}"} for j in range(60)]
        router.compress(json.dumps(rows, ensure_ascii=False))

    if store.retrieve(victim) is not None:
        pytest.skip("victim survived eviction on this build; loudness is unobservable here")

    success, payload = _model_sees(victim)
    assert success is False, "evicted granular offload must miss loudly, not silently"
    assert "error" in payload and payload["hash"] == victim
