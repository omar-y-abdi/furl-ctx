"""Characterization pins for the ``ContentRouter.apply()`` string-path cache lookup.

The string-message path in ``ContentRouter.apply()`` runs a two-tier cache
lookup before deciding what to do with each message:

    hash(content) ─▶ Tier-1 is_skipped?  ─▶ skip (serve unchanged)
                  ─▶ Tier-2 get()        ─▶ ratio >= min_ratio? ─▶ move_to_skip (serve unchanged)
                                          ─▶ ratio <  min_ratio? ─▶ ccr-backed? ─▶ serve cached
                                                                                 ─▶ evict + recompute
                  ─▶ miss                ─▶ recompute (deferred to the parallel pass)

Three of those five outcomes — plain-miss→serve, serve-cached, and
stale-recompute (both CCR stores expired) — are already pinned end-to-end in
``test_result_cache_ccr_divergence.py`` with real compression fixtures.

This file closes the remaining two, which had no coverage:

  * **skip-skipped** (Tier-1 hit): content previously judged non-compressible is
    rejected instantly and served byte-for-byte unchanged.
  * **tightened** (Tier-2 hit, ratio no longer qualifies): a result cached when
    the acceptance bar was looser must NOT be served once ``cached_ratio`` is at
    or above the current ``min_ratio`` — it is relocated to the skip set and the
    original content is served unchanged.

Both are driven through the public ``apply()`` surface with the cache pre-seeded
via its public API (``mark_skip`` / ``put``). ``content`` is hashed verbatim by
``apply()``, so ``hash(content)`` here is the exact key the lookup uses
(``hash`` is stable within a process). No compression actually runs in either
test — the lookup short-circuits before ``compress()`` — so these pins are fast
and deterministic with no domain mocking.
"""

from __future__ import annotations

from headroom.tokenizer import Tokenizer
from headroom.tokenizers import EstimatingTokenCounter
from headroom.transforms.content_router import ContentRouter, ContentRouterConfig


def _make_tokenizer() -> Tokenizer:
    return Tokenizer(EstimatingTokenCounter())


def _routable_tool_content() -> str:
    """Deterministic tool output that clears every pre-cache protection gate.

    Plain prose (not source code → no recent-code/analysis protection), no
    error indicators (not error-protected), no ``<<ccr:`` marker (not
    already-compressed pinning), and well over the 50-token raw-``apply()``
    floor — so execution reaches the Tier-1/Tier-2 cache lookup at
    ``content_key = hash(content)``.
    """
    return " ".join(
        f"Line {i}: the quarterly summary recorded steady throughput and "
        f"nominal latency across region {i % 5} with no anomalies noted."
        for i in range(12)
    )


def _tool_message(content: str) -> dict:
    # A non-excluded tool id (no assistant tool_calls map it to Read/Glob), so
    # the message is compression-eligible rather than excluded.
    return {"role": "tool", "content": content, "tool_call_id": "call_pin"}


def _routable_block_content() -> str:
    """Deterministic tool_result text that clears the content-block gates.

    Over the 500-char ``min_chars_for_block_compression`` floor, no error
    indicators, no ``<<ccr:`` marker — so a ``tool_result`` block carrying it
    reaches ``_compress_content_block`` (the content-block copy of the lookup
    tree), where ``text`` is hashed verbatim.
    """
    return " ".join(
        f"Record {i}: throughput {1000 + i} rps, latency {10 + i % 7} ms, "
        f"region {i % 5}, status nominal, notes none for this interval."
        for i in range(20)
    )


def _tool_result_message(text: str) -> dict:
    # Canonical Anthropic shape: a tool_result block inside a user-role message.
    # The list-content dispatch routes it to _process_content_blocks regardless
    # of role; tool_result blocks compress freely (they are tool outputs).
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "tu_pin", "content": text}],
    }


class TestStringPathCacheLookup:
    def test_tier1_skip_hit_serves_unchanged(self):
        """Tier-1 skip hit: known-non-compressible content is served verbatim.

        Failure mode this guards: a regression that ignored the skip set would
        re-run compression on content already judged not worth compressing —
        wasted work, and (worse) a chance to mutate cache-hot bytes.
        """
        content = _routable_tool_content()
        router = ContentRouter(ContentRouterConfig())

        # Pre-seed Tier 1: this content is known non-compressible.
        router._cache.mark_skip(hash(content))
        before = dict(router._cache.stats)

        result = router.apply([_tool_message(content)], _make_tokenizer())

        # Served byte-for-byte unchanged — no compression transform emitted.
        assert result.messages[0]["content"] == content
        assert result.transforms_applied == ["router:noop"]

        after = dict(router._cache.stats)
        # The Tier-1 skip set was consulted and hit exactly once...
        assert after["cache_skip_hits"] - before["cache_skip_hits"] == 1
        # ...and the Tier-2 result lookup was never reached: neither a hit nor a
        # miss was recorded (a regression that ignored Tier-1 would fall through
        # to get() → a miss → recompute, flipping both of these deltas).
        assert after["cache_hits"] - before["cache_hits"] == 0
        assert after["cache_misses"] - before["cache_misses"] == 0

    def test_tier2_tightened_relocates_to_skip_and_serves_unchanged(self):
        """Tier-2 hit whose ratio no longer qualifies: relocate, serve original.

        ``min_ratio`` defaults to ~0.85 at the moderate (0.5) context pressure
        used when no ``model_limit`` is supplied, so a cached entry at ratio
        0.99 is at/above the bar — the "threshold tightened" branch. The stale
        compressed payload must NOT be served; the entry moves from the result
        cache to the skip set so it is not re-evaluated next time.

        Failure mode this guards: serving a cached compression that no longer
        clears the acceptance bar (stale, low-value bytes), or thrashing —
        re-compressing the same content on every call.
        """
        content = _routable_tool_content()
        router = ContentRouter(ContentRouterConfig())

        stale_payload = "STALE-COMPRESSION-MUST-NOT-BE-SERVED"
        # Pre-seed Tier 2 with a ratio at/above any default min_ratio in [0, 1].
        router._cache.put(hash(content), stale_payload, 0.99, "log")
        assert router._cache.size == 1
        assert router._cache.skip_size == 0
        before = dict(router._cache.stats)

        result = router.apply([_tool_message(content)], _make_tokenizer())

        # Original served, stale payload withheld, no compression transform.
        assert result.messages[0]["content"] == content
        assert stale_payload not in result.messages[0]["content"]
        assert result.transforms_applied == ["router:noop"]

        # The entry was relocated: result cache → skip set.
        assert router._cache.size == 0
        assert router._cache.skip_size == 1
        # The Tier-2 get() was a hit (entry existed) before the relocation.
        assert dict(router._cache.stats)["cache_hits"] - before["cache_hits"] == 1


class TestContentBlockPathCacheLookup:
    """The same two lookup outcomes on the SECOND copy of the decision tree:
    ``_compress_content_block`` (Anthropic content-block path).

    The string path (``apply()`` inline) and this block path are deliberately
    NOT merged (see the comment at the string-path lookup site: the string path
    defers recompute to a batched parallel pass, the block path compresses
    inline, and their transform-string formats differ). Banking that
    duplication is only drift-safe if BOTH copies are pinned — the string copy
    is covered by ``TestStringPathCacheLookup`` above; these pin the block copy.

    ``text`` is the ``tool_result`` block's ``content``, hashed verbatim by
    ``_compress_content_block``, so ``hash(text)`` is the exact lookup key.
    """

    def _served_block_text(self, result) -> str:
        return result.messages[0]["content"][0]["content"]

    def test_tier1_skip_hit_serves_block_unchanged(self):
        """Tier-1 skip hit on the block path: tool_result served verbatim."""
        text = _routable_block_content()
        router = ContentRouter(ContentRouterConfig())

        router._cache.mark_skip(hash(text))
        before = dict(router._cache.stats)

        result = router.apply([_tool_result_message(text)], _make_tokenizer())

        # Block content unchanged, no compression transform emitted.
        assert self._served_block_text(result) == text
        assert result.transforms_applied == ["router:noop"]

        after = dict(router._cache.stats)
        # Tier-1 consulted and hit once; Tier-2 lookup never reached.
        assert after["cache_skip_hits"] - before["cache_skip_hits"] == 1
        assert after["cache_hits"] - before["cache_hits"] == 0
        assert after["cache_misses"] - before["cache_misses"] == 0

    def test_tier2_tightened_relocates_block_to_skip(self):
        """Tier-2 tightened on the block path: relocate, serve original block."""
        text = _routable_block_content()
        router = ContentRouter(ContentRouterConfig())

        stale_payload = "STALE-BLOCK-COMPRESSION-MUST-NOT-BE-SERVED"
        router._cache.put(hash(text), stale_payload, 0.99, "log")
        assert router._cache.size == 1
        assert router._cache.skip_size == 0
        before = dict(router._cache.stats)

        result = router.apply([_tool_result_message(text)], _make_tokenizer())

        # Original served, stale payload withheld, no compression transform.
        assert self._served_block_text(result) == text
        assert stale_payload not in self._served_block_text(result)
        assert result.transforms_applied == ["router:noop"]

        # Entry relocated: result cache → skip set; the get() was a hit.
        assert router._cache.size == 0
        assert router._cache.skip_size == 1
        assert dict(router._cache.stats)["cache_hits"] - before["cache_hits"] == 1
