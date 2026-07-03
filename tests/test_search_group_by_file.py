"""Tests for SearchCompressor's `group_by_file` rendering mode (P2-15).

Restores upstream's grouped search rendering: one ``file:`` header per
file with matches nested under it, instead of the flat
``file:line:content`` list. Contracts pinned here:

1. Default OFF — flat output is byte-identical to the pre-existing
   behavior (when nothing is dropped, output == input).
2. Grouped mode renders one header per file, matches indented under it.
3. Per-file / global caps and omission summaries still apply.
4. CCR round-trip: the store holds the byte-exact ORIGINAL (flat grep
   output), never the grouped rendering, and the marker resolves.
"""

from __future__ import annotations

import pytest

from furl_ctx.cache.compression_store import (
    CompressionStore,
    clear_request_compression_store,
    set_request_compression_store,
)
from furl_ctx.transforms.search_compressor import (
    SearchCompressor,
    SearchCompressorConfig,
)


@pytest.fixture(autouse=True)
def isolated_store():
    """Fresh request-scoped CompressionStore per test (never pollutes the
    global singleton). Mirrors test_diff_compressor_sidecar_persist.py."""
    fresh = CompressionStore(max_entries=500, enable_feedback=False)
    set_request_compression_store(fresh)
    yield fresh
    clear_request_compression_store()


def _flat_content(files: int = 1, matches: int = 3) -> str:
    lines = []
    for f in range(files):
        for i in range(1, matches + 1):
            lines.append(f"src/file{f}.py:{i}:line {i}")
    return "\n".join(lines)


class TestDefaultOffByteIdentity:
    """group_by_file defaults False and the flat rendering is unchanged."""

    def test_config_defaults_off(self):
        assert SearchCompressorConfig().group_by_file is False

    def test_flat_output_reproduces_input_when_nothing_dropped(self):
        """The flat formatter re-emits `file:line:content` byte-for-byte;
        with no drops the compressed output equals the input exactly."""
        content = _flat_content(files=2, matches=3)
        compressor = SearchCompressor(SearchCompressorConfig(enable_ccr=False))
        result = compressor.compress(content)
        assert result.compressed == content

    def test_flat_and_default_configs_are_byte_identical(self):
        """An explicit group_by_file=False config renders the same bytes
        as the default config on a lossy input (drops + summaries)."""
        content = _flat_content(files=3, matches=20)
        explicit = SearchCompressor(
            SearchCompressorConfig(enable_ccr=False, group_by_file=False)
        ).compress(content)
        default = SearchCompressor(SearchCompressorConfig(enable_ccr=False)).compress(content)
        assert explicit.compressed == default.compressed


class TestGroupedRendering:
    """group_by_file=True nests matches under one header per file."""

    def test_one_header_per_file_with_nested_matches(self):
        content = "src/a.py:1:alpha\nsrc/a.py:2:beta\nsrc/b.py:7:gamma"
        compressor = SearchCompressor(SearchCompressorConfig(enable_ccr=False, group_by_file=True))
        result = compressor.compress(content)
        assert result.compressed == "src/a.py:\n  1:alpha\n  2:beta\nsrc/b.py:\n  7:gamma"

    def test_file_path_not_repeated_per_match(self):
        content = "\n".join(f"src/hot.py:{i}:needle {i}" for i in range(1, 6))
        compressor = SearchCompressor(SearchCompressorConfig(enable_ccr=False, group_by_file=True))
        result = compressor.compress(content)
        # Path appears exactly once (the header), not on every line.
        assert result.compressed.count("src/hot.py") == 1
        assert result.compressed.startswith("src/hot.py:\n")

    def test_caps_honored_and_summary_nested(self):
        content = "\n".join(f"src/file.py:{i}:line {i}" for i in range(1, 51))
        compressor = SearchCompressor(
            SearchCompressorConfig(
                enable_ccr=False,
                group_by_file=True,
                max_matches_per_file=3,
            )
        )
        result = compressor.compress(content)
        match_lines = [
            line
            for line in result.compressed.split("\n")
            if line.startswith("  ") and not line.lstrip().startswith("[")
        ]
        assert len(match_lines) <= 3
        # Omission summary present, nested, and recorded in the map.
        assert "  [... and 47 more matches in src/file.py]" in result.compressed
        assert result.summaries["src/file.py"] == "  [... and 47 more matches in src/file.py]"


class TestGroupedCcrRoundTrip:
    """CCR persistence is rendering-independent: the ORIGINAL flat bytes
    are what the store returns."""

    def test_store_roundtrip_is_byte_exact_original(self, isolated_store):
        content = "\n".join(f"src/main.py:{i}:auth event {i}" for i in range(1, 51))
        compressor = SearchCompressor(
            SearchCompressorConfig(
                group_by_file=True,
                enable_ccr=True,
                min_matches_for_ccr=2,
            )
        )
        result = compressor.compress(content, context="auth", bias=0.5)
        assert result.cache_key is not None, (
            "CCR did not fire — 50 matches with min_matches_for_ccr=2 must "
            "clear the thresholds; adjust the fixture if defaults changed."
        )
        # Marker grammar unchanged in grouped mode.
        assert result.compressed.endswith(f". Retrieve more: hash={result.cache_key}]")
        entry = isolated_store.retrieve(result.cache_key)
        assert entry is not None, "marker must never dangle"
        assert entry.original_content == content
