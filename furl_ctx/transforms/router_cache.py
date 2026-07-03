"""Two-tier compression result cache for the ContentRouter.

Extracted from ``content_router.py`` as a focused, self-contained module.
``CompressionCache`` has no dependency on ``ContentRouter`` — it is a
plain in-process dict cache keyed by an opaque hashable value the CALLER
builds. The router builds ``(hash(content), len(content), runtime, bias)``
tuples (see ``content_router._result_cache_key``), so dict key equality —
not just the 64-bit hash — verifies content length and the per-request
options before a hit is served (COR-18: a bare ``hash(content)`` key both
ignored per-request options and served another message's bytes on a SipHash
collision).

THREAD-SAFETY (COR-21): this cache IS shared across threads. The pipeline is
a process-wide singleton (``compress._get_pipeline``) and the MCP server runs
``compress()`` on an executor thread, so concurrent ``apply()`` calls hit one
instance. (An earlier header claimed all access happened on the main thread
and forbade adding a lock; that was wrong — two threads hitting the same
expired key both passed the non-None check and both ``del``'d, a live
``KeyError`` crash on the hot path.) All state — both tiers, the sweep
counter, and the metric counters — is guarded by a single ``threading.Lock``,
and expiry evictions use atomic ``pop(key, None)`` (never ``del``), so an
entry that vanishes is a no-op rather than a crash. Because the metric
counters are only ever updated under the lock, ``stats`` is exact — the
benign lost-increment races of the lock-free design are gone.

MEMORY BOUND (PERF-11): TTL eviction alone is lazy per key, which would let
unique content — the common case for tool outputs, inserted once and never
looked up again — leak forever in a long-lived MCP server. Two mechanisms
bound growth:

* an opportunistic sweep every ``_SWEEP_EVERY_N_INSERTIONS`` insertions
  drops every expired entry in both tiers (steady-state bound: entries
  younger than TTL, plus at most one sweep interval of stragglers);
* a FIFO cap of ``max_entries`` per tier evicts the oldest-inserted entry on
  overflow (hard bound against a burst of unique content inside one TTL
  window). Re-marking an existing skip key refreshes its timestamp but keeps
  its dict position, so a re-marked key can be FIFO-evicted early — the only
  cost is one redundant recompute attempt later; the cache is advisory.

CLOCK: expiry math uses ``time.monotonic()`` — a wall-clock step (NTP) under
``time.time()`` could spuriously expire or immortalize entries. Timestamps
never leave this class, so the monotonic epoch is an internal detail.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Hashable
from dataclasses import dataclass
from typing import Any

# Opaque cache key contract: the cache never inspects key structure — equality
# and hash are all it needs. Callers own key construction (and therefore own
# how much identity — content, length, options — a key encodes).
CacheKey = Hashable


@dataclass(frozen=True)
class ServeOriginal:
    """Serve the original message/block unchanged — the two-tier cache says this
    content will not compress: a Tier-1 skip hit, or a Tier-2 entry whose ratio
    no longer clears ``min_ratio`` and was relocated to the skip set."""


@dataclass(frozen=True)
class ServeCached:
    """Serve a live cached compression whose ``<<ccr:HASH>>`` sentinels (if any)
    are confirmed still backed. The caller swaps in ``compressed`` and formats
    the transform string — the flat (string-path) and label-threaded
    (block-path) formats differ, so formatting stays in the caller."""

    compressed: str
    strategy: str
    ratio: float


@dataclass(frozen=True)
class Recompute:
    """Cache miss, or a stale Tier-2 entry whose CCR backing has expired and was
    evicted. The caller (re)compresses — inline on the block path, deferred to
    the batched parallel pass on the string path."""


# A two-tier cache lookup resolves to exactly one of three dispositions. The two
# empty variants carry no data, so they are shared module singletons: the hot
# path resolves one per message and must not allocate for the common
# serve-original / recompute cases. ``ServeCached`` holds per-entry payload and
# stays fresh. (The ADT lives HERE — the cache's disposition language — so the
# block walker can match on it without importing ``content_router``, which
# re-exports every name for back-compat.)
CacheDisposition = ServeOriginal | ServeCached | Recompute
_SERVE_ORIGINAL = ServeOriginal()
_RECOMPUTE = Recompute()

# Sweep both tiers for expired entries once per this many insertions.
# Amortized cost per insertion is O(max_entries / interval) timestamp
# comparisons — noise next to the compression each insertion fronts — while
# dead entries linger at most one interval past their TTL.
_SWEEP_EVERY_N_INSERTIONS = 256

# Hard per-tier entry cap (FIFO): the backstop for a burst of unique content
# inside one TTL window, which the periodic sweep alone cannot bound. Sized
# for a modest worst case: 4096 result entries at ~10 KB of compressed text
# each is ~40 MB; skip entries (key + float) are negligible.
DEFAULT_MAX_ENTRIES_PER_TIER = 4096


class CompressionCache:
    """Two-tier compression cache with TTL. Thread-safe (single lock).

    Tier 1 (skip set): content hashes that won't compress — instant skip,
    near-zero memory (just keys and timestamps).

    Tier 2 (result cache): compressed results for content that DID compress —
    reuse the compressed text on subsequent requests.

    Entries expire after TTL (default 30min). Expired entries are reclaimed
    lazily on lookup, in bulk by an insertion-driven sweep, and bounded by a
    FIFO cap of ``max_entries`` per tier — see the module docstring for the
    exact bounds (COR-21 / PERF-11).

    Uses in-process dicts for fast lookups (~100ns plus one uncontended lock
    acquisition). Could be backed by memcached/Redis for multi-process
    deployments.
    """

    def __init__(
        self,
        ttl_seconds: int = 1800,
        max_entries: int = DEFAULT_MAX_ENTRIES_PER_TIER,
    ):
        # Guards both tiers, the sweep counter, and the metric counters. The
        # pipeline singleton is shared across executor threads (see module
        # docstring), so lock-free access here was a live crash (COR-21).
        self._lock = threading.Lock()
        # Tier 2: compressed results {key: (text, ratio, strategy, monotonic ts)}
        self._results: dict[CacheKey, tuple[str, float, str, float]] = {}
        # Tier 1: keys of content that won't compress {key: monotonic ts}
        self._skip: dict[CacheKey, float] = {}
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._insertions_since_sweep = 0
        # Metrics — updated under the lock, so they are exact (not the
        # benignly-racy best-effort counters of the old lock-free design).
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
        with self._lock:
            entry = self._results.get(key)
            if entry is not None:
                compressed, ratio, strategy, created_at = entry
                if (time.monotonic() - created_at) < self._ttl_seconds:
                    self._hits += 1
                    self._total_lookup_ns += time.perf_counter_ns() - t0
                    self._lookup_count += 1
                    return (compressed, ratio, strategy)
                # Expired. Atomic pop, never `del`: belt-and-braces for the
                # COR-21 crash shape — a key that vanished since the read
                # above is a no-op, not a KeyError.
                self._results.pop(key, None)
                self._evictions += 1
            self._misses += 1
            self._total_lookup_ns += time.perf_counter_ns() - t0
            self._lookup_count += 1
            return None

    def is_skipped(self, key: CacheKey) -> bool:
        """Check if content is known non-compressible (Tier 1)."""
        with self._lock:
            ts = self._skip.get(key)
            if ts is not None:
                if (time.monotonic() - ts) < self._ttl_seconds:
                    self._skip_hits += 1
                    return True
                # Expired — atomic pop (COR-21, same shape as get()).
                self._skip.pop(key, None)
                self._evictions += 1
            return False

    def put(self, key: CacheKey, compressed: str, ratio: float, strategy: str) -> None:
        """Store a compressed result (Tier 2)."""
        with self._lock:
            self._results[key] = (compressed, ratio, strategy, time.monotonic())
            self._evict_over_cap_locked(self._results)
            self._count_insertion_and_maybe_sweep_locked()

    def mark_skip(self, key: CacheKey) -> None:
        """Mark content as non-compressible (Tier 1)."""
        with self._lock:
            self._skip[key] = time.monotonic()
            self._evict_over_cap_locked(self._skip)
            self._count_insertion_and_maybe_sweep_locked()

    def move_to_skip(self, key: CacheKey) -> None:
        """Move a result to skip set (threshold tightened, no longer qualifies)."""
        with self._lock:
            self._results.pop(key, None)
            self._skip[key] = time.monotonic()
            self._evict_over_cap_locked(self._skip)
            self._count_insertion_and_maybe_sweep_locked()

    def invalidate(self, key: CacheKey) -> None:
        """Drop a result entry without marking it skipped.

        Used when a cached crushed output can no longer be safely served (its
        ``<<ccr:HASH>>`` backing is gone and cannot be re-created from the
        cache hit alone). The caller falls through to a fresh compress(), which
        re-creates and re-stores the CCR backing and re-populates this cache.
        """
        with self._lock:
            self._results.pop(key, None)

    def _evict_over_cap_locked(self, tier: dict[CacheKey, Any]) -> None:
        """FIFO-evict oldest-inserted entries while *tier* exceeds the cap.

        Caller must hold ``self._lock``. Dicts iterate in insertion order,
        and Tier-2 entries are never re-inserted on hit, so FIFO order is
        creation order there; Tier-1 re-marks can lag (see module docstring).
        """
        while len(tier) > self._max_entries:
            tier.pop(next(iter(tier)), None)
            self._evictions += 1

    def _count_insertion_and_maybe_sweep_locked(self) -> None:
        """Advance the insertion counter; sweep when the interval elapses.

        Caller must hold ``self._lock``. Insertions (not lookups) drive the
        sweep because the leak this bounds is insert-only traffic: unique
        content is inserted once and never looked up again, so lazy per-key
        eviction alone never reclaims it (PERF-11).
        """
        self._insertions_since_sweep += 1
        if self._insertions_since_sweep >= _SWEEP_EVERY_N_INSERTIONS:
            self._insertions_since_sweep = 0
            self._sweep_expired_locked()

    def _sweep_expired_locked(self) -> None:
        """Drop every expired entry in both tiers. Caller must hold the lock.

        Expired keys are snapshotted before popping (no mutation while
        iterating), and the pops tolerate already-vanished keys.
        """
        now = time.monotonic()
        expired_results = [
            key
            for key, (_, _, _, created_at) in self._results.items()
            if (now - created_at) >= self._ttl_seconds
        ]
        for key in expired_results:
            self._results.pop(key, None)
        expired_skips = [
            key for key, marked_at in self._skip.items() if (now - marked_at) >= self._ttl_seconds
        ]
        for key in expired_skips:
            self._skip.pop(key, None)
        self._evictions += len(expired_results) + len(expired_skips)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._results)

    @property
    def skip_size(self) -> int:
        with self._lock:
            return len(self._skip)

    @property
    def stats(self) -> dict[str, int | float]:
        with self._lock:
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
        with self._lock:
            self._results.clear()
            self._skip.clear()
            self._insertions_since_sweep = 0
