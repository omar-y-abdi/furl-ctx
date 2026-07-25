"""T3 (reproduction-first pre-mortem audit): a CCR recovery-key collision must
never let one dropped row recover as ANOTHER row's content.

The bug (historical)
--------------------
SmartCrusher once keyed every dropped-row recovery entry by ``SHA-256(canonical)``
truncated to 12 hex chars — 48 bits. A 48-bit space collides by the birthday
bound after ~2**24 distinct rows, and the audit brute-forced a real pair in
~55s. When two rows that collide were BOTH dropped inside one ``crush_array_json``
call, the Rust ``InMemoryCcrStore`` silently overwrote the first row's payload
with the second under the shared per-row-chunk key, THEN the Rust->Python mirror
copied the (already-overwritten) bytes out and stored them under the shared key
— so the first row's ``<<ccr:HASH>>`` marker silently recovered the SECOND row's
content on both the library and MCP retrieval paths.

The colliding pair below was found by a fresh birthday search over ``["c<N>"]``
single-row arrays (the crusher's old per-row chunk shape):

    SHA-256(canonical_array_json(["c5659401"]))[:12]
      == SHA-256(canonical_array_json(["c18191506"]))[:12]
      == "09659eb7ee43"

Why it can no longer happen (Design A, F8/#168)
-----------------------------------------------
Per-row chunks are no longer stored in ANY store: a row-drop persists ONLY the
whole-blob parent (keyed by its own 24-hex hash, which this pair does not share)
and row recovery is served from that parent. A colliding per-row key is never an
independently-addressable entry, so it can never serve a DIFFERENT row's content.
This test pins three things against the PRODUCTION ``compression_store`` the
model reads: the mechanism-INDEPENDENT invariant this file is named for (no key
ever serves foreign content — holds no matter how the fix works), the Design-A
specific (the colliding per-row keys are not stored at all), and recovery
through the whole-blob parent.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from furl_ctx.cache.compression_store import get_compression_store
from furl_ctx.transforms.smart_crusher import SmartCrusher, SmartCrusherConfig

# A verified 48-bit colliding pair (see module docstring). Distinct single-row
# values whose canonical 1-element arrays share SHA-256[:12] but NOT SHA-256[:24].
ROW_A = "c5659401"
ROW_B = "c18191506"


def _canon(items: list[str]) -> str:
    """The canonical JSON the crusher hashes: compact, non-ASCII preserved —
    byte-identical to ``serde_json::to_string`` for these scalar rows."""
    return json.dumps(items, separators=(",", ":"), ensure_ascii=False)


def _key(row: str, width: int) -> str:
    return hashlib.sha256(_canon([row]).encode("utf-8")).hexdigest()[:width]


def _collision_precondition() -> None:
    """Guard: the pair must collide at 12 hex and diverge at 24 hex, else the
    fixture is stale and the test proves nothing."""
    assert _key(ROW_A, 12) == _key(ROW_B, 12), "fixture pair no longer collides at 12 hex"
    assert _key(ROW_A, 24) != _key(ROW_B, 24), "fixture pair unexpectedly collides at 24 hex"


def _crush_both_rows_dropped() -> tuple[SmartCrusher, str]:
    """Drive a real ``crush_array_json`` over an array that buries ROW_A and
    ROW_B among filler so BOTH are dropped (row-drop path). Returns
    ``(crusher, blob_hash)``.

    Both colliding rows are placed mid-array (index 40 and 80): the crusher keeps
    the highest-relevance rows, and burying them away from index 0 keeps them out
    of the kept set for a query that matches only filler. Both must land in the
    whole-blob parent, never the visible sample — asserted below so the collision
    scenario is genuinely exercised.
    """
    filler = [f"log-line-{i}-payload-filler" for i in range(120)]
    items = filler[:40] + [ROW_A] + filler[40:80] + [ROW_B] + filler[80:]
    crusher = SmartCrusher(config=SmartCrusherConfig())
    result = crusher.crush_array_json(
        json.dumps(items, ensure_ascii=False), query="log-line-0-payload-filler"
    )
    blob_hash = result.get("ccr_hash")
    assert blob_hash, "fixture did not take the row-drop path (no ccr_hash)"
    # Confirm BOTH colliding rows were DROPPED (offloaded to the parent), not
    # kept in the visible sample — the kept rows are surfaced in `items`. Without
    # this, a keep-both outcome would make the collision assertions vacuous.
    kept = result.get("items") or ""
    assert ROW_A not in kept and ROW_B not in kept, (
        "fixture setup wrong: a colliding row was kept in the visible sample instead of dropped"
    )
    return crusher, blob_hash


def test_recovery_hash_collision_cannot_return_wrong_data() -> None:
    """A recovery-key collision must never let one dropped row recover as
    ANOTHER row's content on the Python / MCP retrieval plane.

    The T3 bug: at a 12-hex key ROW_A and ROW_B collide; when both were dropped
    and CHUNKED, the Rust store overwrote one under the shared key and the
    Rust->Python mirror copied the wrong bytes out, so ROW_A's key silently
    recovered ROW_B's content on the durable retrieval path.

    Design A removes that path entirely: per-row chunks are no longer stored in
    any store, so the colliding per-row keys are not entries at all (a loud
    miss, never foreign bytes), and row recovery is served from the whole-blob
    PARENT, which holds each row's own canonical bytes. All three assertions
    query the PRODUCTION ``compression_store`` the model reads.
    """
    _collision_precondition()
    _crusher, blob_hash = _crush_both_rows_dropped()

    # Width the producer actually emitted (24 hex on current main).
    width = len(blob_hash)

    store = get_compression_store()

    # (1) MECHANISM-INDEPENDENT INVARIANT — the property this file is named for:
    # no dropped row's recovery key ever resolves to ANOTHER dropped row's
    # content. Holds regardless of HOW the fix works — on buggy main ROW_A's key
    # resolved to ROW_B's bytes (RED); under Design A the key resolves to nothing
    # (None != foreign content). Catches BOTH a chunk-mirroring reintroduction
    # AND the wrong-data outcome, so it is strictly stronger than the absence
    # check below.
    for row, other in ((ROW_A, ROW_B), (ROW_B, ROW_A)):
        entry = store.retrieve(_key(row, width))
        recovered = entry.original_content if entry is not None else None
        assert recovered != _canon([other]), (
            f"silent wrong-data recovery: {row!r}'s recovery key resolved to "
            f"{other!r}'s content ({recovered!r})"
        )

    # (2) MECHANISM-SPECIFIC (Design A): the colliding per-row keys are NOT
    # independently addressable at all, because per-row chunks are never stored.
    # A key that never resolves can never serve a DIFFERENT row's content. This
    # pins today's implementation so a reintroduced mirror is caught even if a
    # future key width happened to dodge this pair's collision.
    for row in (ROW_A, ROW_B):
        assert store.retrieve(_key(row, width)) is None, (
            f"{row!r}'s per-row chunk was mirrored as its own entry; a colliding "
            "chunk key could then serve foreign content"
        )

    # (3) Both dropped rows recover as their OWN content from the whole-blob
    # parent — the canonical array of every original row, keyed by its own
    # 24-hex hash (which the store's collision guard protects independently).
    parent = store.retrieve(blob_hash)
    assert parent is not None, "whole-blob parent recovery entry is missing"
    recovered = json.loads(parent.original_content)
    assert recovered.count(ROW_A) == 1 and recovered.count(ROW_B) == 1, (
        "the parent blob did not recover both colliding rows exactly once each"
    )


if __name__ == "__main__":  # pragma: no cover - manual reproduction helper
    raise SystemExit(pytest.main([__file__, "-q"]))
