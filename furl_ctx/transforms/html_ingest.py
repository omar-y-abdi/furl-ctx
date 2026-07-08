"""HTML main-content extraction — strip boilerplate, keep the article.

WebFetch output is HTML that otherwise routes to prose compression, where the
nav/script/style boilerplate (most of the bytes) survives. This extracts the
readable main content with the stdlib HTML parser — **no trafilatura/readability
dependency** — and ships it with a marker that recovers the FULL original HTML
byte-exact from the CCR store, so the extraction is lossy-but-reversible, never a
silent loss.

lazy: a heuristic extractor (drop a fixed boilerplate-tag set, keep the rest), not
a full readability algorithm. Heavy-JS/SPA pages with little server-rendered text
extract poorly — but the original is always CCR-recoverable, so an imperfect
extraction costs tokens, never data. Upgrade path: a scoring-based main-content
picker, still stdlib.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from html.parser import HTMLParser

from ._ccr_persist import persist_to_python_ccr
from .csv_ingest import raw_recovery_hash

logger = logging.getLogger(__name__)

# Tags whose entire text content is boilerplate — dropped.
_SKIP_TAGS = frozenset(
    {"script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "form", "template"}
)
# Block tags: their boundaries become newlines so extracted text keeps structure.
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "br",
        "tr",
        "blockquote",
    }
)


class _MainContentExtractor(HTMLParser):
    """Collect visible text, dropping the content of boilerplate tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def sniff_html(content: str) -> bool:
    """Heuristic: is *content* an HTML document/fragment worth extracting?"""
    head = content.lstrip()[:256].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        return True
    lowered = content.lower()
    # A fragment: needs a couple of distinct structural tags and a close tag.
    signals = sum(
        tag in lowered for tag in ("<body", "<div", "<p>", "<p ", "<span", "<article", "<table")
    )
    return signals >= 2 and "</" in lowered


def extract_main_content(html: str) -> str:
    """Extract readable main content; returns '' if parsing yields nothing."""
    parser = _MainContentExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed HTML — fail open to no extraction
        return ""
    return parser.text()


def compress_html(content: str, *, token_counter: Callable[[str], int]) -> str | None:
    """Extract the HTML's main content; ship extracted text + recovery marker.

    Returns None (serve the raw HTML) when the content is not HTML, extraction is
    empty, the extracted render does not beat the raw bytes, or the raw-recovery
    store write is vetoed. The full original HTML is stored under ``key`` so
    retrieval restores it byte-exact.
    """
    if not sniff_html(content):
        return None
    extracted = extract_main_content(content)
    if not extracted:
        return None

    key = raw_recovery_hash(content)
    marker = f"[1 html compressed to 0. Retrieve more: hash={key}]"
    candidate = f"{extracted}\n{marker}"

    if token_counter(candidate) >= token_counter(content):
        return None

    if not persist_to_python_ccr(
        content, candidate, key, compression_strategy="html", logger=logger
    ):
        return None  # store veto — serve the raw HTML, no dangling marker

    logger.info(
        "html ingest: %d chars extracted from %d; raw recoverable at hash=%s",
        len(extracted),
        len(content),
        key,
    )
    return candidate
