"""Unit tests for the pure filter/mode modules (NR2-2 features b, c).

These exercise the domain logic directly (no MCP server, no store), matching
the repo's "domain logic testable without mocks" rule:

* ``retrieve_filters`` — smart-constructor validation and the total apply step;
* ``compress_modes`` — mode parsing, the regex-first/glob-fallback matcher, and
  the byte-exact partition/reassembly invariant.
"""

from __future__ import annotations

import json
import time

import pytest

from furl_ctx.ccr.compress_modes import (
    CompressionMode,
    SectionPatterns,
    _pattern_matches,
    build_mode_pipeline,
    partition_content,
)
from furl_ctx.ccr.retrieve_filters import (
    FilteredContent,
    FilterError,
    RetrieveFilters,
    apply_filters,
)

# A generous wall-clock ceiling: the ReDoS guards make these paths return in
# microseconds. A real catastrophic backtrack would take many seconds/minutes,
# so < 1s cleanly separates "guarded" from "hung" without being flaky on a
# loaded CI box.
_REDOS_DEADLINE_S = 1.0
_CATASTROPHIC = r"(a+)+$"  # classic exponential-backtracking pattern

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


# ─── RetrieveFilters.parse — comparison boundaries ──────────────────────────
# One accepted/rejected pair straddling every ``<``/``>``/``<``-derived bound in
# the smart constructor, so a mutated operator (``>`` → ``>=`` etc.) is caught.


@pytest.mark.parametrize(
    ("context_lines", "accepted"),
    [(-1, False), (0, True), (50, True), (51, False)],  # cap is _MAX_CONTEXT_LINES=50
)
def test_context_lines_boundary(context_lines: int, accepted: bool) -> None:
    out = RetrieveFilters.parse({"context_lines": context_lines})
    assert isinstance(out, RetrieveFilters if accepted else FilterError)


@pytest.mark.parametrize(
    ("pattern_len", "accepted"),
    [(200, True), (201, False)],  # cap is _MAX_PATTERN_CHARS=200
)
def test_pattern_length_boundary(pattern_len: int, accepted: bool) -> None:
    out = RetrieveFilters.parse({"pattern": "a" * pattern_len})
    assert isinstance(out, RetrieveFilters if accepted else FilterError)


@pytest.mark.parametrize(
    ("line_bound", "accepted"),
    [(0, False), (1, True)],  # bound must be >= 1
)
def test_line_range_lower_bound_boundary(line_bound: int, accepted: bool) -> None:
    out = RetrieveFilters.parse({"line_range": [line_bound, None]})
    assert isinstance(out, RetrieveFilters if accepted else FilterError)


@pytest.mark.parametrize(
    ("start", "end", "accepted"),
    [(3, 3, True), (3, 2, False)],  # end must be >= start; equality is the edge
)
def test_line_range_end_vs_start_boundary(start: int, end: int, accepted: bool) -> None:
    out = RetrieveFilters.parse({"line_range": [start, end]})
    assert isinstance(out, RetrieveFilters if accepted else FilterError)


@pytest.mark.parametrize("raw", [[1], [1, 2, 3]])
def test_line_range_must_be_length_two(raw: list[int]) -> None:
    out = RetrieveFilters.parse({"line_range": raw})
    assert isinstance(out, FilterError)


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


# ─── apply_filters line-window boundaries ───────────────────────────────────
# The clamp arithmetic (``start > total`` → empty; ``end = min(total, end)``)
# lives only in ``_filter_lines`` and is unreachable from the parse tests.

_THREE_LINES = "l1\nl2\nl3"


def test_apply_line_range_start_past_eof_is_empty() -> None:
    # start (5) > total (3): the window begins past the last line → no lines,
    # but total_count still reports the whole original (not a crash, not an error).
    spec = RetrieveFilters.parse({"line_range": [5, None]})
    assert isinstance(spec, RetrieveFilters)
    out = apply_filters(_THREE_LINES, spec)
    assert isinstance(out, FilteredContent)
    assert out.content == ""
    assert out.matched_count == 0
    assert out.total_count == 3


def test_apply_line_range_end_past_eof_clamps() -> None:
    # end (99) is clamped to total (3); start (2) is in range → lines 2..3 only.
    spec = RetrieveFilters.parse({"line_range": [2, 99]})
    assert isinstance(spec, RetrieveFilters)
    out = apply_filters(_THREE_LINES, spec)
    assert isinstance(out, FilteredContent)
    assert out.content == "2:l2\n3:l3"
    assert out.matched_count == 2
    assert out.total_count == 3


def test_apply_pattern_numbers_matched_lines() -> None:
    # Pattern selection over the full window keeps original 1-based line numbers.
    spec = RetrieveFilters.parse({"pattern": "ERROR"})
    assert isinstance(spec, RetrieveFilters)
    out = apply_filters("alpha\nERROR two\ngamma", spec)
    assert isinstance(out, FilteredContent)
    assert out.content == "2:ERROR two"
    assert out.matched_count == 1
    assert out.total_count == 3


def test_apply_pattern_matches_substring_in_single_giant_line_review_f3() -> None:
    # Review F3: a minified / single-line JSON blob stores as ONE line far
    # longer than the per-line regex cap. A backtracking-bounded pattern (a
    # literal) must still find a substring that is unambiguously present — pre-
    # fix the over-long line was silently skipped and matched_count came back 0.
    blob = '{"exception":{"type":"EXC_BAD_ACCESS","signal":"SIGSEGV"},"frames":['
    blob += ",".join(f'{{"sym":"frame_{i}","off":{i}}}' for i in range(2000))
    blob += "]}"
    assert "\n" not in blob and len(blob) > 10_000
    for pat in ("EXC_BAD_ACCESS", "SIGSEGV", "exception"):
        spec = RetrieveFilters.parse({"pattern": pat})
        assert isinstance(spec, RetrieveFilters)
        out = apply_filters(blob, spec)
        assert isinstance(out, FilteredContent)
        assert out.matched_count == 1, f"{pat!r} present in the single line but not matched"
        assert out.total_count == 1


def test_apply_regex_metachar_pattern_over_long_line_stays_capped_review_rf1() -> None:
    # Review RF1: ONLY a pure LITERAL bypasses the per-line cap. A pattern
    # carrying any regex metacharacter (here '.') is a regex, so on a line longer
    # than the cap it is NOT searched and reports zero matches even though its
    # target is present. The same regex on a SHORT line DOES match, proving the
    # zero is the long-line cap at work, not a malformed pattern. Pre-fix the
    # quantifier heuristic cleared '.' (it carries no '* + {') and ran it on the
    # giant line, returning a match — the behavior RF1 removes; this asserts 0.
    needle = "EXC_BAD_ACCESS"
    blob = '{"exception":{"type":"' + needle + '"},"frames":['
    blob += ",".join(f'{{"sym":"frame_{i}","off":{i}}}' for i in range(2000))
    blob += "]}"
    assert "\n" not in blob and len(blob) > 10_000
    regex = "EXC.BAD.ACCESS"  # the '.' metacharacters match the underscores

    spec = RetrieveFilters.parse({"pattern": regex})
    assert isinstance(spec, RetrieveFilters)
    out = apply_filters(blob, spec)
    assert isinstance(out, FilteredContent)
    assert out.matched_count == 0, "a regex must not search a line beyond the cap"

    # Control: the very same regex matches on a short line, so the zero above is
    # purely the long-line cap, not the pattern failing to match.
    short = RetrieveFilters.parse({"pattern": regex})
    assert isinstance(short, RetrieveFilters)
    out_short = apply_filters(needle, short)
    assert isinstance(out_short, FilteredContent)
    assert out_short.matched_count == 1


def test_apply_optional_heavy_pattern_over_long_line_returns_fast_review_rf1() -> None:
    # Review RF1 + A12: a '?'-heavy pattern backtracks exponentially yet carries
    # no '* + {', so the retired quantifier heuristic cleared it and ran it on a
    # giant line, hanging for tens of seconds. RF1 first confined it to the
    # per-line cap; A12 now rejects the optional-chain shape one step earlier, at
    # PARSE, so it never reaches the matcher on a short OR a long line (the cap
    # bounds line length, not backtracking width, so it never protected short
    # lines). Either way the security property — no hang — holds; this pins the
    # stronger parse-time rejection. The long-line input cap itself is pinned
    # independently by test_regex_line_char_cap_boundary.
    pattern = ".?" * 21 + "." * 21 + "END"  # 21 optional quantifiers > the bound of 12
    start = time.monotonic()
    spec = RetrieveFilters.parse({"pattern": pattern})
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"pattern hung at parse ({elapsed:.1f}s)"
    assert isinstance(spec, FilterError)
    assert "variable-length quantifier" in spec.reason


# ─── CompressionMode.parse ──────────────────────────────────────────────────


def test_mode_parse_defaults_to_normal_on_none() -> None:
    assert CompressionMode.parse(None) is CompressionMode.NORMAL


def test_mode_parse_case_insensitive() -> None:
    assert CompressionMode.parse("AGGRESSIVE") is CompressionMode.AGGRESSIVE


def test_mode_parse_unknown_is_error_string() -> None:
    out = CompressionMode.parse("turbo")
    assert isinstance(out, str)
    assert "unknown mode" in out


# ─── build_mode_pipeline: mode → router config (behavior, not readback) ──────
# The mode's whole point is that it changes the ContentRouterConfig the pipeline
# carries. Pin each mode's config to a fixed vector so a swapped/dropped knob is
# caught here (the MCP tests only observe the emergent token delta downstream).


def test_normal_mode_uses_default_pipeline() -> None:
    # NORMAL returns None so the caller reuses the process default singleton —
    # this is what keeps a default compress call byte-identical.
    assert build_mode_pipeline(CompressionMode.NORMAL) is None


def test_lossless_only_pipeline_config() -> None:
    pipeline = build_mode_pipeline(CompressionMode.LOSSLESS_ONLY)
    assert pipeline is not None
    config = pipeline.transforms[-1].config  # ContentRouter is the last transform
    assert config.lossless_only is True
    # The other knobs stay at their defaults for lossless_only.
    assert config.min_ratio_relaxed == 0.85
    assert config.min_ratio_aggressive == 0.95


def test_aggressive_pipeline_config() -> None:
    pipeline = build_mode_pipeline(CompressionMode.AGGRESSIVE)
    assert pipeline is not None
    config = pipeline.transforms[-1].config
    assert config.lossless_only is False
    # Turned-up acceptance thresholds + a low kept-items cap distinguish
    # aggressive from both default (0.85/0.95/None) and lossless_only.
    assert config.smart_crusher_max_items_after_crush == 5
    assert config.min_ratio_relaxed == 0.98
    assert config.min_ratio_aggressive == 0.99


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


# ─── SEC-1/SEC-2: ReDoS guards ──────────────────────────────────────────────
# Two complementary guards, both exercised here:
#   (a) an input cap so a catastrophic pattern over a LONG line returns at once
#       instead of backtracking (the matcher never sees the over-cap line);
#   (b) a parse-time heuristic that rejects the pathological pattern shape as a
#       typed error before it is ever compiled/run.


def test_compress_catastrophic_pattern_over_long_line_returns_fast() -> None:
    # _pattern_matches bypasses parse (the unit/tool-name path), so the INPUT
    # CAP is what protects it: a 20k-char line is past the 10k cap → no match,
    # no backtracking. Without the cap, (a+)+$ over this line hangs for minutes.
    long_line = "a" * 20_000
    start = time.monotonic()
    result = _pattern_matches(_CATASTROPHIC, long_line)
    assert time.monotonic() - start < _REDOS_DEADLINE_S
    assert result is False  # over-cap line resolves conservatively to no-match


def test_compress_section_filter_over_long_line_is_protected_and_fast() -> None:
    # Same guard through the public partition path: an over-cap line stays fast
    # (the matcher never runs on it) AND, per the conservative safety choice, is
    # treated as PROTECTED — an unfilterable line ships verbatim rather than
    # being compressed. The short line is ordinary (eligible).
    long_line = "a" * 20_000
    content = "short ok line\n" + long_line
    patterns = SectionPatterns(include=(), exclude=(_CATASTROPHIC,))
    start = time.monotonic()
    runs = partition_content(content, patterns)
    assert time.monotonic() - start < _REDOS_DEADLINE_S
    # Byte-exact reassembly still holds regardless of the split.
    assert "\n".join(run.text for run in runs) == content
    # The over-long line is protected (ineligible); it must not be compressed.
    assert patterns.line_is_eligible(long_line) is False
    long_run = next(run for run in runs if long_line in run.text)
    assert long_run.eligible is False


def test_compress_over_long_line_protected_even_with_include_filter() -> None:
    # With an include filter present, an over-long line is still protected — the
    # length guard fires before include matching, so the conservative outcome is
    # consistent regardless of which filter family is in play.
    long_line = "b" * 20_000
    patterns = SectionPatterns(include=("keepme",), exclude=())
    assert patterns.line_is_eligible(long_line) is False


def test_compress_parse_rejects_nested_quantifier_pattern() -> None:
    out = SectionPatterns.parse({"include_patterns": [_CATASTROPHIC]})
    assert isinstance(out, str)  # the parse-error channel (not a hang, not a crash)
    assert "nested unbounded quantifier" in out


def test_compress_parse_rejects_overlong_pattern() -> None:
    out = SectionPatterns.parse({"exclude_patterns": ["a" * 201]})
    assert isinstance(out, str)
    assert "too long" in out


def test_retrieve_catastrophic_pattern_over_long_line_returns_fast() -> None:
    # A benign compiled pattern over a single 50k-char line: the input cap skips
    # the over-cap line from selection, so the per-line search never runs on it.
    # (A catastrophic pattern is rejected at parse — covered below — so the cap
    # is the guard for lines that slip past a heuristic-safe pattern.)
    content = "a" * 50_000
    spec = RetrieveFilters.parse({"pattern": "zzz"})
    assert isinstance(spec, RetrieveFilters)
    start = time.monotonic()
    out = apply_filters(content, spec)
    assert time.monotonic() - start < _REDOS_DEADLINE_S
    assert isinstance(out, FilteredContent)
    assert out.matched_count == 0  # over-cap line skipped from pattern selection


def test_retrieve_parse_rejects_nested_quantifier_pattern() -> None:
    out = RetrieveFilters.parse({"pattern": _CATASTROPHIC})
    assert isinstance(out, FilterError)
    assert "nested unbounded quantifier" in out.reason


def test_retrieve_parse_rejects_overlong_pattern() -> None:
    out = RetrieveFilters.parse({"pattern": "a" * 201})
    assert isinstance(out, FilterError)
    assert "too long" in out.reason


def test_redos_guards_preserve_normal_patterns_byte_identically() -> None:
    # The must-preserve normal patterns are neither over-long nor nested, so
    # they parse and match exactly as before the guards.
    assert _pattern_matches("ERROR.*", "line with ERROR here") is True
    assert _pattern_matches("*.py", "module.py") is True  # glob fallback intact
    assert _pattern_matches(r"^\d+$", "12345") is True
    assert _pattern_matches(r"^\d+$", "12a45") is False
    spec = RetrieveFilters.parse({"pattern": "ERROR.*"})
    assert isinstance(spec, RetrieveFilters)  # not rejected
    section = SectionPatterns.parse({"include_patterns": ["*.py"], "exclude_patterns": ["ERROR.*"]})
    assert isinstance(section, SectionPatterns)  # not rejected


def test_regex_line_char_cap_boundary() -> None:
    # Exactly at the cap the line is still matched; one char over, it is skipped.
    assert _pattern_matches("a+", "a" * 10_000) is True
    assert _pattern_matches("a+", "a" * 10_001) is False


# ─── A12: optional-chain ReDoS on SHORT (within-cap) lines ──────────────────
# The long-line literal-only path (F3) already protects lines OVER the cap. A12
# is the short-line gap: an optional-chain like '.?' repeated dozens of times is
# under _MAX_PATTERN_CHARS and carries no nested quantifier, yet backtracks
# exponentially on a line WITHIN the 10k cap (empirically '.?'×22 + a failing
# tail ≈ 1.5s, doubling per added pair — '.?'×40 hangs for hours). The input cap
# bounds line LENGTH, not backtracking WIDTH, so it does not save short lines.
# The fix rejects the shape at parse; these pin that it stays fast.

# '.?' × 40 followed by a literal that forces the exponential search to fail.
# 81 chars (< 200 cap), no nested quantifier — invisible to the other two
# screens; only the variable-quantifier count (40 > 12) catches it.
_OPTIONAL_CHAIN = ".?" * 40 + "b"


def test_retrieve_parse_rejects_optional_chain_pattern() -> None:
    out = RetrieveFilters.parse({"pattern": _OPTIONAL_CHAIN})
    assert isinstance(out, FilterError)
    assert "variable-length quantifier" in out.reason


def test_compress_parse_rejects_optional_chain_pattern() -> None:
    out = SectionPatterns.parse({"include_patterns": [_OPTIONAL_CHAIN]})
    assert isinstance(out, str)  # the parse-error channel (not a hang, not a crash)
    assert "variable-length quantifier" in out


def test_retrieve_optional_chain_within_cap_is_guarded_and_fast() -> None:
    # The end-to-end wall-clock pin. On a SHORT within-cap line the guard must
    # keep the public parse→apply path fast. With the guard, parse returns a
    # FilterError at once. If a future change drops the guard, parse yields a
    # spec, apply_filters runs '.?'×40 over the line, and this backtracks for
    # far longer than the deadline — a loud regression signal.
    line = "a" * 40  # within the 10k cap; forces the exponential blow-up
    start = time.monotonic()
    spec = RetrieveFilters.parse({"pattern": _OPTIONAL_CHAIN})
    if isinstance(spec, RetrieveFilters):  # only reachable if the guard regresses
        apply_filters(line, spec)
    elapsed = time.monotonic() - start
    assert elapsed < _REDOS_DEADLINE_S, f"optional-chain not guarded: {elapsed:.2f}s"
    assert isinstance(spec, FilterError)


def test_variable_quantifier_guard_preserves_normal_patterns() -> None:
    # A handful of quantifiers (the realistic case) must still pass both surfaces.
    for ok in ("ERROR.*", r"^\d+$", "a?b?c?", r"\d{1,3}-\d{1,3}", "(?:foo)?(?:bar)?"):
        assert isinstance(RetrieveFilters.parse({"pattern": ok}), RetrieveFilters), ok
    # The boundary: exactly _MAX_VARIABLE_QUANTIFIERS (12) passes; 13 is rejected.
    # Distinct atoms so each '?' is a valid quantifier (stacked '???' is a regex
    # error, a different rejection channel).
    twelve = "".join(f"{c}?" for c in "abcdefghijkl")  # 12 optional quantifiers
    assert isinstance(RetrieveFilters.parse({"pattern": twelve}), RetrieveFilters)
    assert isinstance(RetrieveFilters.parse({"pattern": twelve + "m?"}), FilterError)
