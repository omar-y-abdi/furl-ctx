"""
Base types for cache optimization.

The provider-specific cache optimizers (and their I/O dataclasses + the
``CacheOptimizer`` protocol / ``BaseCacheOptimizer`` ABC) were retired with the
public SDK surface. This module now exposes only the shared configuration
types that survive that retirement: ``CacheStrategy`` and ``CacheConfig``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class CacheStrategy(Enum):
    """Cache optimization strategy."""

    # Just stabilize prefix (move dates, normalize whitespace)
    PREFIX_STABILIZATION = "prefix_stabilization"

    # Insert explicit cache breakpoints (Anthropic)
    EXPLICIT_BREAKPOINTS = "explicit_breakpoints"

    # Manage separate cached content objects (Google)
    CACHED_CONTENT = "cached_content"

    # No optimization possible (provider doesn't support caching)
    NONE = "none"


@dataclass
class CacheConfig:
    """Configuration for cache optimization."""

    # Whether to optimize at all
    enabled: bool = True

    # Strategy to use (auto-detected if None)
    strategy: CacheStrategy | None = None

    # Minimum tokens before caching makes sense
    min_cacheable_tokens: int = 1024

    # Maximum number of breakpoints (Anthropic limit is 4)
    max_breakpoints: int = 4

    # Patterns to extract and move to dynamic section
    date_patterns: list[str] = field(
        default_factory=lambda: [
            r"Today is \w+ \d{1,2},? \d{4}\.?",
            r"Current date: \d{4}-\d{2}-\d{2}",
            r"The current time is .+\.",
        ]
    )

    # Whether to normalize whitespace
    normalize_whitespace: bool = True

    # Collapse multiple blank lines
    collapse_blank_lines: bool = True

    # Separator between static and dynamic content
    dynamic_separator: str = "\n\n---\n\n"

    # Dynamic content detection tiers (for OpenAI prefix stabilization)
    # - "regex": Fast pattern matching (~0ms) - always recommended
    # - "ner": Named Entity Recognition via spaCy (~5-10ms) - catches names, money, etc.
    # - "semantic": Embedding similarity (~20-50ms) - catches volatile patterns
    # Default is regex-only for speed. Add tiers for better detection at cost of latency.
    dynamic_detection_tiers: list[Literal["regex", "ner", "semantic"]] = field(
        default_factory=lambda: ["regex"]
    )

    # For semantic caching
    semantic_cache_enabled: bool = False
    semantic_similarity_threshold: float = 0.95
    semantic_cache_ttl_seconds: int = 300
