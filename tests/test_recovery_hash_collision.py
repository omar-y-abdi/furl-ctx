"""T3 (reproduction-first pre-mortem audit): a CCR recovery-key collision must
never let one dropped row recover as ANOTHER row's content.

The bug
-------
SmartCrusher keyed every dropped-row recovery entry by ``SHA-256(canonical)``
truncated to 12 hex chars — 48 bits. A 48-bit space collides by the birthday
bound after ~2**24 distinct rows, and the audit brute-forced a real pair in
~55s. When two rows that collide are BOTH dropped inside one ``crush_array_json``
call, the Rust ``InMemoryCcrStore`` silently overwrote the first row's payload
with the second under the shared key, THEN the Rust->Python mirror copied the
(already-overwritten) bytes out via ``ccr_get`` and stored them under the shared
key. The Python ``CompressionStore``'s own loud collision guard never fired,
because the first row's bytes never reached it — so the first row's
``<<ccr:HASH>>`` marker silently recovered the SECOND row's content, with no
error, on both the library and the MCP retrieval paths.

The colliding pair below was found by a fresh birthday search over
``["c<N>"]`` single-row arrays (the crusher's per-row chunk shape):

    SHA-256(canonical_array_json(["c5659401"]))[:12]
      == SHA-256(canonical_array_json(["c18191506"]))[:12]
      == "09659eb7ee43"

At 24 hex (96 bits) the two diverge, so widening the emitted key dissolves the
collision entirely; the store-level guard closes it independently of width.

Bite
----
RED on current ``main``: ``c5659401``'s recovery key resolves to ``c18191506``'s
content (verified end-to-end before this test was committed). GREEN once the
emitted key is wide enough that the pair no longer collides (each row recovers
its OWN content) — or the store refuses the ambiguous binding so the key
loud-misses instead of serving foreign bytes.
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


def _crush_both_rows_dropped() -> tuple[SmartCrusher, str, list[str]]:
    """Drive a real ``crush_array_json`` over an array that buries ROW_A and
    ROW_B among filler so BOTH are dropped (row-drop path), chunked, and
    mirrored. Returns ``(crusher, blob_hash, row_index)``.

    Both colliding rows are placed mid-array (index 40 and 80): the crusher
    keeps the highest-relevance rows, and burying them away from index 0 keeps
    them out of the kept set for a query that matches only filler.
    """
    filler = [f"log-line-{i}-payload-filler" for i in range(120)]
    items = filler[:40] + [ROW_A] + filler[40:80] + [ROW_B] + filler[80:]
    crusher = SmartCrusher(config=SmartCrusherConfig())
    result = crusher.crush_array_json(
        json.dumps(items, ensure_ascii=False), query="log-line-0-payload-filler"
    )
    blob_hash = result.get("ccr_hash")
    assert blob_hash, "fixture did not take the row-drop path (no ccr_hash)"
    index_raw = crusher._rust.ccr_get(f"{blob_hash}#rows")
    assert index_raw, "fixture did not produce a granular per-row index"
    row_index: list[str] = json.loads(index_raw)
    return crusher, blob_hash, row_index


def test_recovery_hash_collision_cannot_return_wrong_data() -> None:
    """A recovery-key collision must never let one dropped row recover as
    ANOTHER row's content on the Python / MCP retrieval plane.

    The T3 bug: at a 12-hex key ROW_A and ROW_B collide; when both were dropped
    and CHUNKED, the Rust store overwrote one under the shared key and the
    Rust->Python mirror copied the wrong bytes out, so ROW_A's key silently
    recovered ROW_B's content on the durable retrieval path.

    Design A removes that path entirely: per-row chunks are no longer mirrored
    into the Python store, so the colliding per-row keys are not stored at all
    (a loud miss, never foreign bytes), and row recovery is served from the
    whole-blob PARENT, which holds each row's own canonical bytes. Three things
    are asserted below: the mechanism-INDEPENDENT invariant this file is named
    for (no key ever serves foreign content — holds no matter how the fix
    works), the Design-A specific (the colliding chunk keys are not stored at
    all), and recovery through the parent. The wider 24-hex emitted key means
    the pair no longer even collides, but Design A makes the collision
    unreachable regardless.
    """
    _collision_precondition()
    _crusher, blob_hash, row_index = _crush_both_rows_dropped()

    # Width the producer actually emitted (24 hex on current main).
    width = len(blob_hash)
    key_a = _key(ROW_A, width)
    key_b = _key(ROW_B, width)

    # Setup guard: both colliding rows were dropped and chunked in the Rust index.
    assert key_a in row_index, "ROW_A was not dropped/chunked — fixture setup wrong"
    assert key_b in row_index, "ROW_B was not dropped/chunked — fixture setup wrong"

    store = get_compression_store()

    # (1) MECHANISM-INDEPENDENT INVARIANT — the property this file is named for:
    # no dropped row's recovery key ever resolves to ANOTHER dropped row's
    # content. This holds regardless of HOW the fix works — on buggy main
    # ROW_A's key resolves to ROW_B's bytes (RED); under Design A the key
    # resolves to nothing (None != foreign content); at a wider key it would
    # resolve to its own bytes. It catches BOTH a chunk-mirroring reintroduction
    # AND the wrong-data outcome, so it is strictly stronger than the absence
    # check below and does not need removing when the mechanism changes.
    for row, other in ((ROW_A, ROW_B), (ROW_B, ROW_A)):
        entry = store.retrieve(_key(row, width))
        recovered = entry.original_content if entry is not None else None
        assert recovered != _canon([other]), (
            f"silent wrong-data recovery: {row!r}'s recovery key resolved to "
            f"{other!r}'s content ({recovered!r})"
        )

    # (2) MECHANISM-SPECIFIC (Design A): the colliding per-row chunk keys are
    # NOT independently addressable at all, because chunks are no longer
    # mirrored. A key that never resolves can never serve a DIFFERENT row's
    # content. This pins today's implementation so a reintroduced mirror is
    # caught even if a future key width happened to dodge this pair's collision.
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
