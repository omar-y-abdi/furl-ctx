"""Token counting wrapper for Furl SDK.

This module provides a unified interface for token counting that
delegates to provider-specific implementations.
"""

from __future__ import annotations

from typing import Any

from .tokenizers.base import TokenCounter


class Tokenizer:
    """
    Token counting wrapper with model awareness.

    This class wraps a provider-specific TokenCounter to provide
    a consistent interface throughout the Furl SDK.
    """

    def __init__(self, token_counter: TokenCounter, model: str = ""):
        """
        Initialize tokenizer with a provider's token counter.

        Args:
            token_counter: Provider-specific token counter.
            model: Model name (for reference only).
        """
        self._counter = token_counter
        self.model = model

    def count_text(self, text: str) -> int:
        """Count tokens in text."""
        return self._counter.count_text(text)

    def count_message(self, message: dict[str, Any]) -> int:
        """Count tokens in a message."""
        return self._counter.count_message(message)

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        """Count tokens in a list of messages."""
        return self._counter.count_messages(messages)
