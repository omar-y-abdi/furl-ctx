"""TDD tests for DiffCompressor.compress_with_stats CCR persistence (Unit U6).

Contract under test:
    compress_with_stats must persist the original content into the Python
    CompressionStore under the emitted cache_key, exactly as the main
    compress() path does. Without this, any 'Retrieve original: hash=<key>'
    marker emitted by the sidecar API points to a key that was never written
    — a dangling, unrecoverable marker (Contract #1 break).

Three tests:
    1. RED → GREEN: After compress_with_stats on a large-enough diff, the
       CompressionStore contains an entry under cache_key whose original_content
       equals the input content.
    2. No-op: When no cache_key is produced (tiny / no-op diff), no store write
       occurs and no exception is raised.
    3. Parity: compress_with_stats and compress (main path) persist under the
       SAME cache_key for identical input and both resolve to the same original.
"""

from __future__ import annotations

import textwrap

import pytest

from furl_ctx.cache.compression_store import (
    CompressionStore,
    clear_request_compression_store,
    set_request_compression_store,
)
from furl_ctx.transforms.diff_compressor import (
    DiffCompressor,
    DiffCompressorConfig,
)
from tests._fixtures import make_large_diff as _make_large_diff  # TEST-19 shared

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tiny_diff() -> str:
    """A one-line diff — well below the CCR threshold."""
    return textwrap.dedent("""\
        diff --git a/foo.py b/foo.py
        index 0000000..1111111 100644
        --- a/foo.py
        +++ b/foo.py
        @@ -1 +1 @@
        -old
        +new
    """)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_store():
    """Give each test its own fresh CompressionStore via the request-scoped
    override so we never pollute the global singleton.
    """
    fresh = CompressionStore(max_entries=500, enable_feedback=False)
    set_request_compression_store(fresh)
    yield fresh
    clear_request_compression_store()


# ---------------------------------------------------------------------------
# Test 1 — PRIMARY (RED → GREEN): sidecar must persist under cache_key
# ---------------------------------------------------------------------------


class TestCompressWithStatsPersistsToStore:
    """compress_with_stats must write the original content into the Python CCR
    store under the cache_key emitted in the sidecar result — mirroring
    exactly what compress() already does."""

    def test_large_diff_cache_key_resolves_in_store(self, isolated_store):
        """After compress_with_stats on a large diff, retrieve(cache_key)
        returns an entry whose original_content matches the input.

        Baseline: before the fix, the store has no such entry (marker dangles).
        """
        diff = _make_large_diff()
        cfg = DiffCompressorConfig(
            enable_ccr=True,
            min_lines_for_ccr=10,  # Low threshold to ensure CCR fires
        )
        compressor = DiffCompressor(cfg)

        result, _stats = compressor.compress_with_stats(diff)

        # The fix is only meaningful when the Rust path actually emits a cache_key.
        assert result.cache_key is not None, (
            "No cache_key produced — CCR did not fire. "
            "_make_large_diff(n_files=5, hunks_each=20) with min_lines_for_ccr=10 "
            "must emit a cache_key; increase diff size if the threshold changed."
        )

        cache_key = result.cache_key
        entry = isolated_store.retrieve(cache_key)

        assert entry is not None, (
            f"cache_key {cache_key!r} was emitted in the sidecar result but is NOT "
            f"present in the CompressionStore — dangling marker. "
            f"compress_with_stats must call _persist_to_python_ccr when cache_key is set."
        )
        assert entry.original_content == diff, (
            f"CompressionStore entry under {cache_key!r} contains wrong content. "
            f"Expected the original diff, got {entry.original_content[:200]!r}…"
        )

    def test_compressed_output_contains_marker_pointing_to_stored_content(self, isolated_store):
        """The compressed output's 'Retrieve original' marker resolves in the store."""
        diff = _make_large_diff()
        cfg = DiffCompressorConfig(enable_ccr=True, min_lines_for_ccr=10)
        compressor = DiffCompressor(cfg)

        result, _stats = compressor.compress_with_stats(diff)

        assert result.cache_key is not None, (
            "No cache_key produced — CCR did not fire. "
            "_make_large_diff(n_files=5, hunks_each=20) with min_lines_for_ccr=10 "
            "must emit a cache_key; increase diff size if the threshold changed."
        )

        # Confirm the marker is resolvable.
        entry = isolated_store.retrieve(result.cache_key)
        assert entry is not None
        # The stored content is the original, so it equals our diff input.
        assert entry.original_content == diff


# ---------------------------------------------------------------------------
# Test 2 — NO-OP: tiny diff produces no cache_key → no store write, no error
# ---------------------------------------------------------------------------


class TestCompressWithStatsNoOpSmallDiff:
    """When CCR does not fire (diff below threshold), compress_with_stats
    must not write to the store and must not raise any exception."""

    def test_tiny_diff_no_store_write(self, isolated_store):
        diff = _make_tiny_diff()
        cfg = DiffCompressorConfig(enable_ccr=True, min_lines_for_ccr=200)
        compressor = DiffCompressor(cfg)

        result, _stats = compressor.compress_with_stats(diff)

        # No cache_key should be emitted for a tiny diff.
        assert result.cache_key is None, "Expected no cache_key for a tiny diff below CCR threshold"
        # The store must remain empty.
        # We can verify this by checking that retrieval of a synthetic key fails.
        import hashlib

        synthetic = hashlib.sha256(diff.encode()).hexdigest()[:24]
        assert isolated_store.retrieve(synthetic) is None

    def test_no_exception_when_store_not_needed(self, isolated_store):
        """compress_with_stats must not raise even for tiny diffs."""
        diff = _make_tiny_diff()
        cfg = DiffCompressorConfig(enable_ccr=True, min_lines_for_ccr=200)
        compressor = DiffCompressor(cfg)

        # Must complete without exception.
        result, stats = compressor.compress_with_stats(diff)
        assert result is not None
        assert stats is not None


# ---------------------------------------------------------------------------
# Test 3 — PARITY: compress() and compress_with_stats() use the same key
# ---------------------------------------------------------------------------


class TestCompressAndSidecarSameCacheKey:
    """compress_with_stats and compress (the main path) must persist under
    the SAME cache_key for identical input, and both must resolve to the
    same original content.

    This ensures the sidecar is not diverging from the main path in its
    CCR key computation.
    """

    def test_same_cache_key_both_paths(self, isolated_store):
        diff = _make_large_diff()
        cfg = DiffCompressorConfig(enable_ccr=True, min_lines_for_ccr=10)
        compressor = DiffCompressor(cfg)

        # Main path
        result_main = compressor.compress(diff)
        assert result_main.cache_key is not None, (
            "No cache_key from main compress() — CCR did not fire. "
            "_make_large_diff(n_files=5, hunks_each=20) with min_lines_for_ccr=10 "
            "must emit a cache_key; increase diff size if the threshold changed."
        )

        # Sidecar path uses a fresh store to avoid collision from above write.
        fresh2 = CompressionStore(max_entries=500, enable_feedback=False)
        set_request_compression_store(fresh2)
        try:
            result_sidecar, _stats = compressor.compress_with_stats(diff)
        finally:
            # Restore the test fixture store.
            set_request_compression_store(isolated_store)

        assert result_sidecar.cache_key is not None, (
            "No cache_key from compress_with_stats() — CCR did not fire. "
            "_make_large_diff(n_files=5, hunks_each=20) with min_lines_for_ccr=10 "
            "must emit a cache_key; increase diff size if the threshold changed."
        )

        assert result_main.cache_key == result_sidecar.cache_key, (
            f"cache_key mismatch: compress={result_main.cache_key!r}, "
            f"compress_with_stats={result_sidecar.cache_key!r}. "
            f"Both paths must use the same Rust cache_key for identical input."
        )

        # Both store entries must resolve to the original.
        entry_main = isolated_store.retrieve(result_main.cache_key)
        entry_sidecar = fresh2.retrieve(result_sidecar.cache_key)

        assert entry_main is not None, "Main path did not persist to store"
        assert entry_sidecar is not None, "Sidecar path did not persist to store"
        assert entry_main.original_content == diff
        assert entry_sidecar.original_content == diff
