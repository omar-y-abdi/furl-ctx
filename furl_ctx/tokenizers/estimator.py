"""Estimation-based token counter for fallback scenarios.

When no exact tokenizer is available (e.g., unknown models, missing
dependencies), this provides a reasonable approximation based on
character/word heuristics calibrated against real tokenizers.
"""

from __future__ import annotations

import json
import re

from .base import BaseTokenizer

# PERF-14: ratio detection and special-pattern overhead scanning operate on
# a bounded PREFIX SAMPLE of the text. Auto-mode previously json.loads-parsed
# multi-MB strings and regex-scanned the full text on EVERY count_text call
# just to pick 3.2 vs 4.0 chars/token — on large tool outputs the "cheap
# estimator" cost more than the content it was approximating. Texts at or
# under the sample size keep the exact historical behavior.
_DETECTION_SAMPLE_CHARS = 4096

# JSON-ness heuristic for texts LARGER than the sample (a truncated prefix
# never json.loads-parses): after the ``[``/``{`` head check, classify as
# JSON when the sample's structural-character density clears this floor.
# Object arrays run >20% structural chars, bare numeric arrays ~8%, English
# prose and bracketed log lines ("[INFO] ...") 2-3% — 1/16 (6.25%) splits
# the classes cleanly.
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

    def __init__(self, chars_per_token: float | None = None):
        """Initialize estimating counter.

        Args:
            chars_per_token: Override default chars per token ratio.
                            If None, auto-detects based on content type.
        """
        self._fixed_ratio = chars_per_token

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

        Detection runs on a ``_DETECTION_SAMPLE_CHARS`` prefix sample
        (PERF-14): a multi-MB tool output was previously fully
        ``json.loads``-parsed and regex-scanned per call just to pick the
        ratio. Texts at or under the sample size keep the exact historical
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
                try:
                    json.loads(text)
                    return self.CHARS_PER_TOKEN_JSON
                except (json.JSONDecodeError, ValueError):
                    pass
            elif self._sample_is_json_like(sample):
                return self.CHARS_PER_TOKEN_JSON

        # Check for code — match density over the sampled window.
        code_matches = len(self.CODE_PATTERN.findall(sample))
        if code_matches > len(sample) / 500:  # ~2 matches per KB
            return self.CHARS_PER_TOKEN_CODE

        return self.CHARS_PER_TOKEN

    @staticmethod
    def _sample_is_json_like(sample: str) -> bool:
        """Structural-density JSON check for a prefix sample (PERF-14).

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
        ``_DETECTION_SAMPLE_CHARS`` prefix sample as ratio detection
        (PERF-14) — two full-text regex passes per count_text call were
        the other half of the multi-MB auto-mode cost. The overhead is a
        small correction term on top of the length-based estimate; for
        texts at or under the sample size the behavior is exactly the
        historical one.

        Args:
            text: Text to analyze.

        Returns:
            Additional token overhead.
        """
        sample = text[:_DETECTION_SAMPLE_CHARS]
        overhead = 0

        # URLs typically tokenize to more tokens
        urls = self.URL_PATTERN.findall(sample)
        for url in urls:
            # Each URL component adds overhead
            overhead += url.count("/") + url.count("?") + url.count("&")

        # UUIDs are typically 8-10 tokens despite being 36 chars
        uuids = self.UUID_PATTERN.findall(sample)
        overhead += len(uuids) * 2  # Each UUID adds ~2 extra tokens

        return overhead

    def __repr__(self) -> str:
        if self._fixed_ratio:
            return f"EstimatingTokenCounter(chars_per_token={self._fixed_ratio})"
        return "EstimatingTokenCounter(auto)"
