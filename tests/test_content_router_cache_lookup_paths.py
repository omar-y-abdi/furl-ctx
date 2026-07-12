"""Characterization pins for the ``ContentRouter.apply()`` string-path cache lookup.

The string-message path in ``ContentRouter.apply()`` runs a two-tier cache
lookup before deciding what to do with each message:

    key(content,  ─▶ Tier-1 is_skipped?  ─▶ skip (serve unchanged)
    runtime,      ─▶ Tier-2 get()        ─▶ ratio >= min_ratio? ─▶ move_to_skip (serve unchanged)
    bias)                                 ─▶ ratio <  min_ratio? ─▶ ccr-backed? ─▶ serve cached
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
via its public API (``mark_skip`` / ``put``). Keys are built with the router's
own ``_result_cache_key(content, bias)`` (COR-18: the key carries the
per-request bias and a content-length guard, not ``hash(content)`` alone);
a no-kwargs ``apply()`` resolves to bias ``1.0``, so ``_seed_key(content)``
below is the exact key the lookup uses (stable within a
process). No compression actually runs in either test — the lookup
short-circuits before ``compress()`` — so these pins are fast and deterministic
with no domain mocking.
"""

from __future__ import annotations

from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import EstimatingTokenCounter
from furl_ctx.transforms.content_router import (
    ContentRouter,
    ContentRouterConfig,
    Recompute,
    ServeCached,
    ServeOriginal,
    _result_cache_key,
)


def _seed_key(content: str):
    """The exact key a no-kwargs ``apply()`` builds for *content*: neutral
    bias 1.0."""
    return _result_cache_key(content, 1.0)


def _make_tokenizer() -> Tokenizer:
    return Tokenizer(EstimatingTokenCounter())


def _routable_tool_content() -> str:
    """Deterministic tool output that clears every pre-cache protection gate.

    Plain prose (not source code → no recent-code/analysis protection), no
    error indicators (not error-protected), no ``<<ccr:`` marker (not
    already-compressed pinning), and well over the 50-token raw-``apply()``
    floor — so execution reaches the Tier-1/Tier-2 cache lookup at
    ``content_key = _result_cache_key(content, bias)``.
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
    tree), where ``text`` is keyed verbatim via ``_result_cache_key``.
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
        router._cache.mark_skip(_seed_key(content))
        before = dict(router._cache.stats)

        result = router.apply([_tool_message(content)], _make_tokenizer())

        # Served byte-for-byte unchanged — no compression transform emitted.
        assert result.messages[0]["content"] == content
        assert result.transforms_applied == ["router:noop:no_savings"]

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
        router._cache.put(_seed_key(content), stale_payload, 0.99, "log")
        assert router._cache.size == 1
        assert router._cache.skip_size == 0
        before = dict(router._cache.stats)

        result = router.apply([_tool_message(content)], _make_tokenizer())

        # Original served, stale payload withheld, no compression transform.
        assert result.messages[0]["content"] == content
        assert stale_payload not in result.messages[0]["content"]
        assert result.transforms_applied == ["router:noop:no_savings"]

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

    ``text`` is the ``tool_result`` block's ``content``, keyed verbatim by
    ``_compress_content_block`` via ``_result_cache_key``, so ``_seed_key(text)``
    is the exact lookup key.
    """

    def _served_block_text(self, result) -> str:
        return result.messages[0]["content"][0]["content"]

    def test_tier1_skip_hit_serves_block_unchanged(self):
        """Tier-1 skip hit on the block path: tool_result served verbatim."""
        text = _routable_block_content()
        router = ContentRouter(ContentRouterConfig())

        router._cache.mark_skip(_seed_key(text))
        before = dict(router._cache.stats)

        result = router.apply([_tool_result_message(text)], _make_tokenizer())

        # Block content unchanged, no compression transform emitted.
        assert self._served_block_text(result) == text
        assert result.transforms_applied == ["router:noop:no_savings"]

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
        router._cache.put(_seed_key(text), stale_payload, 0.99, "log")
        assert router._cache.size == 1
        assert router._cache.skip_size == 0
        before = dict(router._cache.stats)

        result = router.apply([_tool_result_message(text)], _make_tokenizer())

        # Original served, stale payload withheld, no compression transform.
        assert self._served_block_text(result) == text
        assert stale_payload not in self._served_block_text(result)
        assert result.transforms_applied == ["router:noop:no_savings"]

        # Entry relocated: result cache → skip set; the get() was a hit.
        assert router._cache.size == 0
        assert router._cache.skip_size == 1
        assert dict(router._cache.stats)["cache_hits"] - before["cache_hits"] == 1


class _CapturingObserver:
    """Duck-typed ``CompressionObserver`` (the router takes ``observer: Any``)
    that records the ``route_counts`` dict forwarded at the end of ``apply()``.

    Both methods the router may call are present: ``record_router_route_counts``
    (fired once per ``apply()`` with the merged counters) is captured as a COPY
    — the router holds a live mutable ref — and ``record_compression`` (fired
    during any recompute) is an inert no-op so the stale/miss outcomes, which do
    run compression, never ``AttributeError``.
    """

    def __init__(self) -> None:
        self.route_counts: dict[str, int] | None = None

    def record_router_route_counts(self, counts: dict[str, int]) -> None:
        self.route_counts = dict(counts)

    def record_compression(self, *args: object, **kwargs: object) -> None:
        return None


def _served_block_text(result) -> str:
    return result.messages[0]["content"][0]["content"]


class TestCacheLookupRouteCounts:
    """Characterization pins on the ``route_counts`` dict — the SECOND observable
    surface of the two-tier lookup (the first, ``_cache.stats``, is pinned above).

    ``route_counts`` is what the lookup forwards to the observer and ``/stats``;
    it is the dict where the string-path and content-block copies of the decision
    tree must agree exactly. These lock the per-outcome counter deltas BEFORE the
    lookup half is extracted into one ``_lookup_cached_disposition``, so a rewire
    that double-counts (a bump left in the caller after moving it into the shared
    fn) or drops a counter fails here. Every pure-lookup outcome — the three that
    short-circuit before ``compress()`` runs — is covered on BOTH paths.

    Exact counts (``== 1``, not ``>= 1``) are the point: double-counting is the
    dominant rewire failure mode, and only an exact assertion catches it.
    """

    # --- string path (apply() inline) -------------------------------------

    def test_string_skip_hit_route_counts(self):
        content = _routable_tool_content()
        obs = _CapturingObserver()
        router = ContentRouter(ContentRouterConfig(), observer=obs)
        router._cache.mark_skip(_seed_key(content))

        router.apply([_tool_message(content)], _make_tokenizer())

        assert obs.route_counts is not None
        rc = obs.route_counts
        assert rc.get("cache_hit", 0) == 1
        assert rc["ratio_too_high"] == 1
        assert rc.get("cache_miss", 0) == 0
        assert rc.get("cache_stale_recompute", 0) == 0

    def test_string_tightened_route_counts(self):
        content = _routable_tool_content()
        obs = _CapturingObserver()
        router = ContentRouter(ContentRouterConfig(), observer=obs)
        router._cache.put(_seed_key(content), "STALE", 0.99, "log")

        router.apply([_tool_message(content)], _make_tokenizer())

        rc = obs.route_counts
        assert rc is not None
        # Tightened relocates to skip and serves original: hit + ratio_too_high,
        # never a miss.
        assert rc.get("cache_hit", 0) == 1
        assert rc["ratio_too_high"] == 1
        assert rc.get("cache_miss", 0) == 0

    def test_string_serve_cached_route_counts(self):
        """Live cached entry (ratio below the bar, no CCR sentinel to back) is
        served: exactly one ``cache_hit``, and — unlike the two skip outcomes —
        NO ``ratio_too_high``."""
        content = _routable_tool_content()
        obs = _CapturingObserver()
        router = ContentRouter(ContentRouterConfig(), observer=obs)
        payload = "PRECOMPRESSED-PAYLOAD-NO-CCR-MARKERS"
        router._cache.put(_seed_key(content), payload, 0.3, "log")

        result = router.apply([_tool_message(content)], _make_tokenizer())

        # The cached payload was served (proves the serve-cached branch, not a
        # tightened relocate or a recompute).
        assert result.messages[0]["content"] == payload
        # Drift-guard on the DIVERGENT surface the extraction deliberately keeps
        # in the caller: the string path formats a FLAT router:{strategy}:{ratio}
        # transform (WITH the ratio). This is the exact format whose difference
        # justifies NOT unifying the two callers — a "helpful" unification that
        # collapsed it to the block path's label form would fail here.
        assert "router:log:0.30" in result.transforms_applied
        rc = obs.route_counts
        assert rc is not None
        assert rc.get("cache_hit", 0) == 1
        assert rc["ratio_too_high"] == 0
        assert rc.get("cache_miss", 0) == 0
        assert rc.get("cache_stale_recompute", 0) == 0

    # --- content-block path (_compress_content_block) ---------------------

    def test_block_skip_hit_route_counts(self):
        text = _routable_block_content()
        obs = _CapturingObserver()
        router = ContentRouter(ContentRouterConfig(), observer=obs)
        router._cache.mark_skip(_seed_key(text))

        router.apply([_tool_result_message(text)], _make_tokenizer())

        rc = obs.route_counts
        assert rc is not None
        assert rc.get("cache_hit", 0) == 1
        assert rc["ratio_too_high"] == 1
        assert rc.get("cache_miss", 0) == 0
        assert rc["content_blocks"] == 1

    def test_block_tightened_route_counts(self):
        text = _routable_block_content()
        obs = _CapturingObserver()
        router = ContentRouter(ContentRouterConfig(), observer=obs)
        router._cache.put(_seed_key(text), "STALE", 0.99, "log")

        router.apply([_tool_result_message(text)], _make_tokenizer())

        rc = obs.route_counts
        assert rc is not None
        assert rc.get("cache_hit", 0) == 1
        assert rc["ratio_too_high"] == 1
        assert rc.get("cache_miss", 0) == 0
        assert rc["content_blocks"] == 1

    def test_block_serve_cached_route_counts(self):
        text = _routable_block_content()
        obs = _CapturingObserver()
        router = ContentRouter(ContentRouterConfig(), observer=obs)
        payload = "PRECOMPRESSED-BLOCK-PAYLOAD-NO-CCR-MARKERS"
        router._cache.put(_seed_key(text), payload, 0.3, "log")

        result = router.apply([_tool_result_message(text)], _make_tokenizer())

        assert _served_block_text(result) == payload
        # Drift-guard, block-path counterpart: the content-block path threads a
        # router:{label}:{strategy} transform (label, NO ratio) — the divergent
        # counterpart to the string path's flat router:{strategy}:{ratio}. Pinning
        # BOTH formats is what makes the deliberate non-unification drift-safe.
        assert "router:tool_result:log" in result.transforms_applied
        rc = obs.route_counts
        assert rc is not None
        assert rc.get("cache_hit", 0) == 1
        assert rc["ratio_too_high"] == 0
        assert rc.get("cache_miss", 0) == 0
        assert rc["content_blocks"] == 1


class TestLookupCachedDispositionDirect:
    """Direct unit coverage of the extracted ``_lookup_cached_disposition`` — the
    single home of the two-tier lookup decision and its data-loss guard, which
    both the string path (``apply``) and the content-block path
    (``_compress_content_block``) now route through.

    Each of the five outcomes is driven by pre-seeding the cache and calling the
    method directly, so NO compression runs — the method returns a disposition
    BEFORE any recompute. This is the strongest guard on the invariant *never
    serve a ``<<ccr:HASH>>`` sentinel whose backing has expired*: the unbackable
    case MUST evict and return ``Recompute``, never ``ServeCached``. Counter
    dicts are asserted whole (``==``) so a rewire that double-counts or drops a
    bump — the dominant refactor failure mode — fails here.
    """

    MIN_RATIO = 0.85

    def _router(self) -> ContentRouter:
        return ContentRouter(ContentRouterConfig())

    def test_skip_hit_returns_serve_original(self):
        router = self._router()
        key = hash("probe-skip")
        router._cache.mark_skip(key)
        rc: dict[str, int] = {}

        disp = router._lookup_cached_disposition(key, "", self.MIN_RATIO, rc)

        assert isinstance(disp, ServeOriginal)
        assert rc == {"ratio_too_high": 1, "cache_hit": 1}

    def test_tightened_relocates_and_returns_serve_original(self):
        router = self._router()
        key = hash("probe-tightened")
        router._cache.put(key, "STALE", 0.99, "log")
        rc: dict[str, int] = {}

        disp = router._lookup_cached_disposition(key, "", self.MIN_RATIO, rc)

        assert isinstance(disp, ServeOriginal)
        assert rc == {"ratio_too_high": 1, "cache_hit": 1}
        # Entry relocated result-cache → skip set.
        assert router._cache.size == 0
        assert router._cache.skip_size == 1

    def test_live_backed_returns_serve_cached_with_payload(self):
        router = self._router()
        key = hash("probe-serve")
        router._cache.put(key, "LIVE-PAYLOAD-NO-MARKERS", 0.3, "log")
        rc: dict[str, int] = {}

        disp = router._lookup_cached_disposition(key, "", self.MIN_RATIO, rc)

        assert isinstance(disp, ServeCached)
        assert disp.compressed == "LIVE-PAYLOAD-NO-MARKERS"
        assert disp.strategy == "log"
        assert disp.ratio == 0.3
        assert rc == {"cache_hit": 1}

    def test_unbackable_sentinel_evicts_and_returns_recompute(self):
        router = self._router()
        key = hash("probe-stale")
        # A sentinel pointing at a hash with NO live CCR backing (fresh router →
        # both stores empty). It must NOT be served: evict + recompute.
        router._cache.put(key, "head <<ccr:deadbeefdead>> tail", 0.3, "log")
        rc: dict[str, int] = {}

        disp = router._lookup_cached_disposition(key, "", self.MIN_RATIO, rc)

        assert isinstance(disp, Recompute)
        # The stale entry was evicted, not left to be re-served next call.
        assert router._cache.size == 0
        # Both counters bump on the stale path (stale_recompute AND miss).
        assert rc == {"cache_stale_recompute": 1, "cache_miss": 1}

    def test_plain_miss_returns_recompute(self):
        router = self._router()
        key = hash("probe-miss")
        rc: dict[str, int] = {}

        disp = router._lookup_cached_disposition(key, "", self.MIN_RATIO, rc)

        assert isinstance(disp, Recompute)
        assert rc == {"cache_miss": 1}

    def test_route_counts_none_is_tolerated(self):
        """The block path may pass ``route_counts=None`` (routing summary opted
        out); the lookup must still resolve without raising."""
        router = self._router()
        key = hash("probe-none")
        router._cache.mark_skip(key)

        disp = router._lookup_cached_disposition(key, "", self.MIN_RATIO, None)

        assert isinstance(disp, ServeOriginal)
