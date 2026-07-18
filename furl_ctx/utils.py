"""Shared utilities for Furl SDK."""

from __future__ import annotations

import copy
import hashlib
from typing import Any


def compute_hash(data: str | bytes) -> str:
    """Compute SHA256 hash, returning hex string."""
    if isinstance(data, str):
        data = data.encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(data).hexdigest()


def compute_short_hash(data: str | bytes, length: int = 16) -> str:
    """Compute truncated SHA256 hash."""
    return compute_hash(data)[:length]


def extract_user_query(messages: list[dict[str, Any]]) -> str:
    """Extract the most recent user question from messages.

    Used to pass context through the compression pipeline so transforms like
    SmartCrusher can score items by relevance to the user's actual question,
    not just by statistical properties (position, anomaly, boundary).
    """
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = str(block.get("text", "")).strip()
                        if text:
                            return text
    return ""


def concat_text_parts(content: Any) -> str:
    """Concatenate the text carried by a message ``content`` field.

    Providers accept two content shapes: a plain string, or a list of typed
    blocks (Anthropic-style ``{"type": "text", "text": ...}``). Transforms
    that inspect prompt text (CacheAligner hashing/detection, ContentRouter
    analysis-intent detection) must see BOTH shapes, or block-format prompts
    silently vanish from their view (COR-53).

    - ``str`` content is returned unchanged, so callers hashing plain-string
      prompts stay byte-identical to their pre-helper behavior.
    - ``list`` content yields the ``text`` of every ``{"type": "text"}`` block
      whose ``text`` is a string, joined by newlines. Non-text blocks (images,
      tool_use, ...) and malformed entries contribute nothing.
    - Any other shape (``None``, dict, int, ...) yields ``""``.

    Total: never raises on untrusted content shapes.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def create_tool_digest_marker(original_hash: str) -> str:
    """Create marker for crushed tool output.

    Uses the ``<furl:...>`` namespace (A11): the residual ``<headroom:...>``
    branding from the upstream lineage was renamed to Furl's own name. This is a
    display annotation on the compressed view, not a stored-then-retrieved marker,
    so the rename touches no persisted bytes; it aligns with the Rust
    tag-protector docs, which already reference ``<furl:tool_digest>``."""
    return f'<furl:tool_digest sha256="{original_hash}">'


def deep_copy_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create a deep copy of messages list.

    Uses copy.deepcopy instead of json roundtrip (2-5x faster, avoids
    serialisation overhead on large conversation histories).
    """
    return copy.deepcopy(messages)
