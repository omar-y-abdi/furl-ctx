"""Unit tests for the pure filter/mode modules (NR2-2 features b, c).

These exercise the domain logic directly (no MCP server, no store), matching
the repo's "domain logic testable without mocks" rule:

* ``retrieve_filters`` — smart-constructor validation and the total apply step;
* ``compress_modes`` — mode parsing, the regex-first/glob-fallback matcher, and
  the byte-exact partition/reassembly invariant.
"""

from __future__ import annotations

import json

from furl_ctx.ccr.compress_modes import (
    CompressionMode,
    SectionPatterns,
    _pattern_matches,
    partition_content,
)
from furl_ctx.ccr.retrieve_filters import (
    FilteredContent,
    FilterError,
    RetrieveFilters,
    apply_filters,
)

# ─── RetrieveFilters.parse ──────────────────────────────────────────────────


def test_empty_arguments_parse_to_empty_spec() -> None:
    spec = RetrieveFilters.parse({})
    assert isinstance(spec, RetrieveFilters)
    assert spec.is_empty


def test_parse_rejects_bad_regex() -> None:
    err = RetrieveFilters.parse({"pattern": "([unclosed"})
    assert isinstance(err, FilterError)
    assert "invalid regex" in err.reason


def test_parse_rejects_non_string_pattern() -> None:
    err = RetrieveFilters.parse({"pattern": 7})
    assert isinstance(err, FilterError)


def test_parse_rejects_bool_context_lines() -> None:
    # bool is an int subclass — must be rejected explicitly, not treated as 0/1.
    err = RetrieveFilters.parse({"pattern": "x", "context_lines": True})
    assert isinstance(err, FilterError)
    assert "context_lines" in err.reason


def test_parse_rejects_inverted_range() -> None:
    err = RetrieveFilters.parse({"line_range": [9, 2]})
    assert isinstance(err, FilterError)
    assert "must be >= start" in err.reason


def test_parse_rejects_fields_plus_line_filter() -> None:
    err = RetrieveFilters.parse({"fields": ["a"], "line_range": [1, 2]})
    assert isinstance(err, FilterError)
    assert "cannot be combined" in err.reason


def test_parse_accepts_open_ended_range() -> None:
    spec = RetrieveFilters.parse({"line_range": [3, None]})
    assert isinstance(spec, RetrieveFilters)
    assert spec.line_start == 3
    assert spec.line_end is None


# ─── apply_filters totality ─────────────────────────────────────────────────


def test_apply_fields_on_non_array_is_error_not_crash() -> None:
    out = apply_filters("plain text", RetrieveFilters.parse({"fields": ["id"]}))  # type: ignore[arg-type]
    assert isinstance(out, FilterError)


def test_apply_fields_projection() -> None:
    content = json.dumps([{"a": 1, "b": 2}, {"a": 3, "b": 4}])
    spec = RetrieveFilters.parse({"fields": ["a"]})
    assert isinstance(spec, RetrieveFilters)
    out = apply_filters(content, spec)
    assert isinstance(out, FilteredContent)
    assert json.loads(out.content) == [{"a": 1}, {"a": 3}]


def test_apply_fields_skips_non_object_elements() -> None:
    content = json.dumps([{"a": 1}, "scalar", 42, {"a": 2}])
    spec = RetrieveFilters.parse({"fields": ["a"]})
    assert isinstance(spec, RetrieveFilters)
    out = apply_filters(content, spec)
    assert isinstance(out, FilteredContent)
    assert json.loads(out.content) == [{"a": 1}, {"a": 2}]
    assert out.matched_count == 2  # projectable objects
    assert out.total_count == 4  # all array elements


# ─── CompressionMode.parse ──────────────────────────────────────────────────


def test_mode_parse_defaults_to_normal_on_none() -> None:
    assert CompressionMode.parse(None) is CompressionMode.NORMAL


def test_mode_parse_case_insensitive() -> None:
    assert CompressionMode.parse("AGGRESSIVE") is CompressionMode.AGGRESSIVE


def test_mode_parse_unknown_is_error_string() -> None:
    out = CompressionMode.parse("turbo")
    assert isinstance(out, str)
    assert "unknown mode" in out


# ─── _pattern_matches: regex-first, glob-fallback ───────────────────────────


def test_pattern_regex_partial_match() -> None:
    # A valid regex uses search (partial): "ERROR" matches a line containing it.
    assert _pattern_matches("ERROR", "line with ERROR inside") is True
    assert _pattern_matches("ERROR", "clean line") is False


def test_pattern_glob_fallback() -> None:
    # "*.py" is invalid as a regex (leading *), so it falls back to fnmatch,
    # which anchors the whole string.
    assert _pattern_matches("*.py", "module.py") is True
    assert _pattern_matches("*.py", "module.txt") is False


def test_pattern_regex_anchors_and_classes() -> None:
    assert _pattern_matches(r"^\d+$", "12345") is True
    assert _pattern_matches(r"^\d+$", "12a45") is False


# ─── SectionPatterns eligibility ────────────────────────────────────────────


def test_exclude_protects_line() -> None:
    patterns = SectionPatterns(include=(), exclude=("SECRET",))
    assert patterns.line_is_eligible("has SECRET here") is False
    assert patterns.line_is_eligible("ordinary line") is True


def test_include_restricts_eligibility() -> None:
    patterns = SectionPatterns(include=("DATA",), exclude=())
    assert patterns.line_is_eligible("DATA row") is True
    assert patterns.line_is_eligible("header row") is False


def test_exclude_overrides_include() -> None:
    patterns = SectionPatterns(include=("DATA",), exclude=("SKIP",))
    # Matches include but ALSO matches exclude → protected.
    assert patterns.line_is_eligible("DATA but SKIP this") is False


def test_tool_name_exclusion() -> None:
    patterns = SectionPatterns(include=(), exclude=("Read",))
    assert patterns.tool_name_is_excluded("Read") is True
    assert patterns.tool_name_is_excluded("Bash") is False
    assert patterns.tool_name_is_excluded(None) is False


# ─── partition_content byte-exact reassembly ────────────────────────────────


def test_partition_reassembly_is_byte_exact() -> None:
    # The core safety invariant: concatenating the runs with newlines
    # reproduces the original string exactly, whatever the pattern split.
    content = "a\nSKIP b\nc\nd\nSKIP e\nf"
    patterns = SectionPatterns(include=(), exclude=("SKIP",))
    runs = partition_content(content, patterns)
    reassembled = "\n".join(run.text for run in runs)
    assert reassembled == content


def test_partition_reassembly_no_patterns_single_run() -> None:
    content = "one\ntwo\nthree"
    patterns = SectionPatterns(include=(), exclude=())
    runs = partition_content(content, patterns)
    assert len(runs) == 1
    assert runs[0].eligible is True
    assert runs[0].text == content


def test_partition_alternating_runs() -> None:
    content = "keep1\ncomp1\ncomp2\nkeep2"
    patterns = SectionPatterns(include=(), exclude=("keep",))
    runs = partition_content(content, patterns)
    # protected(keep1) | eligible(comp1,comp2) | protected(keep2)
    assert [(r.eligible, r.text) for r in runs] == [
        (False, "keep1"),
        (True, "comp1\ncomp2"),
        (False, "keep2"),
    ]
