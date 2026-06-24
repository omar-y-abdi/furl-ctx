"""Comprehensive tests for search_compressor.py.

Tests cover:
1. grep/ripgrep output parsing
2. File grouping
3. Match selection and scoring
4. Edge cases
"""

from headroom.transforms.search_compressor import (
    FileMatches,
    SearchCompressionResult,
    SearchCompressor,
    SearchCompressorConfig,
    SearchMatch,
)


class TestFileGrouping:
    """Tests for grouping matches by file."""

    def test_file_matches_first_property(self):
        """FileMatches.first returns first match."""
        fm = FileMatches(
            file="test.py",
            matches=[
                SearchMatch(file="test.py", line_number=10, content="first"),
                SearchMatch(file="test.py", line_number=20, content="second"),
            ],
        )

        assert fm.first is not None
        assert fm.first.line_number == 10
        assert fm.first.content == "first"

    def test_file_matches_last_property(self):
        """FileMatches.last returns last match."""
        fm = FileMatches(
            file="test.py",
            matches=[
                SearchMatch(file="test.py", line_number=10, content="first"),
                SearchMatch(file="test.py", line_number=20, content="last"),
            ],
        )

        assert fm.last is not None
        assert fm.last.line_number == 20
        assert fm.last.content == "last"

    def test_file_matches_empty(self):
        """FileMatches with no matches handles first/last."""
        fm = FileMatches(file="test.py", matches=[])

        assert fm.first is None
        assert fm.last is None


class TestMatchSelection:
    """Tests for selecting which matches to keep."""

    def test_keeps_first_and_last_by_default(self):
        """First and last matches are kept by default."""
        content = "\n".join([f"src/file.py:{i}:line {i}" for i in range(1, 101)])

        compressor = SearchCompressor(
            config=SearchCompressorConfig(
                always_keep_first=True,
                always_keep_last=True,
                max_matches_per_file=5,
            )
        )
        result = compressor.compress(content)

        assert "src/file.py:1:line 1" in result.compressed
        assert "src/file.py:100:line 100" in result.compressed

    def test_respects_max_matches_per_file(self):
        """max_matches_per_file limits matches per file."""
        content = "\n".join([f"src/file.py:{i}:line {i}" for i in range(1, 51)])

        compressor = SearchCompressor(
            config=SearchCompressorConfig(
                max_matches_per_file=3,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # Should have at most 3 matches + summary
        file_lines = [
            line for line in result.compressed.split("\n") if line.startswith("src/file.py:")
        ]
        assert len(file_lines) <= 3

    def test_respects_max_total_matches(self):
        """max_total_matches limits total output."""
        # Create matches across many files
        lines = []
        for f in range(20):
            for i in range(10):
                lines.append(f"src/file{f}.py:{i}:line content")
        content = "\n".join(lines)

        compressor = SearchCompressor(
            config=SearchCompressorConfig(
                max_total_matches=15,
                max_files=20,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # Count actual match lines (not summaries)
        match_lines = [
            line for line in result.compressed.split("\n") if line and not line.startswith("[")
        ]
        assert len(match_lines) <= 15

    def test_respects_max_files(self):
        """max_files limits number of files in output."""
        # Create matches in many files
        lines = []
        for f in range(30):
            lines.append(f"src/file{f}.py:1:content")
        content = "\n".join(lines)

        compressor = SearchCompressor(
            config=SearchCompressorConfig(
                max_files=5,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # Count unique files in output
        output_files = set()
        for line in result.compressed.split("\n"):
            if ":" in line and not line.startswith("["):
                parts = line.split(":")
                if len(parts) >= 2:
                    output_files.add(parts[0])

        assert len(output_files) <= 5

    def test_high_scoring_files_selected_first(self):
        """Files with higher-scoring matches are selected first."""
        content = """normal/file.py:1:regular content
important/file.py:1:ERROR critical failure
another/file.py:1:some code here
"""
        compressor = SearchCompressor(
            config=SearchCompressorConfig(
                max_files=1,
                boost_errors=True,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # File with ERROR should be selected
        assert "important/file.py" in result.compressed

    def test_output_sorted_by_line_number(self):
        """Matches in output are sorted by line number within file."""
        content = """src/file.py:50:middle line
src/file.py:10:first line
src/file.py:90:last line
"""
        compressor = SearchCompressor()
        result = compressor.compress(content)

        lines = result.compressed.split("\n")
        line_numbers = []
        for line in lines:
            if line.startswith("src/file.py:"):
                parts = line.split(":")
                if len(parts) >= 2 and parts[1].isdigit():
                    line_numbers.append(int(parts[1]))

        assert line_numbers == sorted(line_numbers)


class TestCompressionBehavior:
    """Tests for overall compression behavior."""

    def test_small_results_unchanged(self):
        """Small results pass through unchanged."""
        content = "src/file.py:1:def foo():\nsrc/file.py:2:    pass"

        compressor = SearchCompressor()
        result = compressor.compress(content)

        assert result.compression_ratio == 1.0
        assert result.compressed == content

    def test_empty_input_handled(self):
        """Empty input is handled gracefully."""
        compressor = SearchCompressor()
        result = compressor.compress("")

        assert result.compressed == ""
        assert result.original_match_count == 0
        assert result.compression_ratio == 1.0

    def test_compression_adds_summary(self):
        """Compression adds summary for omitted matches."""
        content = "\n".join([f"src/file.py:{i}:line {i}" for i in range(1, 51)])

        compressor = SearchCompressor(
            config=SearchCompressorConfig(
                max_matches_per_file=3,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # Should have summary about omitted matches
        assert "[... and" in result.compressed
        assert "more matches" in result.compressed

    def test_compression_ratio_calculated(self):
        """Compression ratio is calculated correctly."""
        content = "\n".join([f"src/file.py:{i}:line {i}" for i in range(1, 101)])

        compressor = SearchCompressor(
            config=SearchCompressorConfig(
                max_matches_per_file=5,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # Ratio should be less than 1.0 for compression
        assert result.compression_ratio < 1.0


class TestSearchCompressionResult:
    """Tests for SearchCompressionResult dataclass."""

    def test_tokens_saved_estimate(self):
        """Token savings estimation works correctly."""
        original = "a" * 400  # ~100 tokens
        compressed = "b" * 40  # ~10 tokens

        result = SearchCompressionResult(
            compressed=compressed,
            original=original,
            original_match_count=100,
            compressed_match_count=10,
            files_affected=5,
            compression_ratio=0.1,
        )

        # (400 - 40) / 4 = 90 tokens saved
        assert result.tokens_saved_estimate == 90

    def test_matches_omitted_property(self):
        """matches_omitted property calculates correctly."""
        result = SearchCompressionResult(
            compressed="test",
            original="original",
            original_match_count=100,
            compressed_match_count=15,
            files_affected=10,
            compression_ratio=0.15,
        )

        assert result.matches_omitted == 85

    def test_default_summaries_empty(self):
        """Default summaries is empty dict."""
        result = SearchCompressionResult(
            compressed="test",
            original="original",
            original_match_count=1,
            compressed_match_count=1,
            files_affected=1,
            compression_ratio=1.0,
        )

        assert result.summaries == {}


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_single_match_passthrough(self):
        """Single match passes through unchanged."""
        content = "src/file.py:10:single match"

        compressor = SearchCompressor()
        result = compressor.compress(content)

        assert result.compressed == content
        assert result.original_match_count == 1
        assert result.compressed_match_count == 1

    def test_unicode_content(self):
        """Unicode characters in content are handled."""
        content = """src/main.py:10:msg = "こんにちは"
src/main.py:20:emoji = "🎉"
src/main.py:30:umlaut = "über"
"""
        compressor = SearchCompressor()
        result = compressor.compress(content)

        assert "こんにちは" in result.compressed
        assert "🎉" in result.compressed
        assert "über" in result.compressed

    def test_very_long_lines(self):
        """Very long content lines are handled."""
        long_content = "x" * 10000
        content = f"src/file.py:1:{long_content}"

        compressor = SearchCompressor()
        result = compressor.compress(content)

        assert len(result.compressed) > 0
        assert long_content in result.compressed

    def test_many_files_few_matches(self):
        """Many files with one match each are handled."""
        lines = [f"src/file{i}.py:1:single match" for i in range(100)]
        content = "\n".join(lines)

        compressor = SearchCompressor(
            config=SearchCompressorConfig(
                max_files=10,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        assert result.files_affected == 100
        # Output should be limited to max_files
        output_files = set()
        for line in result.compressed.split("\n"):
            if ":" in line and not line.startswith("["):
                parts = line.split(":")
                if len(parts) >= 2:
                    output_files.add(parts[0])
        assert len(output_files) <= 10

    def test_special_characters_in_path(self):
        """Special characters in file paths are handled."""
        content = """src/my-file.py:10:content
src/my_file.py:20:content
src/my.file.py:30:content
src/file (1).py:40:content
"""
        compressor = SearchCompressor()
        result = compressor.compress(content)

        assert "my-file.py" in result.compressed
        assert "my_file.py" in result.compressed

    def test_line_number_zero(self):
        """Line number 0 is handled (edge case)."""
        content = "src/file.py:0:line at position 0"

        compressor = SearchCompressor()
        result = compressor.compress(content)

        assert ":0:" in result.compressed

class TestContextIntegration:
    """Tests for context-aware compression."""

    def test_context_influences_selection(self):
        """Context string influences which matches are selected."""
        lines = []
        for i in range(50):
            lines.append(f"src/utils.py:{i}:def helper_{i}():")

        # Add some specific matches
        lines.append("src/auth.py:100:def authenticate_user():")
        lines.append("src/auth.py:200:def validate_token():")

        content = "\n".join(lines)

        compressor = SearchCompressor(
            config=SearchCompressorConfig(
                max_total_matches=5,
                context_keywords=["auth", "token", "validate"],
                enable_ccr=False,
            )
        )
        result = compressor.compress(content, context="find authentication code")

        # Auth-related matches should be included
        assert "authenticate" in result.compressed or "token" in result.compressed


class TestOutputFormatting:
    """Tests for output format and structure."""

    def test_output_maintains_grep_format(self):
        """Output maintains file:line:content format."""
        content = """src/file.py:10:def foo():
src/file.py:20:def bar():
"""
        compressor = SearchCompressor()
        result = compressor.compress(content)

        for line in result.compressed.split("\n"):
            if line and not line.startswith("["):
                assert line.count(":") >= 2
                parts = line.split(":", 2)
                assert parts[1].isdigit()

    def test_summaries_track_omitted_per_file(self):
        """Summaries dict tracks omissions per file."""
        content = "\n".join([f"src/file.py:{i}:line {i}" for i in range(1, 51)])

        compressor = SearchCompressor(
            config=SearchCompressorConfig(
                max_matches_per_file=3,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        assert "src/file.py" in result.summaries
        assert "more matches" in result.summaries["src/file.py"]

    def test_files_sorted_in_output(self):
        """Files are sorted alphabetically in output."""
        content = """z_file.py:1:content
a_file.py:1:content
m_file.py:1:content
"""
        compressor = SearchCompressor()
        result = compressor.compress(content)

        lines = [
            line for line in result.compressed.split("\n") if line and not line.startswith("[")
        ]
        files = [line.split(":")[0] for line in lines]

        assert files == sorted(files)


class TestSearchMatchDataclass:
    """Tests for SearchMatch dataclass."""

    def test_default_score_zero(self):
        """Default score is 0.0."""
        match = SearchMatch(file="test.py", line_number=1, content="test")
        assert match.score == 0.0

    def test_match_attributes(self):
        """Match attributes are set correctly."""
        match = SearchMatch(
            file="src/main.py",
            line_number=42,
            content="def process():",
            score=0.8,
        )

        assert match.file == "src/main.py"
        assert match.line_number == 42
        assert match.content == "def process():"
        assert match.score == 0.8


class TestConfigOptions:
    """Tests for configuration options."""

    def test_disable_keep_first(self):
        """always_keep_first=False doesn't force first match."""
        content = "\n".join([f"src/file.py:{i}:line {i}" for i in range(1, 51)])

        compressor = SearchCompressor(
            config=SearchCompressorConfig(
                always_keep_first=False,
                always_keep_last=True,
                max_matches_per_file=2,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # First line not guaranteed to be present
        # But last should be
        assert "src/file.py:50:line 50" in result.compressed

    def test_disable_keep_last(self):
        """always_keep_last=False doesn't force last match."""
        content = "\n".join([f"src/file.py:{i}:line {i}" for i in range(1, 51)])

        compressor = SearchCompressor(
            config=SearchCompressorConfig(
                always_keep_first=True,
                always_keep_last=False,
                max_matches_per_file=2,
                enable_ccr=False,
            )
        )
        result = compressor.compress(content)

        # First line should be present
        assert "src/file.py:1:line 1" in result.compressed

    def test_min_matches_for_ccr(self):
        """min_matches_for_ccr threshold is respected."""
        content = "\n".join([f"src/file.py:{i}:line {i}" for i in range(1, 6)])

        # With threshold of 10, CCR should not activate for 5 matches
        compressor = SearchCompressor(
            config=SearchCompressorConfig(
                min_matches_for_ccr=10,
                enable_ccr=True,
            )
        )
        result = compressor.compress(content)

        assert result.cache_key is None
