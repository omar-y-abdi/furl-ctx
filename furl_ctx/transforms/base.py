"""Base transform interface for Furl SDK."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol

from ..config import TransformResult
from ..tokenizer import Tokenizer


class CompressionObserver(Protocol):
    """Structural contract for per-compression observers (TYPE-3).

    ``ContentRouter`` (and ``SmartCrusher``) call ``record_compression`` once
    per routing decision, always with keyword arguments; the router
    additionally forwards its merged ``route_counts`` dict once per
    ``apply()`` via ``record_router_route_counts``.

    ``record_router_route_counts`` is OPTIONAL at runtime: the router
    tolerates observers that predate it (its absence is swallowed as an
    ``AttributeError``), but it is part of the protocol so new
    implementations provide it and the forwarding call site type-checks.

    Observers must not raise; if one does anyway, callers swallow the
    exception at debug level — compression already succeeded, and a buggy
    metrics implementation must not break it.
    """

    def record_compression(
        self,
        *,
        strategy: str,
        original_tokens: int,
        compressed_tokens: int,
    ) -> None:
        """Record one routing decision's token movement."""
        ...

    def record_router_route_counts(self, route_counts: dict[str, int], /) -> None:
        """Receive the merged per-``apply()`` routing reason counters.

        Positional-only so implementations may name the parameter freely.
        """
        ...


class Transform(ABC):
    """Abstract base class for message transforms."""

    name: str = "base"

    @abstractmethod
    def apply(
        self,
        messages: list[dict[str, Any]],
        tokenizer: Tokenizer,
        **kwargs: Any,
    ) -> TransformResult:
        """
        Apply the transform to messages.

        Args:
            messages: List of message dicts to transform.
            tokenizer: Tokenizer for token counting.
            **kwargs: Additional transform-specific arguments.
                frozen_message_count: Number of leading messages in the
                    provider's prefix cache. Transforms should skip these
                    to avoid invalidating the cache.

        Returns:
            TransformResult with transformed messages and metadata.
        """
        pass

    def should_apply(
        self,
        messages: list[dict[str, Any]],
        tokenizer: Tokenizer,
        **kwargs: Any,
    ) -> bool:
        """
        Check if this transform should be applied.

        Default implementation always returns True.
        Override in subclasses for conditional application.

        Args:
            messages: List of message dicts.
            tokenizer: Tokenizer for token counting.
            **kwargs: Additional arguments.

        Returns:
            True if transform should be applied.
        """
        return True
