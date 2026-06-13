"""Regression test: result-cache vs CCR-store lifetime divergence (P0 fix).

The bug: the router's Tier-2 result cache (in ``apply()``) stores the crushed
output (including its ``{"_ccr_dropped": "<<ccr:HASH>>"}`` sentinel) keyed by
content hash.  On a result-cache HIT no fresh compression runs, so the
Rust→Python CCR mirror (``SmartCrusher._mirror_ccr_to_python_store``) was
skipped.  The CCR store has an independent ~300 s TTL.  After that TTL expires
the result cache still returns the sentinel-bearing output, but the Python
compression_store no longer has the entry — a SIGNALLED but UNRECOVERABLE drop
(silent data loss).

Fix (``content_router.py:ContentRouter._ensure_ccr_backed``): on every Tier-2
result-cache HIT whose payload contains ``<<ccr:``, call
``SmartCrusher._mirror_ccr_to_python_store`` to re-persist (or refresh the TTL
of) the backing entry before serving the output.

Reproduction path (mirrors ``verify/run.py::probe_result_cache_ccr_divergence``):
  1. Call ``router.apply(messages, tokenizer)`` → Tier-2 result cache MISS,
     fresh compression runs, CCR mirror runs, drop is backed in Python store.
  2. Wipe the Python compression_store only (simulates the CCR store's
     independent TTL expiring while the router's ``_cache`` is untouched).
  3. Call ``router.apply(messages, tokenizer)`` again with the SAME router and
     SAME messages → Tier-2 result-cache HIT (``cache_hits`` increments),
     same bytes served.
  4. Parse any ``<<ccr:HASH>>`` sentinels from the served output.
  5. Assert every hash is retrievable from the Python compression_store.

Without the fix, step 5 fails: the sentinel is served but unbacked.
With the fix, step 5 passes: ``_ensure_ccr_backed`` re-mirrored on the hit.

BOTH-EXPIRED hardening (``test_both_stores_expired_*``): the Rust CCR store
ALSO has a 300 s TTL (``crates/headroom-core/src/ccr/mod.rs`` DEFAULT_TTL),
same as the Python store, while the result cache (CompressionCache) has a
30-min TTL. After ~5 minutes BOTH CCR stores expire but the result cache still
serves the crushed output → re-mirror finds nothing in the Rust store either →
the served sentinel would be UNBACKED. The strengthened fix detects this and
REFUSES to serve the stale output: it evicts the cache entry and recomputes
(``self.compress()``), which re-creates + re-stores the CCR backing and emits a
fresh backed sentinel. These tests simulate both stores expired with a stateful
Rust shim (``_ExpiringRustShim``) that returns ``None`` from ``ccr_get`` until a
fresh ``crush()`` re-stores — faithfully reproducing "old entry expired, but a
recompute re-creates it".
"""

from __future__ import annotations

import json
import re

import pytest

from headroom.cache.compression_store import get_compression_store, reset_compression_store
from headroom.tokenizer import Tokenizer
from headroom.tokenizers import EstimatingTokenCounter
from headroom.transforms.content_router import ContentRouter, ContentRouterConfig

# General CCR hash extractor: matches <<ccr:HASH>> and <<ccr:HASH ...>>
_ANY_CCR_RE = re.compile(r"<<ccr:([a-f0-9]{6,})")


def _extract_ccr_hashes(text: str) -> set[str]:
    """Return every distinct CCR hash referenced in *text*."""
    return set(_ANY_CCR_RE.findall(text))


def _flatten_content(messages: list[dict]) -> str:
    """Flatten all message content to a single string for hash scanning."""
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("content", "") or block.get("text", ""))
    return "\n".join(parts)


def _log_rows(n: int = 90) -> list[dict]:
    """High-entropy distinct log-shaped rows that force the lossy drop path.

    Fractional-second timestamps are used deliberately: second-precision ISO
    columns delta-encode losslessly and tip the fixture onto the lossless path.
    Fractional seconds are refused by the strict encoder, keeping this fixture
    reliably lossy so the CCR drop sentinel is emitted.
    """
    _PREFIXES = ["feat", "fix", "docs", "chore", "refactor", "test", "perf", "ci"]
    _AREAS = ["crusher", "proxy", "ccr", "router", "bench",
               "tokenizer", "store", "pipeline", "compaction", "relevance"]
    _VERBS = ["add", "remove", "rework", "guard", "pin",
               "extend", "isolate", "deflake", "speed up", "harden"]
    _THINGS = [
        "the lossy budget", "novelty fill", "sentinel emission", "marker parsing",
        "store mirroring", "field-role gates", "ditto marks", "schema folding",
        "query anchors", "drop accounting", "TTL handling", "thread-local state",
        "import guards", "error surfaces", "byte parity",
    ]
    return [
        {
            "commit": f"{i * 2654435761 + 12345:040x}",
            "author": f"Author {i % 7}",
            "date": (
                f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
                f"T{i % 24:02d}:{(i * 13) % 60:02d}:00"
                f".{(i * 104729) % 1000000:06d}+02:00"
            ),
            "subject": (
                f"{_PREFIXES[i % 8]}({_AREAS[i % 10]}): "
                f"{_VERBS[i % 10]} {_THINGS[i % 15]} #{i + 100}"
            ),
        }
        for i in range(n)
    ]


def _make_messages(rows: list[dict]) -> list[dict]:
    return [
        {"role": "user", "content": "show me the git log"},
        {
            "role": "tool",
            "content": json.dumps(rows, ensure_ascii=False),
            "tool_call_id": "call_test_001",
        },
    ]


def _make_tokenizer() -> Tokenizer:
    return Tokenizer(EstimatingTokenCounter())


class _ExpiringRustShim:
    """Wraps the Rust SmartCrusher to simulate the Rust CCR store's TTL expiry.

    ``ccr_get`` returns ``None`` (entry expired) UNTIL a fresh ``crush()`` runs
    — modelling "the original entry's 300 s TTL lapsed, but a recompute
    re-creates and re-stores it". This is the faithful both-expired state: a
    naive cache-hit serve cannot recover, only a recompute can.

    Delegates every other attribute (including ``crush`` side effects that
    re-populate the underlying store) to the real Rust object.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self._restored = False

    def crush(self, *args: object, **kwargs: object) -> object:
        # A recompute re-stores into the live Rust store; from here on the
        # (re-created, same-hash) entry resolves again.
        self._restored = True
        return self._inner.crush(*args, **kwargs)

    def ccr_get(self, hash_key: str) -> str | None:
        if not self._restored:
            return None  # old entry expired
        return self._inner.ccr_get(hash_key)

    def __getattr__(self, name: str) -> object:
        # Everything else (ccr_len, crush_array_json, etc.) delegates.
        return getattr(self._inner, name)


@pytest.fixture(autouse=True)
def _reset_ccr_store():
    """Isolate every test: fresh Python CCR store before and after."""
    reset_compression_store()
    yield
    reset_compression_store()


class TestResultCacheCCRDivergence:
    """Pin the fix for result-cache vs CCR-store TTL divergence.

    All tests drive ``ContentRouter.apply()`` (the messages path that houses
    the Tier-2 result cache) with a single router instance so the second call
    is guaranteed to hit the cache.
    """

    def test_sentinel_backed_after_ccr_expiry_and_cache_hit(self):
        """Core regression: after CCR store is wiped (TTL expiry), a
        result-cache HIT must still produce a backed <<ccr:HASH>> sentinel.

        Failure mode (pre-fix): second apply() returns identical bytes via the
        result cache, but mirror is skipped → Python store is empty → sentinel
        is SIGNALLED but UNRECOVERABLE.

        Expected (post-fix): ``_ensure_ccr_backed`` re-mirrors on the hit,
        Python store has the entry, sentinel is retrievable.
        """
        messages = _make_messages(_log_rows(90))
        tokenizer = _make_tokenizer()

        # Single router instance — its result cache persists across apply() calls.
        router = ContentRouter(ContentRouterConfig())

        # --- First apply: cold result cache → fresh compression runs, CCR mirrored ---
        r1 = router.apply(messages, tokenizer)
        out1 = _flatten_content(r1.messages)
        hashes1 = _extract_ccr_hashes(out1)

        if not hashes1:
            pytest.skip("No CCR drop produced for this fixture — cannot test invariant")

        cache_stats_after_r1 = dict(router._cache.stats)
        assert cache_stats_after_r1["cache_misses"] >= 1, (
            "Expected at least one result-cache miss on the first apply()"
        )

        # Precondition: first apply backed every sentinel.
        py_store = get_compression_store()
        for h in hashes1:
            assert py_store.retrieve(h) is not None, (
                f"Precondition: first apply left hash {h!r} unbacked"
            )

        # --- Simulate CCR TTL expiry: wipe the Python store only ---
        # The router's result cache (_cache) is NOT cleared — it lives on the
        # ContentRouter object and is only bounded by its own 30-min TTL.
        reset_compression_store()
        py_store = get_compression_store()

        # Confirm wipe cleared every backed entry.
        for h in hashes1:
            assert py_store.retrieve(h) is None, (
                f"Store reset did not clear hash {h!r}"
            )

        # --- Second apply: must be a Tier-2 result-cache HIT ---
        r2 = router.apply(messages, tokenizer)
        out2 = _flatten_content(r2.messages)

        cache_stats_after_r2 = dict(router._cache.stats)
        hits_delta = cache_stats_after_r2["cache_hits"] - cache_stats_after_r1["cache_hits"]
        assert hits_delta > 0, (
            f"Expected a result-cache HIT on the second apply() of identical content; "
            f"got hits_delta={hits_delta}. The test premise does not hold."
        )

        # The served output must be the same bytes (same sentinel hashes).
        assert out1 == out2, (
            "Result-cache HIT returned different bytes — unexpected. "
            "Check fixture determinism or ContentRouter caching logic."
        )

        hashes2 = _extract_ccr_hashes(out2)
        assert hashes2 == hashes1, "Served output changed its CCR hashes unexpectedly"

        # --- Invariant: every served hash must now be backed in the Python store ---
        for h in hashes2:
            entry = py_store.retrieve(h)
            assert entry is not None, (
                f"INVARIANT VIOLATED: hash {h!r} is in the served output (<<ccr:{h}>>)"
                f" but NOT in the Python compression_store after a result-cache HIT. "
                f"The sentinel is signalled-but-unrecoverable (P0 silent data loss)."
            )
            assert entry.original_content, (
                f"hash {h!r}: CCR entry present but original_content is empty"
            )

    def test_multiple_resets_invariant_holds(self):
        """Repeated CCR expiry + result-cache-hit cycles all stay backed."""
        messages = _make_messages(_log_rows(90))
        tokenizer = _make_tokenizer()
        router = ContentRouter(ContentRouterConfig())

        # Warm the result cache.
        r0 = router.apply(messages, tokenizer)
        hashes0 = _extract_ccr_hashes(_flatten_content(r0.messages))
        if not hashes0:
            pytest.skip("No CCR drop produced — cannot test invariant")

        for cycle in range(3):
            reset_compression_store()
            py_store = get_compression_store()

            r = router.apply(messages, tokenizer)
            out = _flatten_content(r.messages)
            served_hashes = _extract_ccr_hashes(out)

            for h in served_hashes:
                entry = py_store.retrieve(h)
                assert entry is not None, (
                    f"Cycle {cycle}: hash {h!r} unbacked after reset + result-cache hit"
                )

    def test_no_ccr_sentinel_is_cheap_noop(self):
        """Content producing no CCR drop must not error on the re-mirror path."""
        # Tiny items compress losslessly — no drop, no <<ccr: sentinel.
        tiny_messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        tokenizer = _make_tokenizer()
        router = ContentRouter(ContentRouterConfig())

        r1 = router.apply(tiny_messages, tokenizer)
        r2 = router.apply(tiny_messages, tokenizer)

        out1 = _flatten_content(r1.messages)
        out2 = _flatten_content(r2.messages)
        assert not _extract_ccr_hashes(out1)
        assert not _extract_ccr_hashes(out2)

    def test_ensure_ccr_backed_method_present(self):
        """Smoke: ``_ensure_ccr_backed`` is on ContentRouter and callable."""
        router = ContentRouter(ContentRouterConfig())
        assert hasattr(router, "_ensure_ccr_backed"), (
            "_ensure_ccr_backed missing from ContentRouter — fix not applied"
        )
        # No-op when no sentinel present — must not raise.
        router._ensure_ccr_backed("plain text, no sentinels", "")
        # Must not raise even with a sentinel pattern if SmartCrusher is loaded.
        router._ensure_ccr_backed("<<ccr:deadbeef01234567>>", "some query")

    def test_result_cache_hit_confirmed_on_second_apply(self):
        """Confirm the result cache is actually hit: ``cache_hits`` increments."""
        messages = _make_messages(_log_rows(90))
        tokenizer = _make_tokenizer()
        router = ContentRouter(ContentRouterConfig())

        router.apply(messages, tokenizer)  # warms cache
        stats_before = dict(router._cache.stats)

        router.apply(messages, tokenizer)  # must hit cache
        stats_after = dict(router._cache.stats)

        hits_delta = stats_after["cache_hits"] - stats_before["cache_hits"]
        assert hits_delta > 0, (
            f"Expected result-cache hit on second apply(); got hits_delta={hits_delta}. "
            "The Tier-2 result cache may not be engaged for this content shape."
        )

    # ------------------------------------------------------------------ #
    # BOTH-EXPIRED hardening: Rust + Python CCR stores both gone (300 s TTL)
    # while the result cache (30-min TTL) still holds the crushed output.
    # The strengthened fix must NOT serve a dead pointer — it recomputes.
    # ------------------------------------------------------------------ #

    def test_both_stores_expired_recomputes_and_rebacks(self):
        """Both CCR stores expired + result-cache HIT → recompute re-backs.

        Failure mode (pre-strengthening): ``_ensure_ccr_backed`` re-mirror finds
        nothing in the Rust store (also expired), the no-op leaves the sentinel
        unbacked, and the stale cached output is served anyway → silent loss.

        Expected (post-strengthening): the unbackable sentinel is detected, the
        cache entry evicted, and a fresh compress() recomputes + re-stores the
        CCR backing → the served sentinel resolves.
        """
        messages = _make_messages(_log_rows(90))
        tokenizer = _make_tokenizer()
        router = ContentRouter(ContentRouterConfig())

        # First apply: cold cache → fresh compress, both stores backed.
        r1 = router.apply(messages, tokenizer)
        out1 = _flatten_content(r1.messages)
        hashes1 = _extract_ccr_hashes(out1)
        if not hashes1:
            pytest.skip("No CCR drop produced for this fixture — cannot test invariant")

        crusher = router._get_smart_crusher()
        assert crusher is not None, "SmartCrusher must be available for this test"
        assert crusher.ccr_len() >= 1, "Rust store should hold the backing after first apply"

        # Simulate BOTH stores expired:
        #   - Python store wiped
        #   - Rust ccr_get returns None until a fresh crush() re-stores
        reset_compression_store()
        real_rust = crusher._rust
        shim = _ExpiringRustShim(real_rust)
        crusher._rust = shim
        try:
            # Second apply: result-cache HIT, but the served sentinel is
            # unbackable from either store → the fix must recompute.
            r2 = router.apply(messages, tokenizer)
        finally:
            crusher._rust = real_rust

        out2 = _flatten_content(r2.messages)
        hashes2 = _extract_ccr_hashes(out2)
        assert hashes2, "Recomputed output must still surface a recovery sentinel"

        # The recompute must have run (the shim's crush() flips _restored).
        assert shim._restored, (
            "Expected a recompute (fresh crush()) after the unbackable cache hit; "
            "the fix did not fall through to the recompute path."
        )

        # Invariant: every served sentinel resolves in the Python store again.
        py_store = get_compression_store()
        for h in hashes2:
            entry = py_store.retrieve(h)
            assert entry is not None, (
                f"BOTH-EXPIRED INVARIANT VIOLATED: served sentinel <<ccr:{h}>> is "
                f"NOT backed after the cache hit. The fix served a dead pointer "
                f"instead of recomputing."
            )
            assert entry.original_content, (
                f"hash {h!r}: recomputed CCR entry has empty original_content"
            )

    def test_both_stores_expired_does_not_serve_stale_pointer(self):
        """The served output after both-expired must be a backed recompute.

        Strong form: the result cache must be re-populated with a FRESH entry
        (the stale one evicted then re-put by the recompute), the recompute must
        have run (shim flips ``_restored``), and every served sentinel resolves.
        """
        messages = _make_messages(_log_rows(90))
        tokenizer = _make_tokenizer()
        router = ContentRouter(ContentRouterConfig())

        r1 = router.apply(messages, tokenizer)
        if not _extract_ccr_hashes(_flatten_content(r1.messages)):
            pytest.skip("No CCR drop produced — cannot test invariant")

        crusher = router._get_smart_crusher()

        reset_compression_store()
        real_rust = crusher._rust
        shim = _ExpiringRustShim(real_rust)
        crusher._rust = shim
        try:
            r2 = router.apply(messages, tokenizer)
        finally:
            crusher._rust = real_rust

        # The fix must have fallen through to a recompute (fresh crush()).
        assert shim._restored, (
            "Expected a recompute (fresh crush()) after the unbackable cache hit; "
            "the fix served the stale cached output instead of recomputing."
        )

        # The cache must still hold a (freshly re-populated) entry for reuse.
        assert router._cache.size >= 1, (
            "Recompute should re-populate the result cache with a fresh, backed entry"
        )

        py_store = get_compression_store()
        served = _extract_ccr_hashes(_flatten_content(r2.messages))
        assert served, "Recomputed output must still surface a recovery sentinel"
        for h in served:
            assert py_store.retrieve(h) is not None, (
                f"served sentinel <<ccr:{h}>> unbacked after both-expired recompute"
            )
