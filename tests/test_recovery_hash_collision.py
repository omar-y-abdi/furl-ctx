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
    """A dropped row's recovery key must resolve to ITS OWN content — never to
    a different dropped row's content.

    RED today: at 12 hex ROW_A and ROW_B share a recovery key, the Rust store
    keeps only the last write, and ROW_A's key resolves to ROW_B's content.
    GREEN after the fix: at the wider emitted key the pair no longer collides
    (each recovers its own row), or the store drops the ambiguous binding so the
    key loud-misses — in neither case is foreign content served.
    """
    _collision_precondition()
    crusher, blob_hash, row_index = _crush_both_rows_dropped()

    # Width the producer actually emitted (12 on buggy main, 24 after the fix).
    width = len(blob_hash)
    key_a = _key(ROW_A, width)
    key_b = _key(ROW_B, width)

    # Setup guard: both colliding rows were dropped and chunked at this width.
    assert key_a in row_index, "ROW_A was not dropped/chunked — fixture setup wrong"
    assert key_b in row_index, "ROW_B was not dropped/chunked — fixture setup wrong"

    store = get_compression_store()

    # The core invariant: no dropped row recovers as the OTHER dropped row's
    # content. On buggy main, ROW_A's key resolves to ROW_B's content — RED.
    for row, other in ((ROW_A, ROW_B), (ROW_B, ROW_A)):
        entry = store.retrieve(_key(row, width))
        recovered = entry.original_content if entry is not None else None
        assert recovered != _canon([other]), (
            f"silent wrong-data recovery: {row!r}'s recovery key resolved to "
            f"{other!r}'s content ({recovered!r})"
        )

    # And each row recovers as its OWN content: at the wider width the two keys
    # are distinct, so both dropped rows are independently retrievable.
    for row in (ROW_A, ROW_B):
        entry = store.retrieve(_key(row, width))
        assert entry is not None, f"{row!r}'s recovery entry is missing"
        assert entry.original_content == _canon([row]), (
            f"{row!r}'s recovery key resolved to {entry.original_content!r}, "
            f"expected {_canon([row])!r}"
        )


if __name__ == "__main__":  # pragma: no cover - manual reproduction helper
    raise SystemExit(pytest.main([__file__, "-q"]))
