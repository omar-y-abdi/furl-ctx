"""Comprehensive tests for log_compressor.py.

Tests cover the live `compress()` surface end-to-end:
1. Compression behavior (error/summary preservation, context lines, limits)
2. Compression ratios and result properties
3. Config options
4. Edge cases and the LogLine dataclass

Format detection, line classification/scoring, and dedupe are owned by the
Rust crate (`log_compressor.rs` unit tests); the Python shim no longer carries
those helpers, so this suite exercises them only through `compress()`.
"""

from headroom.transforms.log_compressor import (
    LogCompressionResult,
    LogCompressor,
    LogCompressorConfig,
    LogFormat,
    LogLevel,
    LogLine,
)


class TestCompressionBehavior:
    """Tests for overall compression behavior."""

    def test_small_log_passthrough(self):
        """Logs smaller than threshold pass through unchanged."""
        content = "INFO: Starting\nINFO: Done"

        compressor = LogCompressor(config=LogCompressorConfig(min_lines_for_ccr=100))
        result = compressor.compress(content)

        assert result.compression_ratio == 1.0
        assert result.compressed == content
        assert result.original_line_count == 2

    def test_large_log_compressed(self):
        """Large logs are compressed."""
        lines = [f"INFO: Processing item {i}" for i in range(200)]
        lines.append("ERROR: Failed at item 100")
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=50,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        assert result.compression_ratio < 1.0
        assert result.compressed_line_count < result.original_line_count
        # Error is preserved
        assert "ERROR: Failed" in result.compressed

    def test_keeps_first_and_last_errors(self):
        """First and last errors are preserved."""
        lines = [f"INFO: item {i}" for i in range(100)]
        lines[10] = "ERROR: first error"
        lines[50] = "ERROR: middle error"
        lines[90] = "ERROR: last error"
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=50,
                keep_first_error=True,
                keep_last_error=True,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        assert "first error" in result.compressed
        assert "last error" in result.compressed

    def test_summary_lines_preserved(self):
        """Summary lines are always preserved."""
        content = """INFO: test 1
INFO: test 2
========================================
TOTAL: 10 tests passed
Build succeeded in 5.2s
"""
        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=2,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        assert "========" in result.compressed
        assert "TOTAL:" in result.compressed or "Build succeeded" in result.compressed

    def test_context_lines_added(self):
        """Context lines around errors are included."""
        lines = [f"INFO: item {i}" for i in range(100)]
        lines[50] = "ERROR: critical failure"
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=50,
                error_context_lines=2,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # Should have context around the error
        assert "item 48" in result.compressed or "item 49" in result.compressed
        assert "item 51" in result.compressed or "item 52" in result.compressed


class TestCompressionRatios:
    """Tests for compression ratio calculations."""

    def test_compression_ratio_calculation(self):
        """Compression ratio is calculated correctly."""
        content = "a" * 1000  # 1000 chars
        compressed = "b" * 100  # 100 chars

        # Direct calculation: len(compressed) / len(content)
        expected_ratio = 100 / 1000  # 0.1

        # Result ratio is based on character counts
        result = LogCompressionResult(
            compressed=compressed,
            original=content,
            original_line_count=100,
            compressed_line_count=10,
            format_detected=LogFormat.GENERIC,
            compression_ratio=len(compressed) / len(content),
        )

        assert result.compression_ratio == expected_ratio

    def test_tokens_saved_estimate(self):
        """Token savings estimation works correctly."""
        content = "a" * 400  # ~100 tokens
        compressed = "b" * 40  # ~10 tokens

        result = LogCompressionResult(
            compressed=compressed,
            original=content,
            original_line_count=10,
            compressed_line_count=1,
            format_detected=LogFormat.GENERIC,
            compression_ratio=0.1,
        )

        # (400 - 40) / 4 = 90 tokens saved
        assert result.tokens_saved_estimate == 90

    def test_lines_omitted_property(self):
        """Lines omitted property works correctly."""
        result = LogCompressionResult(
            compressed="test",
            original="test\noriginal",
            original_line_count=100,
            compressed_line_count=10,
            format_detected=LogFormat.GENERIC,
            compression_ratio=0.1,
        )

        assert result.lines_omitted == 90


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_input(self):
        """Empty input is handled gracefully."""
        compressor = LogCompressor()
        result = compressor.compress("")

        assert result.compressed == ""
        assert result.original_line_count == 1  # Empty string splits to one empty line
        assert result.compression_ratio == 1.0

    def test_single_line_input(self):
        """Single line input passes through."""
        compressor = LogCompressor()
        result = compressor.compress("Single line of text")

        assert result.compressed == "Single line of text"
        assert result.compression_ratio == 1.0

    def test_all_errors_no_info(self):
        """Log with only errors is handled."""
        lines = [f"ERROR: failure {i}" for i in range(100)]
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=50,
                max_errors=5,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # Should limit to max_errors
        assert result.compressed_line_count <= compressor.config.max_total_lines

    def test_unicode_content(self):
        """Unicode characters are handled correctly."""
        content = """INFO: Processing 日本語
ERROR: Failed with émoji 🚀
WARN: Über important
"""
        compressor = LogCompressor()
        result = compressor.compress(content)

        # Should not crash and preserve unicode
        assert (
            "日本語" in result.compressed
            or "émoji" in result.compressed
            or "Über" in result.compressed
        )

    def test_very_long_lines(self):
        """Very long lines don't cause issues."""
        long_line = "ERROR: " + "x" * 10000
        lines = [f"INFO: line {i}" for i in range(100)]
        lines[50] = long_line
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=50,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # Should complete without error
        assert len(result.compressed) > 0

    def test_mixed_line_endings(self):
        """Mixed line endings are handled."""
        content = "INFO: line 1\r\nERROR: line 2\rINFO: line 3\n"

        compressor = LogCompressor()
        # Should not crash
        result = compressor.compress(content)
        assert result.compressed is not None

    def test_binary_like_content(self):
        """Content with binary-like patterns doesn't crash."""
        content = "INFO: data\x00\x01\x02ERROR: test"

        compressor = LogCompressor()
        result = compressor.compress(content)
        assert result.compressed is not None


class TestConfigOptions:
    """Tests for configuration options."""

    def test_max_errors_config(self):
        """max_errors configuration limits error selection."""
        lines = [f"ERROR: error {i}" for i in range(50)]
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=10,
                max_errors=3,
                max_total_lines=50,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # Count error lines in output (excluding summary line)
        error_count = sum(1 for line in result.compressed.split("\n") if "ERROR:" in line)
        assert error_count <= 3 + compressor.config.error_context_lines * 2

    def test_max_warnings_config(self):
        """max_warnings configuration limits warning selection."""
        lines = [f"WARN: warning {i}" for i in range(50)]
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=10,
                max_warnings=2,
                dedupe_warnings=False,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # Warnings should be limited
        warn_count = sum(1 for line in result.compressed.split("\n") if "WARN:" in line)
        assert warn_count <= 2 + compressor.config.error_context_lines * 2

    def test_max_total_lines_config(self):
        """max_total_lines configuration limits output."""
        lines = [f"ERROR: error {i}" for i in range(200)]
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=50,
                max_total_lines=20,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # Output lines should be limited (plus summary line)
        output_lines = [line for line in result.compressed.split("\n") if line.strip()]
        assert len(output_lines) <= 21  # max_total_lines + 1 summary

    def test_dedupe_warnings_disabled(self):
        """dedupe_warnings=False preserves duplicate warnings."""
        lines = [
            "WARN: same warning",
            "WARN: same warning",
            "WARN: same warning",
        ]
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=1,
                dedupe_warnings=False,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # All warnings preserved when dedupe disabled
        warn_count = sum(1 for line in result.compressed.split("\n") if "WARN:" in line)
        assert warn_count == 3


class TestLogLineDataclass:
    """Tests for LogLine dataclass behavior."""

    def test_equality_by_line_number(self):
        """LogLine equality is based on line_number."""
        line1 = LogLine(line_number=10, content="foo")
        line2 = LogLine(line_number=10, content="bar")
        line3 = LogLine(line_number=20, content="foo")

        assert line1 == line2
        assert line1 != line3

    def test_hash_by_line_number(self):
        """LogLine hash is based on line_number."""
        line1 = LogLine(line_number=10, content="foo")
        line2 = LogLine(line_number=10, content="bar")

        assert hash(line1) == hash(line2)

        # Can be used in sets
        line_set = {line1, line2}
        assert len(line_set) == 1

    def test_default_values(self):
        """LogLine default values are correct."""
        line = LogLine(line_number=1, content="test")

        assert line.level == LogLevel.UNKNOWN
        assert line.is_stack_trace is False
        assert line.is_summary is False
        assert line.score == 0.0


class TestOutputFormatting:
    """Tests for output formatting and stats."""

    def test_format_output_includes_stats(self):
        """Format output includes category stats."""
        lines = [
            "ERROR: error 1",
            "ERROR: error 2",
            "WARN: warning 1",
            "INFO: info 1",
            "INFO: info 2",
            "INFO: info 3",
        ] * 20  # Make it large enough to trigger compression
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=50,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # Stats should be populated
        assert "errors" in result.stats
        assert "warnings" in result.stats
        assert "info" in result.stats
        assert result.stats["errors"] > 0
        assert result.stats["warnings"] > 0

    def test_format_output_summary_line(self):
        """Formatted output includes summary of omitted lines."""
        lines = [f"INFO: message {i}" for i in range(200)]
        lines.append("ERROR: critical")
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=50,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # Should have omission summary
        assert "lines omitted" in result.compressed
