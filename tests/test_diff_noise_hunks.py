"""Tests for DiffCompressor's `drop_noise_hunks` mode (DiffNoise, P2-14).

Elides noise hunks before compression — lockfile churn
(package-lock.json / Cargo.lock / uv.lock / yarn.lock) and hunks whose
changed lines are whitespace-only — each file summarized with one
``[noise hunk elided: <path> (+A/-B)]`` line. Contracts pinned here:

1. Default OFF — noise hunks survive verbatim, no elision marker,
   output byte-identical to the previous behavior.
2. Lockfile hunks elided when enabled (nested paths included).
3. Whitespace-only hunks elided when enabled.
4. Mixed diffs keep the real hunks.
5. Recovery stays byte-exact: the CCR store holds the FULL original
   diff including every elided hunk, and the store-write veto still
   protects against dangling markers.
"""

from __future__ import annotations

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


@pytest.fixture(autouse=True)
def isolated_store():
    """Fresh request-scoped CompressionStore per test. Mirrors
    test_diff_compressor_sidecar_persist.py."""
    fresh = CompressionStore(max_entries=500, enable_feedback=False)
    set_request_compression_store(fresh)
    yield fresh
    clear_request_compression_store()


def _mixed_noise_diff() -> str:
    """One real code change + one whitespace-only hunk in src/app.py,
    plus a Cargo.lock section with two churn hunks (+3/-2 total)."""
    return (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,4 +1,4 @@\n"
        " ctx_head\n"
        "-real_old_logic\n"
        "+real_new_logic\n"
        " ctx_tail\n"
        "@@ -20,4 +20,4 @@\n"
        " ctx_ws_a\n"
        "-\t\n"
        "+\n"
        " ctx_ws_b\n"
        "diff --git a/Cargo.lock b/Cargo.lock\n"
        "--- a/Cargo.lock\n"
        "+++ b/Cargo.lock\n"
        "@@ -10,4 +10,5 @@\n"
        " [[package]]\n"
        '-version = "1.0.0"\n'
        '+version = "1.0.1"\n'
        '+checksum = "abc123"\n'
        ' name = "serde"\n'
        "@@ -100,3 +101,3 @@\n"
        " [[package]]\n"
        '-version = "2.4.0"\n'
        '+version = "2.5.0"\n'
    )


def _cfg(**overrides) -> DiffCompressorConfig:
    defaults = {"min_lines_for_ccr": 5, "enable_ccr": False}
    defaults.update(overrides)
    return DiffCompressorConfig(**defaults)


class TestDefaultOffByteIdentity:
    """drop_noise_hunks defaults False; noise survives verbatim."""

    def test_config_defaults_off(self):
        assert DiffCompressorConfig().drop_noise_hunks is False

    def test_noise_hunks_survive_by_default(self):
        result = DiffCompressor(_cfg()).compress(_mixed_noise_diff())
        assert '+version = "1.0.1"' in result.compressed
        assert "@@ -20,4 +20,4 @@" in result.compressed
        assert "[noise hunk elided:" not in result.compressed

    def test_default_and_explicit_false_are_byte_identical(self):
        default = DiffCompressor(_cfg()).compress(_mixed_noise_diff())
        explicit = DiffCompressor(_cfg(drop_noise_hunks=False)).compress(_mixed_noise_diff())
        assert default.compressed == explicit.compressed


class TestNoiseElision:
    """drop_noise_hunks=True elides lockfile + whitespace-only hunks."""

    def test_lockfile_hunks_elided_with_per_file_summary(self):
        result = DiffCompressor(_cfg(drop_noise_hunks=True)).compress(_mixed_noise_diff())
        assert "[noise hunk elided: Cargo.lock (+3/-2)]" in result.compressed
        assert '+version = "1.0.1"' not in result.compressed
        assert '+checksum = "abc123"' not in result.compressed
        # The section header survives so the reader sees WHICH file changed.
        assert "diff --git a/Cargo.lock b/Cargo.lock" in result.compressed

    def test_whitespace_only_hunk_elided(self):
        result = DiffCompressor(_cfg(drop_noise_hunks=True)).compress(_mixed_noise_diff())
        assert "@@ -20,4 +20,4 @@" not in result.compressed
        assert "[noise hunk elided: src/app.py (+1/-1)]" in result.compressed

    def test_mixed_diff_keeps_real_hunks(self):
        result = DiffCompressor(_cfg(drop_noise_hunks=True)).compress(_mixed_noise_diff())
        assert "-real_old_logic" in result.compressed
        assert "+real_new_logic" in result.compressed
        # 4 hunks total: 1 kept, 3 elided (elided count as removed).
        assert result.hunks_kept == 1
        assert result.hunks_removed == 3

    def test_nested_lockfile_path_matches_basename(self):
        diff = (
            "diff --git a/frontend/package-lock.json b/frontend/package-lock.json\n"
            "--- a/frontend/package-lock.json\n"
            "+++ b/frontend/package-lock.json\n"
            "@@ -1,3 +1,3 @@\n"
            " ctx\n"
            '-  "version": "1.0.0",\n'
            '+  "version": "1.0.1",\n'
            " ctx2\n"
        )
        result = DiffCompressor(_cfg(min_lines_for_ccr=1, drop_noise_hunks=True)).compress(diff)
        assert "[noise hunk elided: frontend/package-lock.json (+1/-1)]" in result.compressed
        assert '"1.0.1"' not in result.compressed

    def test_sidecar_stats_expose_noise_counter(self):
        result, stats = DiffCompressor(_cfg(drop_noise_hunks=True)).compress_with_stats(
            _mixed_noise_diff()
        )
        assert stats.noise_hunks_elided == 3
        assert stats.hunks_total == stats.hunks_kept + stats.hunks_dropped
        assert result.hunks_removed == 3


class TestRecoveryDiscipline:
    """Elision never reduces recoverability: the CCR entry is the FULL
    original diff, elided hunks included."""

    def test_store_roundtrip_is_byte_exact_including_elided_hunks(self, isolated_store):
        # Pad the lockfile section so elision produces enough savings for
        # the CCR ratio gate — the elided churn IS the savings.
        diff = _mixed_noise_diff()
        for i in range(40):
            start = 200 + i * 10
            diff += f"@@ -{start},3 +{start},4 @@\n dep{i}\n-old{i}\n+new{i}\n+extra{i}\n"

        compressor = DiffCompressor(
            DiffCompressorConfig(min_lines_for_ccr=5, enable_ccr=True, drop_noise_hunks=True)
        )
        result = compressor.compress(diff)
        assert result.cache_key is not None, (
            "CCR did not fire — the padded lockfile churn must clear the "
            "0.8 savings ratio once elided; grow the fixture if this fails."
        )
        entry = isolated_store.retrieve(result.cache_key)
        assert entry is not None, "marker must never dangle"
        assert entry.original_content == diff
        # Every elided noise hunk is inside the recoverable original.
        assert '+checksum = "abc123"' in entry.original_content
