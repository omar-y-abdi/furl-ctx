"""HTML main-content extraction: sniff, extract, byte-exact recovery, veto."""

from __future__ import annotations

from furl_ctx.cache.compression_store import (
    get_compression_store,
    reset_compression_store,
)
from furl_ctx.ccr.marker_grammar import BRACKET_RETRIEVE_PATTERN
from furl_ctx.tokenizers import get_tokenizer
from furl_ctx.transforms import html_ingest
from furl_ctx.transforms.content_router import ContentRouter
from furl_ctx.transforms.csv_ingest import raw_recovery_hash
from furl_ctx.transforms.html_ingest import extract_main_content, sniff_html

_COUNT = get_tokenizer("gpt-4o").count_text


def _page() -> str:
    return (
        "<!DOCTYPE html><html><head><style>.x{color:red}</style>"
        "<script>var a=1;track()</script></head><body>"
        "<nav>Home About Contact Login Signup Menu</nav>"
        "<article><h1>Real Title</h1><p>"
        + ("The actual article content here. " * 40)
        + "</p></article>"
        "<footer>Copyright 2026 Boilerplate Inc. All rights reserved.</footer>"
        "</body></html>"
    )


def test_sniff_hits_html_and_misses_prose() -> None:
    assert sniff_html(_page()) is True
    assert sniff_html("<html><body>x</body></html>") is True
    assert sniff_html("just some plain prose, no tags at all") is False
    assert sniff_html('{"data": [1, 2, 3]}') is False


def test_extract_keeps_article_drops_boilerplate() -> None:
    text = extract_main_content(_page())
    assert "Real Title" in text
    assert "actual article content" in text
    assert "var a=1" not in text  # <script> dropped
    assert "color:red" not in text  # <style> dropped
    assert "Copyright 2026" not in text  # <footer> dropped


def test_recovery_byte_exact_html_through_real_router() -> None:
    reset_compression_store()
    try:
        raw = _page()
        result = ContentRouter().compress(raw, token_counter=_COUNT)
        assert result.compressed != raw
        assert _COUNT(result.compressed) < _COUNT(raw)
        assert "Real Title" in result.compressed  # main content shipped inline

        match = BRACKET_RETRIEVE_PATTERN.search(result.compressed)
        assert match is not None, f"recovery marker missing: {result.compressed!r}"
        assert match.group(3) == raw_recovery_hash(raw)
        entry = get_compression_store().retrieve(match.group(3))
        assert entry is not None and entry.original_content == raw  # full HTML recoverable
    finally:
        reset_compression_store()


def test_compress_html_vetoes_on_persist_failure(monkeypatch) -> None:
    monkeypatch.setattr(html_ingest, "persist_to_python_ccr", lambda *a, **k: False)
    out = html_ingest.compress_html(_page(), token_counter=len)
    assert out is None  # vetoed → no dangling marker


def test_adjacent_table_cells_do_not_fuse() -> None:
    # Two cells "5" and "3" must never read as the number "53" in the
    # extracted view the model consumes.
    assert extract_main_content("<table><tr><td>5</td><td>3</td></tr></table>") == "5 3"


def test_table_rows_keep_cell_and_row_structure() -> None:
    html = "<table><tr><th>Name</th><th>Age</th></tr><tr><td>Ann</td><td>7</td></tr></table>"
    assert extract_main_content(html) == "Name Age\nAnn 7"


def test_inline_whitespace_between_elements_is_preserved() -> None:
    # The whitespace between two inline elements arrives as its own data
    # event; it must collapse to one space (browser semantics), not vanish.
    assert extract_main_content("<p><span>Hello</span>\n  <span>World</span></p>") == "Hello World"


def test_text_after_block_close_does_not_fuse() -> None:
    assert extract_main_content("<div>alpha</div>beta") == "alpha\nbeta"
    assert extract_main_content("<ul><li>one</li>two</ul>") == "one\ntwo"


def test_deliberately_adjacent_inline_text_still_fuses() -> None:
    # No whitespace in the source between the <b> runs: a browser renders "10USD", so the extractor must
    # too — separators are only ever reintroduced where the source had a boundary (whitespace/cell/block).
    assert extract_main_content("<p>Price <b>10</b><b>USD</b> total</p>") == "Price 10USD total"


def test_intra_data_spacing_and_block_lines_unchanged() -> None:
    assert extract_main_content("<p>keep <b>bold</b> spacing</p>") == "keep bold spacing"
    assert extract_main_content("<p>one</p><p>two</p>") == "one\ntwo"
