"""Base provider protocol for Headroom SDK.

Defines the `TokenCounter` protocol that token-counting implementations
satisfy (model-specific token counting for the compression core).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TokenCounter(Protocol):
    """Protocol for token counting implementations."""

    def count_text(self, text: str) -> int:
        """Count tokens in a text string."""
        ...

    def count_message(self, message: dict[str, Any]) -> int:
        """Count tokens in a single message dict."""
        ...

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        """Count tokens in a list of messages."""
        ...
