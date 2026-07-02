"""Regression test for #17: explicit None text in a content block raised TypeError.

A content block ``{"type": "text", "text": None}`` is conformant (some clients
emit it). The parser used ``part.get("text", "")``, whose default applies only
to an ABSENT key — an explicit ``None`` value passed straight through and then
crashed ``"\\n".join(...)`` with a TypeError. Upstream (pipeline) that TypeError
was swallowed and silently disabled waste-signal diagnostics.

Fix: coerce to "" (``part.get("text") or ""``) at all three sites. Compression
output is unaffected (this is diagnostics/parsing).

Mutation-sensitive: reverting to ``.get("text", "")`` re-raises TypeError.
"""

from __future__ import annotations

import pytest

from furl_ctx.parser import (
    _extract_tool_result_text,
    get_message_content_text,
    parse_message_to_blocks,
)
from furl_ctx.tokenizers import get_tokenizer


def test_extract_tool_result_text_handles_none() -> None:
    # site :70
    out = _extract_tool_result_text({"content": [{"type": "text", "text": None}]})
    assert out == ""


def test_get_message_content_text_handles_none() -> None:
    # site :444
    out = get_message_content_text({"role": "user", "content": [{"type": "text", "text": None}]})
    assert out == ""


def test_parse_message_to_blocks_handles_none() -> None:
    # site :167 — must not raise; produces a parseable result.
    tokenizer = get_tokenizer("gpt-4o")
    result = parse_message_to_blocks(
        {"role": "user", "content": [{"type": "text", "text": None}]},
        0,
        tokenizer,
    )
    assert result is not None


@pytest.mark.parametrize(
    "block,expected",
    [
        ({"type": "text", "text": None}, ""),  # explicit None
        ({"type": "text", "text": ""}, ""),  # empty string preserved
        ({"type": "text", "text": "hello"}, "hello"),  # normal value preserved
        ({"type": "text"}, ""),  # absent key
    ],
)
def test_text_coercion_matrix(block: dict, expected: str) -> None:
    out = get_message_content_text({"role": "user", "content": [block]})
    assert out == expected


def test_none_text_mixed_with_real_text() -> None:
    # A None block adjacent to a real block: the real text survives, no crash.
    out = get_message_content_text(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": None},
                {"type": "text", "text": "kept"},
            ],
        }
    )
    assert "kept" in out
