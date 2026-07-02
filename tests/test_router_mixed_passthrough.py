"""Byte-faithful mixed-content reassembly (COR-30).

Mixed reassembly is not byte-faithful even when every section passes through:
sections are re-joined with ``"\\n\\n"``, whitespace-only sections are dropped,
and code fences are re-synthesized — so direct ``compress()`` callers got
mutated bytes at ~zero savings with ``strategy_used=MIXED``. Fence markers
were also re-added AFTER ``compressed_tokens`` was counted, undercounting
fenced sections and overstating savings.

Pinned here:
* when NO section changed, the ORIGINAL string returns verbatim as
  PASSTHROUGH with honest zero-savings metrics;
* when a section DID change, reassembly still happens (MIXED);
* fenced sections count their tokens AFTER fence wrapping.
"""

from __future__ import annotations

import pytest

from furl_ctx.transforms.content_router import CompressionStrategy, ContentRouter

# Mixed content whose reassembly is NOT byte-identical: fence with language,
# a blank-heavy gap, and a whitespace-only segment the splitter drops.
MIXED_CONTENT = (
    "Intro prose describing The Overall Result Of The Run in several words.\n"
    "\n"
    "```python\n"
    "print('alpha')\n"
    "print('beta')\n"
    "```\n"
    "\n"
    "   \n"
    "\n"
    "Tail prose Summarizing What Happened After The Fence in more words."
)


def _identity_strategy(monkeypatch: pytest.MonkeyPatch, router: ContentRouter) -> None:
    monkeypatch.setattr(
        router,
        "_apply_strategy_to_content",
        lambda content, strategy, context, language=None, question=None, bias=1.0, **kwargs: (
            content,
            len(content.split()),
            [strategy.value],
        ),
    )


def test_unchanged_mixed_returns_verbatim_passthrough(monkeypatch):
    router = ContentRouter()
    _identity_strategy(monkeypatch, router)

    result = router._compress_mixed(MIXED_CONTENT, "ctx")

    # Byte-identical original — reassembly would have re-synthesized the
    # fence, joined with a normalized gap, and dropped the whitespace run.
    assert result.compressed == MIXED_CONTENT
    assert result.strategy_used is CompressionStrategy.PASSTHROUGH
    assert result.tokens_saved == 0
    assert result.compression_ratio == 1.0
    assert result.sections_processed >= 2


def test_changed_mixed_still_reassembles(monkeypatch):
    router = ContentRouter()
    monkeypatch.setattr(
        router,
        "_apply_strategy_to_content",
        lambda content, strategy, context, language=None, question=None, bias=1.0, **kwargs: (
            "shrunk output",
            2,
            [strategy.value],
        ),
    )

    result = router._compress_mixed(MIXED_CONTENT, "ctx")

    assert result.strategy_used is CompressionStrategy.MIXED
    assert result.compressed != MIXED_CONTENT
    assert "shrunk output" in result.compressed


def test_fenced_section_tokens_counted_after_wrapping(monkeypatch):
    """The fence bytes ship, so they count: a 2-word compressed fenced section
    records 4 words (``` + language line and closing ```)."""
    router = ContentRouter()
    monkeypatch.setattr(
        router,
        "_apply_strategy_to_content",
        lambda content, strategy, context, language=None, question=None, bias=1.0, **kwargs: (
            "xx yy",
            2,
            [strategy.value],
        ),
    )

    result = router._compress_mixed(MIXED_CONTENT, "ctx")

    fenced = [d for d in result.routing_log if d.content_type.value == "source_code"]
    assert fenced, "expected a fenced source_code section in the fixture"
    # "```python\nxx yy\n```" → ["```python", "xx", "yy", "```"] = 4 words.
    assert fenced[0].compressed_tokens == 4
