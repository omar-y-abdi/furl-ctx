"""F-beta2 pins: ``search_all`` no longer holds the store lock across the full decode.

Before the fix ``search_all`` materialized every entry via ``self._backend.items()``
INSIDE ``self._lock``, decoding up to the cap of large entries while blocking every
concurrent store op. It now snapshots keys under the lock and decodes each entry
under a brief per-key lock, so a concurrent op can interleave.

Pin 1 (behavior): with an entry whose ``is_expired`` blocks, which is the
decode-and-filter step, a concurrent lock-taking op completes while the block is
held, proving the lock is free during decode. Pre-fix the block ran inside the
locked comprehension, so the concurrent op is starved and the pin times out.

Pin 2 (semantics): the ranked results are unchanged versus the documented
ordering, so a matching entry is still ranked first.
"""

from __future__ import annotations

import json
import threading

import pytest

from furl_ctx.cache.compression_store import CompressionEntry, CompressionStore


def _seed(store: CompressionStore, n: int) -> list[str]:
    keys: list[str] = []
    for i in range(n):
        hash_key = f"{i:024x}"
        store.store(
            original=json.dumps([{"id": i, "v": f"needleword item {i}"}]),
            compressed=f"<<ccr:{hash_key}>>",
            explicit_hash=hash_key,
        )
        keys.append(hash_key)
    return keys


def test_search_all_releases_lock_during_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    store = CompressionStore(max_entries=100)
    _seed(store, 3)

    reached_decode = threading.Event()
    release_decode = threading.Event()
    orig_is_expired = CompressionEntry.is_expired
    state = {"blocked_once": False}

    def blocking_is_expired(self: CompressionEntry, now: float | None = None) -> bool:
        # Block the FIRST expiry check, which is the decode-and-filter step of
        # search_all. Post-fix that step runs OUTSIDE the store lock.
        if not state["blocked_once"]:
            state["blocked_once"] = True
            reached_decode.set()
            release_decode.wait(timeout=10)
        return orig_is_expired(self, now)

    monkeypatch.setattr(CompressionEntry, "is_expired", blocking_is_expired)

    results: list[list] = []

    def run_search() -> None:
        results.append(store.search_all("needleword"))

    searcher = threading.Thread(target=run_search, daemon=True)
    searcher.start()

    assert reached_decode.wait(timeout=5), "search_all never reached the decode/filter step"

    # The decode/filter step is blocking. A concurrent lock-taking op must NOT be
    # blocked by it: post-fix the lock is released during decode; pre-fix it is
    # held across the whole comprehension and this op is starved.
    op_done = threading.Event()

    def concurrent_op() -> None:
        store.exists("f" * 24)  # acquires self._lock, does not touch is_expired
        op_done.set()

    threading.Thread(target=concurrent_op, daemon=True).start()
    completed = op_done.wait(timeout=3)

    # Let search finish regardless of the assertion outcome, so threads unwind.
    release_decode.set()
    searcher.join(timeout=5)

    assert completed, (
        "a concurrent store op was blocked while search_all decoded; the lock is "
        "held across the full decode (F-beta2 regression)"
    )
    assert results and any(match.hash for match in results[0])


def test_search_all_results_unchanged() -> None:
    store = CompressionStore(max_entries=100)
    # One clearly-matching entry among noise; ranking must put it first.
    store.store(
        original=json.dumps([{"v": "alpha beta"}]), compressed="<<ccr:11>>", explicit_hash="1" * 24
    )
    store.store(
        original=json.dumps([{"v": "needleword unique"}]),
        compressed="<<ccr:22>>",
        explicit_hash="2" * 24,
    )
    store.store(
        original=json.dumps([{"v": "gamma delta"}]),
        compressed="<<ccr:33>>",
        explicit_hash="3" * 24,
    )

    matches = store.search_all("needleword")
    assert matches, "search_all found nothing for a present term"
    assert matches[0].hash == "2" * 24, "the matching entry must rank first"
