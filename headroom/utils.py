"""Shared utilities for Headroom SDK."""

from __future__ import annotations

import copy
import hashlib
from typing import Any

# Marker format for Headroom modifications
MARKER_PREFIX = "<headroom:"
MARKER_SUFFIX = ">"


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


def create_marker(marker_type: str, **kwargs: Any) -> str:
    """
    Create a Headroom marker string.

    Args:
        marker_type: Type of marker (e.g., "tool_digest", "dropped_context").
        **kwargs: Attributes to include in the marker.

    Returns:
        Formatted marker string.
    """
    attrs = " ".join(f'{k}="{v}"' for k, v in kwargs.items())
    if attrs:
        return f"{MARKER_PREFIX}{marker_type} {attrs}{MARKER_SUFFIX}"
    return f"{MARKER_PREFIX}{marker_type}{MARKER_SUFFIX}"


def create_tool_digest_marker(original_hash: str) -> str:
    """Create marker for crushed tool output."""
    return create_marker("tool_digest", sha256=original_hash)


def deep_copy_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create a deep copy of messages list.

    Uses copy.deepcopy instead of json roundtrip (2-5x faster, avoids
    serialisation overhead on large conversation histories).
    """
    return copy.deepcopy(messages)
