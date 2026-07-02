"""Two-tier compression result cache for the ContentRouter.

Extracted verbatim from ``content_router.py`` as a focused, self-contained
module. ``CompressionCache`` has no dependency on ``ContentRouter`` — it is a
plain in-process dict cache keyed by an opaque hashable value the CALLER
builds. The router builds ``(hash(content), len(content), runtime, bias)``
tuples (see ``content_router._result_cache_key``), so dict key equality —
not just the 64-bit hash — verifies content length and the per-request
options before a hit is served (COR-18: a bare ``hash(content)`` key both
ignored per-request options and served another message's bytes on a SipHash
collision).

THREAD-SAFETY NOTE (do not "fix" this): the cache is intentionally LOCK-FREE.
All access happens on the main thread (the ThreadPoolExecutor workers in the
router never touch the cache), so the lock-free design is correct and must be
preserved exactly. Adding a lock would be a behavior change, not a hardening.
"""

from __future__ import annotations

import time
from collections.abc import Hashable

# Opaque cache key contract: the cache never inspects key structure — equality
# and hash are all it needs. Callers own key construction (and therefore own
# how much identity — content, length, options — a key encodes).
CacheKey = Hashable


class CompressionCache:
    """Two-tier compression cache with TTL.

    Tier 1 (skip set): content hashes that won't compress — instant skip,
    near-zero memory (just ints in a set).

    Tier 2 (result cache): compressed results for content that DID compress —
    reuse the compressed text on subsequent requests.

    Entries expire after TTL (default 30min). No max-entries cap — TTL is the
    natural bound. Memory grows proportional to compressible content × TTL,
    which is bounded by session duration.

    Uses in-process dict for ultra-fast lookups (~100ns). Could be backed
    by memcached/Redis for multi-process deployments.
    """

    def __init__(self, ttl_seconds: int = 1800):
        # Tier 2: compressed results {key: (text, ratio, strategy, timestamp)}
        self._results: dict[CacheKey, tuple[str, float, str, float]] = {}
        # Tier 1: keys of content that won't compress {key: timestamp}
        self._skip: dict[CacheKey, float] = {}
        self._ttl_seconds = ttl_seconds
        # Metrics
        self._hits = 0
        self._misses = 0
        self._skip_hits = 0
        self._evictions = 0
        self._total_lookup_ns = 0
        self._lookup_count = 0

    def get(self, key: CacheKey) -> tuple[str, float, str] | None:
        """Get cached compression result.

        Returns (compressed_text, ratio, strategy) or None if not found/expired.
        Use is_skipped() first to check if content is known non-compressible.
        """
        t0 = time.perf_counter_ns()
        entry = self._results.get(key)
        if entry is not None:
            compressed, ratio, strategy, created_at = entry
            if (time.time() - created_at) < self._ttl_seconds:
                self._hits += 1
                self._total_lookup_ns += time.perf_counter_ns() - t0
                self._lookup_count += 1
                return (compressed, ratio, strategy)
            else:
                del self._results[key]
                self._evictions += 1
        self._misses += 1
        self._total_lookup_ns += time.perf_counter_ns() - t0
        self._lookup_count += 1
        return None

    def is_skipped(self, key: CacheKey) -> bool:
        """Check if content is known non-compressible (Tier 1)."""
        ts = self._skip.get(key)
        if ts is not None:
            if (time.time() - ts) < self._ttl_seconds:
                self._skip_hits += 1
                return True
            else:
                del self._skip[key]
                self._evictions += 1
        return False

    def put(self, key: CacheKey, compressed: str, ratio: float, strategy: str) -> None:
        """Store a compressed result (Tier 2)."""
        self._results[key] = (compressed, ratio, strategy, time.time())

    def mark_skip(self, key: CacheKey) -> None:
        """Mark content as non-compressible (Tier 1)."""
        self._skip[key] = time.time()

    def move_to_skip(self, key: CacheKey) -> None:
        """Move a result to skip set (threshold tightened, no longer qualifies)."""
        self._results.pop(key, None)
        self._skip[key] = time.time()

    def invalidate(self, key: CacheKey) -> None:
        """Drop a result entry without marking it skipped.

        Used when a cached crushed output can no longer be safely served (its
        ``<<ccr:HASH>>`` backing is gone and cannot be re-created from the
        cache hit alone). The caller falls through to a fresh compress(), which
        re-creates and re-stores the CCR backing and re-populates this cache.
        """
        self._results.pop(key, None)

    @property
    def size(self) -> int:
        return len(self._results)

    @property
    def skip_size(self) -> int:
        return len(self._skip)

    @property
    def stats(self) -> dict[str, int | float]:
        avg_ns = self._total_lookup_ns / self._lookup_count if self._lookup_count else 0
        return {
            "cache_hits": self._hits,
            "cache_skip_hits": self._skip_hits,
            "cache_misses": self._misses,
            "cache_evictions": self._evictions,
            "cache_size": len(self._results),
            "cache_skip_size": len(self._skip),
            "cache_avg_lookup_ns": avg_ns,
        }

    def clear(self) -> None:
        """Clear all entries (e.g., on session end)."""
        self._results.clear()
        self._skip.clear()
