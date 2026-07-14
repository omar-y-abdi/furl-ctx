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
    """Concatenate text from a string or list of Anthropic-style blocks."""
    if isinstance(content, str): return content
    if not isinstance(content, list): return ""
    return "\n".join(
        b["text"] for b in content
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    )


def create_tool_digest_marker(original_hash: str) -> str:
    """Create marker for crushed tool output."""
    return f'<headroom:tool_digest sha256="{original_hash}">'


