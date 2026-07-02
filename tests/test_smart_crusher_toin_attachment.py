"""TOIN re-attachment regression tests for the Rust-backed SmartCrusher.

When SmartCrusher's Python implementation was retired in Stage 3c.1b,
its inline `toin.record_compression()` call was lost. The Rust port
doesn't know about TOIN, and `ContentRouter._record_to_toin` skips
SmartCrusher on the assumption SmartCrusher records its own. The net
result was a silent regression: JSON-array compressions — the
highest-traffic strategy — stopped feeding the learning loop.

The shim now bridges the gap. These tests assert that:

1. Calling `SmartCrusher.crush(...)` on a JSON array of dicts produces a
   `record_compression` event in TOIN when the input is large enough to
   actually compress.
2. Pass-through inputs (no modification) do NOT record — TOIN only
   learns from real compression events.
3. The recorded `tool_signature.structure_hash` is computed over the
   parsed items, so two compressions of structurally-similar inputs
   land on the same pattern.
4. Calling `_smart_crush_content(...)` (the legacy `apply()` path) also
   records.
5. TOIN failures are non-fatal — compression completes even if the
   telemetry import or call raises.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from headroom.telemetry.toin import TOINConfig, get_toin, reset_toin
from headroom.transforms.smart_crusher import SmartCrusher, SmartCrusherConfig


@pytest.fixture
def fresh_toin():
    reset_toin()
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = str(Path(tmpdir) / "toin.json")
        toin = get_toin(
            TOINConfig(
                storage_path=storage,
                auto_save_interval=0,
            )
        )
        yield toin
        reset_toin()


def _bigger_array(n: int = 60) -> str:
    """JSON array of `n` dicts, large enough to trigger crushing.

    Items use a low-uniqueness shape (`{"status": "ok", "tag": "x"}`)
    so the analyzer recommends compaction or row drops. We need at
    least 200 tokens (`min_tokens_to_crush` default) to enter the
    crusher; 60 items keeps us well above that.
    """
    items = [{"status": "ok", "tag": "x", "n": i} for i in range(n)]
    import json as _json

    return _json.dumps(items)


# ─── crush() path (router-style) ───────────────────────────────────────


def test_crush_records_to_toin_on_modification(fresh_toin):
    crusher = SmartCrusher(SmartCrusherConfig())
    payload = _bigger_array(60)
    pre = sum(p.total_compressions for p in fresh_toin._patterns.values())

    result = crusher.crush(payload, query="test query", bias=1.0)

    assert result.was_modified, (
        "payload did not trigger compression — _bigger_array(60) must exceed "
        "min_tokens_to_crush; bump n if the threshold changed"
    )
    post = sum(p.total_compressions for p in fresh_toin._patterns.values())
    assert post > pre, "TOIN should have recorded a compression event"


def test_crush_does_not_record_on_passthrough(fresh_toin):
    """A small input the analyzer doesn't compress should produce no
    TOIN recording, even if the Rust port flipped `was_modified=True`
    from JSON whitespace re-canonicalization. The strategy stays
    `passthrough` in that case and we filter on it."""
    crusher = SmartCrusher(SmartCrusherConfig())
    payload = '[{"id": 1}]'  # Single item — below min_items_to_analyze.
    pre = sum(p.total_compressions for p in fresh_toin._patterns.values())

    result = crusher.crush(payload, query="", bias=1.0)
    assert result.strategy == "passthrough"
    post = sum(p.total_compressions for p in fresh_toin._patterns.values())
    assert post == pre, "no recording when strategy is passthrough"


def test_crush_signature_groups_similar_inputs(fresh_toin):
    """Two structurally-similar payloads should record under the same
    tool signature so TOIN can aggregate the pattern across calls."""
    crusher = SmartCrusher(SmartCrusherConfig())

    payload_a = _bigger_array(60)
    payload_b = _bigger_array(80)

    crusher.crush(payload_a, query="", bias=1.0)
    crusher.crush(payload_b, query="", bias=1.0)

    assert fresh_toin._patterns, (
        "neither payload compressed — _bigger_array(60) and _bigger_array(80) must "
        "exceed min_tokens_to_crush; bump n if the threshold changed"
    )
    # Both share field shape {status, tag, n} → same structure hash →
    # one pattern with at least 2 recordings.
    pattern_counts = {h: p.total_compressions for h, p in fresh_toin._patterns.items()}
    assert max(pattern_counts.values()) >= 2, (
        f"expected the same pattern to be recorded twice, got {pattern_counts}"
    )


# ─── _smart_crush_content() path (legacy apply()) ──────────────────────


def test_smart_crush_content_records_to_toin(fresh_toin):
    crusher = SmartCrusher(SmartCrusherConfig())
    payload = _bigger_array(60)
    pre = sum(p.total_compressions for p in fresh_toin._patterns.values())

    crushed, was_modified, _info = crusher._smart_crush_content(
        payload, query_context="user query", tool_name="get_records", bias=1.0
    )

    assert was_modified, (
        "payload did not trigger compression — _bigger_array(60) must exceed "
        "min_tokens_to_crush; bump n if the threshold changed"
    )
    post = sum(p.total_compressions for p in fresh_toin._patterns.values())
    assert post > pre


# ─── Failure modes ─────────────────────────────────────────────────────


def test_toin_failure_does_not_break_compression(fresh_toin):
    """If TOIN's `record_compression` raises, compression still
    completes and returns a valid CrushResult. Telemetry is best-
    effort; the request must not fail."""
    crusher = SmartCrusher(SmartCrusherConfig())
    payload = _bigger_array(60)

    def _boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("simulated TOIN backend down")

    with patch.object(fresh_toin, "record_compression", side_effect=_boom):
        result = crusher.crush(payload, query="", bias=1.0)
        assert isinstance(result.compressed, str)
        # Crusher itself should still report a result regardless of
        # whether the underlying analysis chose to modify.


def test_non_json_input_does_not_record(fresh_toin):
    """Non-JSON input shouldn't blow up — it just doesn't record.
    The Rust crusher returns `was_modified=False` for non-arrays;
    even if it didn't, the helper guards against `json.loads`
    failure."""
    crusher = SmartCrusher(SmartCrusherConfig())
    pre = sum(p.total_compressions for p in fresh_toin._patterns.values())

    result = crusher.crush("not json at all", query="", bias=1.0)
    assert result.was_modified is False  # passthrough
    post = sum(p.total_compressions for p in fresh_toin._patterns.values())
    assert post == pre


# ─── CCR marker knob ───────────────────────────────────────────────────


def test_ccr_inject_marker_false_suppresses_markers_in_output(fresh_toin):
    """`inject_retrieval_marker=False` suppresses the prompt-visible
    marker on the NO-DROP (lossless) path. Scope note: under the
    production default (route-by-min-tokens) this uniform array routes
    lossy and a dropped row ALWAYS surfaces its `<<ccr:HASH>>` recovery
    pointer (Defect 1 — the pointer is the retrieval key, not a UX flag;
    that path is pinned by tests/test_ccr_recovery_invariant.py). The
    suppression knob only applies where there is no forced drop, so we
    pin LosslessFirst here to isolate it."""
    from headroom.config import CCRConfig

    crusher = SmartCrusher(
        SmartCrusherConfig(routing_policy="lossless-first"),
        ccr_config=CCRConfig(enabled=True, inject_retrieval_marker=False),
    )
    payload = _bigger_array(60)
    result = crusher.crush(payload, query="", bias=1.0)

    assert result.strategy != "passthrough", (
        "payload must compress on lossless-first — _bigger_array(60) must exceed "
        "min_tokens_to_crush; bump n if the threshold changed"
    )

    assert "<<ccr:" not in result.compressed, f"expected no marker, got: {result.compressed!r}"
    assert "_ccr_dropped" not in result.compressed


def test_ccr_inject_marker_true_emits_markers_when_lossy(fresh_toin):
    """The opt-in case keeps marker emission on. If the lossy path
    runs (which it should for a sufficiently big crushable payload),
    the `<<ccr:HASH>>` marker appears in the compressed output."""
    from headroom.config import CCRConfig

    crusher = SmartCrusher(
        SmartCrusherConfig(),
        ccr_config=CCRConfig(enabled=True, inject_retrieval_marker=True),
    )
    payload = _bigger_array(60)
    result = crusher.crush(payload, query="", bias=1.0)

    assert result.strategy != "passthrough", (
        "payload must compress — _bigger_array(60) must exceed "
        "min_tokens_to_crush; bump n if the threshold changed"
    )
    # The engine drops rows (smart_sample, lossy, or any row-offload strategy)
    # and emits a <<ccr:HASH>> marker when inject_retrieval_marker=True.
    # The uniform {status, tag, n} shape always produces a drop on a non-passthrough
    # compression, so the marker must be present regardless of the strategy label.
    assert "<<ccr:" in result.compressed, (
        f"marker absent after non-passthrough compression (strategy={result.strategy!r}); "
        "inject_retrieval_marker=True must emit a marker whenever rows are dropped"
    )


def test_ccr_enabled_false_suppresses_markers_in_output(fresh_toin):
    """`CCRConfig.enabled=False` is the master kill-switch for the
    prompt-visible marker and must behave the same as
    `inject_retrieval_marker=False`: no marker text, no sentinel key in
    the output. Both flags collapse to the Rust-side
    `enable_ccr_marker=False` gate. Per DESIGN.md 1A the underlying CCR
    store write is unconditional (so a dropped needle is never silently
    lost); this test pins only the prompt-visible suppression on the
    NO-DROP (lossless) path. (On the lossy/drop path the recovery pointer
    is unconditional — Defect 1 — and is pinned by
    tests/test_ccr_recovery_invariant.py; we pin LosslessFirst here to
    isolate the suppression knob.)"""
    from headroom.config import CCRConfig

    crusher = SmartCrusher(
        SmartCrusherConfig(routing_policy="lossless-first"),
        # Note: inject_retrieval_marker stays True — we want to prove
        # `enabled=False` alone is enough to suppress.
        ccr_config=CCRConfig(enabled=False, inject_retrieval_marker=True),
    )
    payload = _bigger_array(60)
    result = crusher.crush(payload, query="", bias=1.0)

    assert result.strategy != "passthrough", (
        "payload must compress on lossless-first — _bigger_array(60) must exceed "
        "min_tokens_to_crush; bump n if the threshold changed"
    )

    assert "<<ccr:" not in result.compressed, f"expected no marker, got: {result.compressed!r}"
    assert "_ccr_dropped" not in result.compressed


def test_ccr_marker_off_still_persists_and_is_retrievable(fresh_toin):
    """DESIGN.md sub-step 1A — kill silent loss.

    A forced row drop must persist the original to the CCR store and be
    recoverable by hash, even with `inject_retrieval_marker=False`. This
    is the exact failure 1A eliminates: a dropped needle is never
    silently, unrecoverably lost.

    Per Defect 1 the `<<ccr:HASH>>` recovery pointer is the retrieval
    KEY, not a UX nicety — so it is surfaced UNCONDITIONALLY on every
    drop (the `inject_retrieval_marker` flag governs only the heavier
    retrieval-tool advertisement, owned by the proxy layer). We drive the
    structured `crush_array_json` path (which always surfaces `ccr_hash`
    when rows drop) and assert:
      1. the recovery pointer is present in `dropped_summary` (the key),
      2. a `ccr_hash` is still returned,
      3. `ccr_get(hash)` round-trips the canonical original array.
    """
    import json as _json

    from headroom.config import CCRConfig

    crusher = SmartCrusher(
        SmartCrusherConfig(),
        ccr_config=CCRConfig(enabled=True, inject_retrieval_marker=False),
    )
    payload = _bigger_array(60)
    result = crusher.crush_array_json(payload, query="", bias=1.0)

    strategy = str(result.get("strategy_info") or "")
    assert result.get("ccr_hash"), (
        f"payload did not trigger a row drop (strategy={strategy!r}); "
        "_bigger_array(60) must produce a drop via crush_array_json; "
        "bump n if the threshold changed"
    )

    # The recovery pointer IS surfaced on a drop — it is the retrieval
    # key (Defect 1), not gated by the marker flag.
    dropped_summary = str(result.get("dropped_summary") or "")
    assert "<<ccr:" in dropped_summary, (
        f"a dropped row must surface its recovery pointer (Defect 1), got: {dropped_summary!r}"
    )

    # ...and the hash is returned and the original is recoverable.
    ccr_hash = str(result["ccr_hash"])
    assert ccr_hash in dropped_summary, "the pointer must name the returned hash"
    recovered = crusher.ccr_get(ccr_hash)
    assert recovered is not None, "dropped payload must be retrievable from the CCR store (1A)"

    original_items = _json.loads(payload)
    recovered_items = _json.loads(recovered)
    assert recovered_items == original_items, "recovered payload must equal the original array"


def test_imp2_stable_hash_collapses_identity_varying_rows(fresh_toin):
    """DESIGN.md Imp2 — field-aware stable-projection hash on the LOSSY path.

    A JSON array of log rows that are identical EXCEPT for a varying
    request-id must collapse via HONEST dedup on the lossy path: the kept
    representatives carry a `_dup_count` recording how many original rows
    shared their content, rather than every distinct-by-id row being
    deleted. This proves the stable hash excludes identity columns from
    grouping while the canonical CCR hash stays full-item.

    Note: for cleanly-tabular repeated data the engine's *lossless*
    columnar path wins (it keeps ALL rows AND compresses — strictly better
    than dedup), so we force the lossy path with
    `lossless_min_savings_ratio=0.99` to exercise the dedup grouping that
    Imp2 changes. The honest-dedup win is real on the path it applies to.
    """
    import json as _json

    # 90 rows, 3 distinct messages, each repeated 30x with a unique
    # request-id (the "one varying field defeats dedup" shape). Whole-item
    # hashing makes all 90 unique; the stable projection (excluding the
    # identity req_id) sees only 3 distinct rows.
    messages = ["disk full on /dev/sda1", "auth token refreshed", "cache miss for key user:42"]
    items = [{"req_id": f"{i:040x}", "level": "INFO", "msg": messages[i % 3]} for i in range(90)]

    # Force the lossy path (lossless columnar would otherwise keep all
    # rows and never reach the dedup grouping Imp2 touches).
    crusher = SmartCrusher(SmartCrusherConfig(lossless_min_savings_ratio=0.99))
    result = crusher.crush_array_json(_json.dumps(items), query="", bias=1.0)

    kept = result.get("items")
    kept_rows = _json.loads(kept) if isinstance(kept, str) else kept
    kept_rows = [
        r for r in kept_rows if not (isinstance(r, dict) and set(r.keys()) == {"_ccr_dropped"})
    ]

    # The varying req_id column is excluded from the dedup grouping, so
    # the kept representatives collapse to the 3 distinct messages and
    # carry `_dup_count = 30`. Without Imp2 every row hashed unique and
    # the survivors would be 15 arbitrary distinct-by-id rows with no
    # dup_count at all.
    dup_counts = [
        r.get("_dup_count") for r in kept_rows if isinstance(r, dict) and "_dup_count" in r
    ]
    assert dup_counts, (
        "expected representatives to carry _dup_count after identity-aware "
        f"collapse; kept rows: {kept_rows}"
    )
    assert all(c == 30 for c in dup_counts), f"each of 3 messages repeated 30x; got {dup_counts}"
    # All 3 distinct value-bearing messages survive — none silently lost.
    kept_msgs = {r.get("msg") for r in kept_rows if isinstance(r, dict)}
    for m in messages:
        assert m in kept_msgs, f"distinct message {m!r} must survive; got {kept_msgs}"

    # CCR canonical hash is unchanged + the full original is recoverable
    # (the canonical hash is full-item, not the stable projection).
    ccr_hash = result.get("ccr_hash")
    assert ccr_hash, "lossy drop should produce a CCR hash"
    recovered = crusher.ccr_get(str(ccr_hash))
    assert recovered is not None
    assert _json.loads(recovered) == items


# ─── Custom scorer / relevance_config override ─────────────────────────


def test_custom_scorer_arg_raises_not_implemented():
    """The Rust port doesn't support custom scorers yet. Silently
    dropping a user-supplied scorer would be a textbook silent
    fallback (the user's scoring logic gets ignored, compression
    looks fine but is wrong). Fail loud instead."""

    class FakeScorer:
        pass

    with pytest.raises(NotImplementedError, match="relevance_config.*scorer"):
        SmartCrusher(SmartCrusherConfig(), scorer=FakeScorer())


def test_custom_relevance_config_arg_raises_not_implemented():
    """Same fail-loud contract for `relevance_config`."""
    with pytest.raises(NotImplementedError, match="relevance_config.*scorer"):
        SmartCrusher(SmartCrusherConfig(), relevance_config={"alpha": 0.7})


def test_default_construction_still_works():
    """Sanity: the audit fail-loud only triggers when the user passes
    one of the unsupported args. Default `SmartCrusher()` still
    constructs fine."""
    SmartCrusher(SmartCrusherConfig())  # no raise
