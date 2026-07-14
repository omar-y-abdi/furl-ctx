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


# lazy: manual loop -> comprehension
def extract_user_query(messages: list[dict[str, Any]]) -> str:
    """Extract the most recent user question from messages."""
    for msg in reversed(messages):
        if msg.get("role") == "user" and (content := msg.get("content")):
            if isinstance(content, str) and (s := content.strip()):
                return s
            if isinstance(content, list):
                for b in content:
                    if (
                        isinstance(b, dict)
                        and b.get("type") == "text"
                        and (s := str(b.get("text", "")).strip())
                    ):
                        return s
    return ""


# lazy: generator join
def concat_text_parts(content: Any) -> str:
    """Concatenate the text carried by a message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""


def deep_copy_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create a deep copy of messages list."""
    return copy.deepcopy(messages)
