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

import re

from furl_ctx.cache.compression_store import (
    get_compression_store,
    reset_compression_store,
)
from furl_ctx.transforms.log_compressor import (
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

    def test_traceback_terminator_does_not_sweep_following_log_lines(self):
        """fixed_in_cor25: the `ExceptionType: message` line ends the trace.

        Uppercase-starting log lines after a traceback (`INFO …`,
        `Build …`) used to be swept into the stack-trace selection until
        a lowercase/digit line or the 20-line cap.
        """
        lines = [
            "Traceback (most recent call last):",
            '  File "app.py", line 10, in <module>',
            "    main()",
            "ValueError: boom",
        ] + [f"INFO idle tick {i}" for i in range(60)]

        compressor = LogCompressor(config=LogCompressorConfig(enable_ccr=False))
        result = compressor.compress("\n".join(lines))

        assert "Traceback (most recent call last):" in result.compressed
        assert "ValueError: boom" in result.compressed
        # Tick 12 sits beyond the ±3 context window around the terminator;
        # the old state machine swept ticks 0-15 into the trace selection.
        assert "INFO idle tick 12" not in result.compressed


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
        """All-error input: kept ERROR lines are bounded and the omission
        arithmetic ties out.

        TEST-11: the old assert checked ``max_total_lines`` after setting
        ``max_errors=5`` — it never counted ERROR lines, so a selector that
        ignored ``max_errors`` entirely still passed. Pin the real contract:
        a small head/tail window of errors survives, everything else is
        disclosed in the omission summary, and kept + omitted == input.
        """
        n = 100
        lines = [f"ERROR: failure {i}" for i in range(n)]
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=50,
                max_errors=5,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        out_lines = result.compressed.split("\n")
        kept_errors = [ln for ln in out_lines if ln.startswith("ERROR: failure")]
        # Selection is real: a bounded window survives, not the whole log.
        assert 0 < len(kept_errors) < n / 2, (
            f"expected a bounded error window, kept {len(kept_errors)} of {n}"
        )

        # The omission summary discloses the drop and the arithmetic ties out.
        summaries = [ln for ln in out_lines if "lines omitted" in ln]
        assert summaries, f"expected an omission summary line, got: {out_lines[-3:]}"
        m = re.search(r"\[(\d+) lines omitted", summaries[-1])
        assert m is not None, f"unparseable summary: {summaries[-1]!r}"
        assert len(kept_errors) + int(m.group(1)) == n, (
            "kept + omitted must equal the input line count"
        )

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
        """Mixed line endings are handled and the ERROR line survives."""
        content = "INFO: line 1\r\nERROR: line 2\rINFO: line 3\n"

        compressor = LogCompressor()
        # Should not crash
        result = compressor.compress(content)
        assert result.compressed is not None
        # Content is the real contract for a log compressor: the ERROR line must
        # survive regardless of which line ending precedes it. `is not None`
        # alone would pass output that silently dropped it.
        assert "ERROR: line 2" in result.compressed

    def test_binary_like_content(self):
        """Content with binary-like patterns doesn't crash; ERROR survives."""
        content = "INFO: data\x00\x01\x02ERROR: test"

        compressor = LogCompressor()
        result = compressor.compress(content)
        assert result.compressed is not None
        # The ERROR line must survive even when embedded NUL/control bytes sit
        # next to it; pin the content, not just non-None.
        assert "ERROR: test" in result.compressed


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


class TestMinLinesBoundary:
    """At/below/above triple for min_lines_for_ccr (TEST-12).

    The gate is ``original_line_count < min_lines_for_ccr``
    (log_compressor.rs) → the boundary value itself gets compression
    attempted. Only the below side was pinned before.
    """

    def _compress(self, n_lines: int) -> "LogCompressionResult":
        compressor = LogCompressor(config=LogCompressorConfig(min_lines_for_ccr=5, enable_ccr=True))
        return compressor.compress("\n".join(f"INFO line {i}" for i in range(n_lines)))

    def test_below_floor_is_verbatim(self):
        result = self._compress(4)
        assert result.compression_ratio == 1.0
        assert result.cache_key is None

    def test_at_floor_attempts_compression(self):
        result = self._compress(5)
        assert result.compression_ratio < 1.0, (
            "exactly min_lines_for_ccr lines must be compressed (gate is `<`)"
        )

    def test_above_floor_attempts_compression(self):
        result = self._compress(6)
        assert result.compression_ratio < 1.0


class TestUniqueUnexpectedLogs:
    """Integration tests for unique/unexpected logs extraction (PR #97)."""

    def test_python_unique_logs_extraction(self):
        """Unique log lines are extracted and kept in the compressed output."""
        lines = [f"INFO: connection pool status active {i % 5}" for i in range(100)]
        lines[15] = "INFO: unique database checkpoint reached"
        lines[45] = "INFO: unexpected background task failed due to disk"
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=50,
                enable_ccr=False,
                max_unique_logs=5,
                unique_log_threshold=3,
            )
        )
        result = compressor.compress(content)

        assert "unique database checkpoint" in result.compressed
        assert "unexpected background task" in result.compressed

    def test_python_unique_logs_deduplicated_by_template(self):
        """Unique log lines are deduplicated by their template to save tokens."""
        lines = [f"INFO: heartbeat tick {i % 3}" for i in range(100)]
        lines[15] = "INFO: unique error signature x"
        lines[35] = "INFO: unique error signature x"  # duplicate template
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=50,
                enable_ccr=False,
                max_unique_logs=5,
                unique_log_threshold=3,
            )
        )
        result = compressor.compress(content)

        assert "unique error signature x" in result.compressed

    def test_python_unique_logs_config_bounds(self):
        """max_unique_logs strictly bounds the selection of unique log lines."""
        lines = [f"INFO: standard line {i % 5}" for i in range(100)]
        lines[15] = "INFO: first unique message"
        lines[25] = "INFO: second unique message"
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=50,
                enable_ccr=False,
                max_unique_logs=1,  # only allow 1 unique log
                unique_log_threshold=3,
            )
        )
        result = compressor.compress(content)

        assert "first unique message" in result.compressed
        assert "second unique message" not in result.compressed


class TestUniqueLogRegressionGuards:
    """Fix 1-3 regression guards for the unique-log feature."""

    def test_dropped_lines_are_ccr_recoverable_no_silent_loss(self):
        """Fix 1: with enable_ccr=True, dropped lines must stay CCR-recoverable.

        Unique-line preservation inflates the compressed body and can push
        the byte ratio ABOVE the 0.5 CCR threshold while lines are still
        dropped. Pre-fix that SUPPRESSED the recovery marker => silent loss.
        The marker must fire whenever any line is dropped, so the full
        original round-trips out of the store and nothing is silently lost.
        """
        reset_compression_store()
        try:
            # 40 identical heartbeats + 15 rare long INFO lines with DISTINCT
            # word stems. Distinct stems (not a varying digit) matter: they do
            # NOT collapse under template normalization, so they are kept as
            # unique lines and INFLATE the compressed body past ratio 0.5.
            # That is the exact bug region — pre-fix, ratio >= 0.5 suppressed
            # the recovery marker even though lines were still dropped.
            stems = [
                "quokka",
                "narwhal",
                "axolotl",
                "pangolin",
                "capybara",
                "meerkat",
                "ocelot",
                "tapir",
                "wombat",
                "lemur",
                "gecko",
                "ibis",
                "manta",
                "heron",
                "civet",
            ]
            lines = ["INFO: hb ping"] * 40
            for s in stems:
                lines.append(
                    f"INFO: subsystem {s} emitted an unexpected and fairly long diagnostic payload line"
                )
            content = "\n".join(lines)

            compressor = LogCompressor(
                config=LogCompressorConfig(enable_ccr=True, min_lines_for_ccr=50)
            )
            result = compressor.compress(content)

            # Body was inflated by kept unique lines, landing ABOVE the 0.5
            # CCR threshold, yet lines were still dropped — the bug region.
            assert result.unique_logs_kept > 0, "rare lines must be kept as unique"
            assert result.compression_ratio > 0.5, (
                "ratio must exceed the 0.5 CCR threshold so this exercises the "
                "region where pre-fix code suppressed the recovery marker"
            )
            assert result.compressed_line_count < result.original_line_count
            # Fix 1: despite ratio > 0.5, a recovery key is emitted because
            # lines were dropped, and it round-trips the FULL original — zero
            # silent loss. Pre-fix this returned None (silent loss).
            assert result.cache_key is not None, (
                "dropped lines require a recovery key even when ratio > 0.5"
            )
            entry = get_compression_store().retrieve(result.cache_key)
            assert entry is not None, "cache_key must resolve in the store"
            assert entry.original_content == content
            # Some rare lines are dropped from the visible output by the unique
            # cap (10 < 15) yet remain recoverable: nothing silently lost.
            dropped = [s for s in stems if s not in result.compressed]
            assert dropped, "unique cap (10 < 15) must drop some rare lines"
            for s in dropped:
                assert s in entry.original_content
        finally:
            reset_compression_store()

    def test_colon_less_warnings_stay_distinct(self):
        """Fix 2: distinct colon-less warnings differing only by variable
        DIGITS must NOT be collapsed by the shared dedupe normalizer.

        Digit-differing (not hex) inputs are the discriminating case: under
        whole-line normalization (the reverted bug) `\\d+` templates 5/6/7 to
        N and the three collapse to one, so only the first survives dedupe.
        The correct normalizer keeps colon-less lines verbatim, so all three
        survive.
        """
        lines = [f"INFO connection tick {i % 4}" for i in range(60)]
        lines[10] = "WARN queue depth exceeded 5 items"
        lines[20] = "WARN queue depth exceeded 6 items"
        lines[30] = "WARN queue depth exceeded 7 items"
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                enable_ccr=False,
                min_lines_for_ccr=50,
                max_warnings=5,
                dedupe_warnings=True,
            )
        )
        result = compressor.compress(content)

        # All three distinct warnings survive (not collapsed to one template).
        assert "exceeded 5 items" in result.compressed
        assert "exceeded 6 items" in result.compressed
        assert "exceeded 7 items" in result.compressed

    def test_python_unique_logs_kept_is_surfaced(self):
        """Fix 3: the Rust `unique_logs_kept` stat must reach the Python
        result (the shim built results only from the stats BTreeMap before)."""
        lines = [f"INFO: connection pool status active {i % 5}" for i in range(100)]
        lines[15] = "INFO: unique database checkpoint reached"
        lines[45] = "INFO: unexpected background task failed due to disk"
        content = "\n".join(lines)

        compressor = LogCompressor(
            config=LogCompressorConfig(
                min_lines_for_ccr=50,
                enable_ccr=False,
                max_unique_logs=5,
                unique_log_threshold=3,
            )
        )
        result = compressor.compress(content)

        assert result.unique_logs_kept == 2
