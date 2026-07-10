"""Direct behavioral tests for the Drain-style ``log_template_miner``.

The miner is covered only incidentally (as a callee of the log compressor). These
tests exercise ``mine`` and the frozen ``Template`` API directly against corpora
whose templates are hand-derived, pinning the losslessness crux: every mined line
reconstructs BYTE-EXACT from ``(template, params)``.

Tokenisation mirrors the caller's ``re.findall(r"\\s+|\\S+", content)`` so that
``"".join(tokens) == content`` — the invariant the miner relies on for exact
reconstruction.
"""

from __future__ import annotations

import re

import pytest

from furl_ctx.transforms import log_template_miner as lm


def _tok(line: str) -> tuple[str, ...]:
    """Concatenation-preserving tokenisation, identical to the miner's caller."""
    tokens = tuple(re.findall(r"\s+|\S+", line))
    assert "".join(tokens) == line, "tokenisation must preserve the original bytes"
    return tokens


def _mine(lines: list[str]) -> lm.MiningResult:
    return lm.mine(tuple(_tok(line) for line in lines))


def test_similar_lines_merge_into_one_template_with_a_single_wildcard() -> None:
    """Two lines identical except one trailing token collapse to one 1-param template.

    The differing token sits AFTER the depth-4 prefix, so both lines share a
    bucket and merge; the varying position becomes the sole wildcard.
    """
    result = _mine(["INFO worker started task 42", "INFO worker started task 99"])
    assert len(result.templates) == 1
    template = result.templates[0]
    assert template.param_count == 1
    assert template.wildcard_positions() == (8,)
    assert [m.template_id for m in result.matches] == [0, 0]
    assert [m.params for m in result.matches] == [("42",), ("99",)]


def test_multi_template_corpus_has_the_hand_derived_shape() -> None:
    """A mixed corpus mines into exactly the two templates its structure implies.

    Lines 0-2 share the 13-token 'INFO worker started task N in Nms' shape (two
    varying positions -> 2 params at indices 8 and 12); lines 3-4 share the
    11-token 'WARN cache miss for key X' shape (1 param at index 10).
    """
    corpus = [
        "INFO worker started task 1 in 10ms",
        "INFO worker started task 2 in 20ms",
        "INFO worker started task 3 in 30ms",
        "WARN cache miss for key alpha",
        "WARN cache miss for key beta",
    ]
    result = _mine(corpus)
    assert len(result.templates) == 2

    by_id = result.template_by_id()
    assert by_id[0].param_count == 2
    assert by_id[0].wildcard_positions() == (8, 12)
    assert by_id[1].param_count == 1
    assert by_id[1].wildcard_positions() == (10,)

    assert [m.template_id for m in result.matches] == [0, 0, 0, 1, 1]
    assert [m.params for m in result.matches] == [
        ("1", "10ms"),
        ("2", "20ms"),
        ("3", "30ms"),
        ("alpha",),
        ("beta",),
    ]


def test_every_mined_line_reconstructs_byte_exact() -> None:
    """Losslessness: render(template, params) rebuilds each original line verbatim."""
    corpus = [
        "INFO worker started task 1 in 10ms",
        "INFO worker started task 2 in 20ms",
        "INFO worker started task 3 in 30ms",
        "WARN cache miss for key alpha",
        "WARN cache miss for key beta",
    ]
    result = _mine(corpus)
    by_id = result.template_by_id()
    rebuilt = [by_id[m.template_id].render_with_params(m.params) for m in result.matches]
    assert rebuilt == corpus


def test_distinct_length_buckets_never_merge() -> None:
    """Lines with different token counts land in separate templates (bucket-by-length)."""
    result = _mine(["INFO worker started task 42", "ERROR db down"])
    assert len(result.templates) == 2
    assert sorted(t.template_id for t in result.templates) == [0, 1]
    assert all(t.param_count == 0 for t in result.templates)  # nothing merged -> no wildcards


@pytest.mark.parametrize("bad_params", [(), ("only-one",), ("a", "b", "c")])
def test_render_with_wrong_param_count_raises_valueerror(bad_params: tuple[str, ...]) -> None:
    """Rendering with a param count != the wildcard count is a loud ValueError.

    The template below has exactly 2 wildcards; supplying 0, 1, or 3 params all fail.
    """
    template = _mine(
        ["INFO worker started task 1 in 10ms", "INFO worker started task 2 in 20ms"]
    ).templates[0]
    assert template.param_count == 2
    with pytest.raises(ValueError, match=r"expects 2 params, got \d+"):
        template.render_with_params(bad_params)


def test_is_wildcard_distinguishes_sentinel_from_real_tokens() -> None:
    """``is_wildcard`` is True only for the internal sentinel, never a str token."""
    template = _mine(["INFO worker started task 42", "INFO worker started task 99"]).templates[0]
    wildcard_index = template.wildcard_positions()[0]
    assert lm.is_wildcard(template.tokens[wildcard_index]) is True
    assert lm.is_wildcard(template.tokens[0]) is False  # the literal "INFO"
    assert lm.is_wildcard("42") is False


def test_token_count_key_and_prefix_key_skip_whitespace_for_indexing() -> None:
    """The length key counts every token; the prefix key uses leading NON-ws tokens."""
    tokens = _tok("  INFO worker started task done")  # leading indent + 5 words
    assert lm.token_count_key(tokens) == len(tokens)
    # Whitespace (the leading indent and inter-word spaces) is skipped for the
    # depth-4 prefix, so the key is the first four meaningful tokens.
    assert lm.prefix_key(tokens, 4) == ("INFO", "worker", "started", "task")


def test_empty_corpus_yields_no_templates_and_no_matches() -> None:
    """Totality edge: mining zero lines returns empty, parallel result tuples."""
    result = lm.mine(())
    assert result.templates == ()
    assert result.matches == ()


def test_two_empty_token_lines_are_structurally_identical() -> None:
    """Edge: two zero-token lines merge into one 0-param template (trivially similar).

    Exercises the empty-sequence guard in the similarity check — two empty lines
    are defined as identical in structure, so they share a template and each
    renders back to the empty string.
    """
    result = lm.mine(((), ()))
    assert len(result.templates) == 1
    assert result.templates[0].param_count == 0
    assert [m.template_id for m in result.matches] == [0, 0]
    assert result.templates[0].render_with_params(()) == ""
