"""Estimation-based token counter for fallback scenarios.

When no exact tokenizer is available (e.g., unknown models, missing
dependencies), this provides a reasonable approximation based on
character/word heuristics calibrated against real tokenizers.
"""

from __future__ import annotations

import json
import re
from contextlib import suppress

from .base import BaseTokenizer

# Ratio detection and special-pattern overhead scanning operate on a bounded PREFIX SAMPLE of the text.
_DETECTION_SAMPLE_CHARS = 4096

# JSON-ness heuristic for texts LARGER than the sample (a truncated prefix never json.loads-parses): after the
# ``[``/``{`` head check, classify as JSON when the sample's structural-character density clears this floor.
_JSON_STRUCTURAL_CHARS = frozenset(',:"{}[]')
_JSON_STRUCTURAL_DENSITY = 1 / 16


class EstimatingTokenCounter(BaseTokenizer):
    """Token counter using estimation heuristics.

    This is the fallback tokenizer used when:
    - Model is unknown/unsupported
    - Required tokenizer library not installed
    - Speed is prioritized over accuracy

    The estimation is calibrated against tiktoken cl100k_base and
    provides ~90% accuracy for typical text. It tends to slightly
    overestimate, which is safer for context window management.

    Estimation Strategy:
    - Base: ~4 characters per token (calibrated against GPT-4)
    - Adjustments for code, URLs, numbers, whitespace
    - Special handling for JSON structure

    Example:
        counter = EstimatingTokenCounter()
        tokens = counter.count_text("Hello, world!")
        print(f"Estimated tokens: {tokens}")
    """

    # Calibration constants (derived from tiktoken analysis)
    CHARS_PER_TOKEN = 4.0  # Average for English text
    CHARS_PER_TOKEN_CODE = 3.5  # Code is denser
    CHARS_PER_TOKEN_JSON = 3.2  # JSON has more structure

    # Patterns for content type detection
    CODE_PATTERN = re.compile(
        r"(?:def |class |function |const |let |var |import |from |"
        r"if \(|for \(|while \(|switch \(|try \{|catch \(|"
        r"=>|->|\{\{|\}\}|;$)",
        re.MULTILINE,
    )
    JSON_PATTERN = re.compile(r"^\s*[\[\{]")
    URL_PATTERN = re.compile(r"https?://\S+")
    UUID_PATTERN = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
    )

    def __init__(self, chars_per_token: float | None = None, *, proxy_for: str | None = None):
        """Initialize estimating counter.

        Args:
            chars_per_token: Override default chars per token ratio.
                            If None, auto-detects based on content type.
            proxy_for: Set by the registry (T10) when this fixed-ratio
                estimate is standing in for a vendor with no accessible
                tokenizer — e.g. ``"fixed_ratio_estimate"`` for Gemini/
                Cohere models, which get a flat 4.0 chars-per-token
                estimate instead of a real BPE tokenizer. ``None`` (the
                default) means generic/auto estimation with no specific
                vendor claim attached. Exposed as ``self.proxy_for`` and
                included in ``__repr__`` — see
                ``furl_ctx.tokenizers.registry.FIXED_RATIO_ESTIMATOR_NOTE``
                for the documented error band (can be roughly 2x off,
                worse for CJK/non-Latin or densely structured content,
                closer for plain English prose/code).
        """
        self._fixed_ratio = chars_per_token
        self.proxy_for = proxy_for

    def count_text(self, text: str) -> int:
        """Estimate token count for text.

        Args:
            text: Text to count tokens for.

        Returns:
            Estimated number of tokens.
        """
        if not text:
            return 0

        # Use fixed ratio if provided
        if self._fixed_ratio is not None:
            return max(1, int(len(text) / self._fixed_ratio + 0.5))

        # Auto-detect content type and adjust ratio
        ratio = self._detect_ratio(text)

        # Apply ratio with minimum of 1 token
        base_count = int(len(text) / ratio + 0.5)

        # Add overhead for special patterns
        overhead = self._count_special_overhead(text)

        return max(1, base_count + overhead)

    def _detect_ratio(self, text: str) -> float:
        """Detect optimal chars-per-token ratio based on content.

        Detection runs on a ``_DETECTION_SAMPLE_CHARS`` prefix sample.
        Texts at or under the sample size keep the exact historical
        behavior (the sample IS the text); larger JSON candidates classify
        via a structural-density heuristic on the prefix, since a truncated
        prefix never parses.

        Args:
            text: Text to analyze.

        Returns:
            Chars per token ratio.
        """
        sample = text[:_DETECTION_SAMPLE_CHARS]

        # Check for JSON
        if self.JSON_PATTERN.match(sample):
            if len(text) <= _DETECTION_SAMPLE_CHARS:
                with suppress(json.JSONDecodeError, ValueError):
                    json.loads(text)
                    return self.CHARS_PER_TOKEN_JSON
            elif self._sample_is_json_like(sample):
                return self.CHARS_PER_TOKEN_JSON

        # Check for code — match density over the sampled window.
        code_matches = len(self.CODE_PATTERN.findall(sample))
        if code_matches > len(sample) / 500:  # ~2 matches per KB
            return self.CHARS_PER_TOKEN_CODE

        return self.CHARS_PER_TOKEN

    @staticmethod
    def _sample_is_json_like(sample: str) -> bool:
        """Structural-density JSON check for a prefix sample.

        Used only when the full text exceeds the sample window, so
        ``json.loads`` on the (truncated) prefix cannot decide. Counts the
        JSON structural characters and compares their density against
        ``_JSON_STRUCTURAL_DENSITY``.
        """
        if not sample:
            return False
        structural = sum(1 for ch in sample if ch in _JSON_STRUCTURAL_CHARS)
        return structural >= len(sample) * _JSON_STRUCTURAL_DENSITY

    def _count_special_overhead(self, text: str) -> int:
        """Count additional tokens for special patterns.

        URLs and UUIDs often tokenize into more tokens than
        character count would suggest. The scan is bounded to the same
        ``_DETECTION_SAMPLE_CHARS`` prefix sample as ratio detection. The overhead is a
        small correction term on top of the length-based estimate; for
        texts at or under the sample size the behavior is exactly the
        historical one.

        Args:
            text: Text to analyze.

        Returns:
            Additional token overhead.
        """
        sample = text[:_DETECTION_SAMPLE_CHARS]

        # URLs typically tokenize to more tokens — each component adds overhead
        overhead: int = sum(
            url.count("/") + url.count("?") + url.count("&")
            for url in self.URL_PATTERN.findall(sample)
        )

        # UUIDs are typically 8-10 tokens despite being 36 chars (~2 extra each)
        overhead += len(self.UUID_PATTERN.findall(sample)) * 2

        return overhead

    def __repr__(self) -> str:
        if self.proxy_for is not None:
            return (
                f"EstimatingTokenCounter(chars_per_token={self._fixed_ratio}, "
                f"proxy_for={self.proxy_for!r})"
            )
        if self._fixed_ratio:
            return f"EstimatingTokenCounter(chars_per_token={self._fixed_ratio})"
        return "EstimatingTokenCounter(auto)"
