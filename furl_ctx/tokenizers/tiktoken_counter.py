"""Tiktoken-based token counter for OpenAI models.

Tiktoken is OpenAI's fast BPE tokenizer used by GPT models.
It supports multiple encodings:
- cl100k_base: GPT-4, GPT-3.5-turbo, text-embedding-ada-002
- o200k_base: GPT-4o, GPT-4o-mini
- p50k_base: Codex models, text-davinci-002/003
- r50k_base: GPT-3 models (davinci, curie, etc.)
"""

from __future__ import annotations

import threading
from collections.abc import Collection
from functools import lru_cache
from typing import Any, Literal, Protocol, cast

from .base import BaseTokenizer


class _Encoding(Protocol):
    """Structural shape of ``tiktoken.Encoding`` actually used here.

    Defined locally instead of importing ``tiktoken.Encoding`` as a type:
    CI's lint job runs mypy without the project's runtime dependencies
    installed, so an unresolved ``tiktoken`` import collapses the real
    class to ``Any`` (even under ``TYPE_CHECKING``) and defeats every
    annotation that would otherwise reference it.
    """

    def encode(
        self, text: str, *, disallowed_special: Literal["all"] | Collection[str] = ...
    ) -> list[int]: ...

    def decode(self, tokens: list[int]) -> str: ...


# Model to encoding mapping
MODEL_TO_ENCODING = {
    # GPT-4o family (o200k_base)
    "gpt-4o": "o200k_base",
    "gpt-4o-mini": "o200k_base",
    "gpt-4o-2024-05-13": "o200k_base",
    "gpt-4o-2024-08-06": "o200k_base",
    "gpt-4o-2024-11-20": "o200k_base",
    "gpt-4o-mini-2024-07-18": "o200k_base",
    # o1 reasoning models (o200k_base)
    "o1": "o200k_base",
    "o1-mini": "o200k_base",
    "o1-preview": "o200k_base",
    "o3-mini": "o200k_base",
    # GPT-4 family (cl100k_base)
    "gpt-4": "cl100k_base",
    "gpt-4-turbo": "cl100k_base",
    "gpt-4-turbo-preview": "cl100k_base",
    "gpt-4-0314": "cl100k_base",
    "gpt-4-0613": "cl100k_base",
    "gpt-4-32k": "cl100k_base",
    "gpt-4-32k-0314": "cl100k_base",
    "gpt-4-32k-0613": "cl100k_base",
    "gpt-4-1106-preview": "cl100k_base",
    "gpt-4-0125-preview": "cl100k_base",
    "gpt-4-turbo-2024-04-09": "cl100k_base",
    # GPT-3.5 family (cl100k_base)
    "gpt-3.5-turbo": "cl100k_base",
    "gpt-3.5-turbo-0301": "cl100k_base",
    "gpt-3.5-turbo-0613": "cl100k_base",
    "gpt-3.5-turbo-1106": "cl100k_base",
    "gpt-3.5-turbo-0125": "cl100k_base",
    "gpt-3.5-turbo-16k": "cl100k_base",
    "gpt-3.5-turbo-16k-0613": "cl100k_base",
    "gpt-3.5-turbo-instruct": "cl100k_base",
    # Embeddings (cl100k_base)
    "text-embedding-ada-002": "cl100k_base",
    "text-embedding-3-small": "cl100k_base",
    "text-embedding-3-large": "cl100k_base",
    # Codex (p50k_base)
    "code-davinci-002": "p50k_base",
    "code-davinci-001": "p50k_base",
    "code-cushman-002": "p50k_base",
    "code-cushman-001": "p50k_base",
    # Legacy GPT-3 (r50k_base)
    "text-davinci-003": "p50k_base",
    "text-davinci-002": "p50k_base",
    "text-davinci-001": "r50k_base",
    "text-curie-001": "r50k_base",
    "text-babbage-001": "r50k_base",
    "text-ada-001": "r50k_base",
    "davinci": "r50k_base",
    "curie": "r50k_base",
    "babbage": "r50k_base",
    "ada": "r50k_base",
}

# Default encoding for unknown models
DEFAULT_ENCODING = "cl100k_base"

# Hard deadline for first-time encoding acquisition (P0-6). tiktoken
# downloads BPE vocab files over the network on the first
# `get_encoding(...)` for an encoding not yet cached on disk; a hung
# download would otherwise hang the caller — and therefore compress() —
# indefinitely. Module-level (not bound as a default argument) so it is
# read at call time and tests can patch it.
ENCODING_LOAD_TIMEOUT_SECONDS = 10.0


@lru_cache(maxsize=8)
def _get_encoding(encoding_name: str) -> _Encoding:
    """Get tiktoken encoding, cached for performance."""
    import tiktoken

    # tiktoken ships no stubs mypy can see in every environment this runs in
    # (CI's lint job installs mypy but not the project's dependencies), so
    # `tiktoken.get_encoding` is Any there; this is the one place that
    # boundary is asserted, everything downstream is typed via _Encoding.
    return cast(_Encoding, tiktoken.get_encoding(encoding_name))


def _load_encoding_bounded(encoding_name: str, timeout: float | None = None) -> _Encoding:
    """Load a tiktoken encoding with a hard deadline (thread + join).

    Thread-based rather than signal-based because compress() may run off
    the main thread (signal handlers only fire on the main thread). The
    worker is a daemon so a stuck download never blocks interpreter
    exit; if it eventually completes anyway, the `_get_encoding`
    lru_cache makes the next retry instant.

    Args:
        encoding_name: tiktoken encoding name (e.g. 'o200k_base').
        timeout: Deadline in seconds; defaults to
            ``ENCODING_LOAD_TIMEOUT_SECONDS`` (read at call time).

    Returns:
        The loaded tiktoken encoding.

    Raises:
        TimeoutError: The load did not finish within the deadline.
        Exception: Whatever ``tiktoken.get_encoding`` raised, re-raised
            on the calling thread (e.g. ``ValueError: Unknown encoding``
            from a tiktoken build older than the encoding).
    """
    deadline = ENCODING_LOAD_TIMEOUT_SECONDS if timeout is None else timeout
    encoding: _Encoding | None = None
    error: Exception | None = None

    def _worker() -> None:
        nonlocal encoding, error
        try:
            encoding = _get_encoding(encoding_name)
        except Exception as exc:  # re-raised on the calling thread below
            error = exc

    thread = threading.Thread(
        target=_worker,
        name=f"tiktoken-load-{encoding_name}",
        daemon=True,
    )
    thread.start()
    thread.join(deadline)
    if thread.is_alive():
        # Leave the worker running (daemon): if the download eventually
        # succeeds it populates the lru_cache and the next retry is free.
        raise TimeoutError(
            f"tiktoken encoding {encoding_name!r} did not load within {deadline}s "
            "(stalled BPE vocab download?)"
        )
    if error is not None:
        raise error
    if encoding is None:  # totality net: worker died without a result
        raise RuntimeError(f"tiktoken encoding {encoding_name!r} load produced no result")
    return encoding


def get_encoding_for_model(model: str) -> str:
    """Get the tiktoken encoding name for a model.

    Args:
        model: Model name (e.g., 'gpt-4o', 'gpt-3.5-turbo').

    Returns:
        Encoding name (e.g., 'o200k_base', 'cl100k_base').
    """
    # Direct lookup
    if model in MODEL_TO_ENCODING:
        return MODEL_TO_ENCODING[model]

    # Try prefix matching for versioned models. Ordered most-specific first
    # so that, e.g., "gpt-4o-*" resolves before "gpt-4-*". Each prefix maps
    # directly to its encoding: scanning MODEL_TO_ENCODING for the first key
    # that merely starts with the prefix is order-dependent and wrong — the
    # "gpt-4" prefix would match the "gpt-4o" dict entry first and return
    # o200k_base instead of cl100k_base for unknown gpt-4 snapshots.
    for prefix, encoding in (
        ("gpt-4o", "o200k_base"),
        ("gpt-4-turbo", "cl100k_base"),
        ("gpt-4", "cl100k_base"),
        ("gpt-3.5", "cl100k_base"),
        ("o1", "o200k_base"),
        ("o3", "o200k_base"),
        # Claude models: o200k_base is the closest public encoding (Q1).
        # Anthropic's tokenizer is not publicly available; o200k_base is far
        # more accurate than the old 3.5-chars/token flat estimate.
        ("claude-", "o200k_base"),
    ):
        if model.startswith(prefix):
            return encoding

    return DEFAULT_ENCODING


# Refuse to tokenize input carrying an unbroken same-class run at least this long.
# tiktoken's split regex has UNBOUNDED quantifiers for letters (``\p{L}+``),
# whitespace (``\s+``) and punctuation/symbols (``[^\s\p{L}\p{N}]+``); a very long
# run of any ONE of those classes drives fancy-regex into catastrophic
# backtracking. That failure is PLATFORM-DIVERGENT: small-stack platforms (macOS)
# raise "Max stack size exceeded for backtracking", while larger-stack ones
# (CI Linux) merely burn super-linear time and then hand the content back
# un-tokenized — so the same 2 MB single-line input fails LOUD on one platform and
# declines SILENTLY on the other. Rejecting the run here (before the encode) makes
# the decline LOUD and DETERMINISTIC everywhere: the ValueError rides compress()'s
# fail-open boundary into result.error, byte-exact original preserved. The limit
# sits far above any legitimate same-class token/word/run (real ones are a handful
# of characters) and caps the measured divergence regime: a sub-limit run costs
# ~7.5 s worst-case at ~130k and grows super-linearly beyond (~18.7 s @ 200k,
# ~46.5 s @ 300k; the hard stack raise only lands near ~2 M). Being >= the
# memoization floor ``_COUNT_CACHE_MIN_LEN`` (65_536) also guarantees the
# un-memoized fast path (shorter text) can never smuggle a longer run past this
# gate.
_MAX_SAFE_SAME_CLASS_RUN = 131_072


def _has_backtracking_prone_run(text: str, limit: int) -> bool:
    """True iff *text* holds a run of at least *limit* consecutive characters in
    one backtracking-prone tokenizer class — letters, whitespace, or
    punctuation/symbols.

    Digit runs are EXEMPT (o200k caps them at ``\\p{N}{1,3}``, so they never
    backtrack) and any class transition breaks the run — so high-entropy base64
    and other mixed content passes (its runs stay short), while a DEGENERATE
    single-class run is refused loud regardless of provenance: low-entropy
    base64 (e.g. the all-'A' encoding of zero-filled data) at >= *limit* IS
    flagged. Pure ``O(n)`` scan with an early exit at the first offending run,
    so genuinely pathological input costs only ``O(limit)``.
    """
    if len(text) < limit:
        return False
    run = 0
    current = ""
    for ch in text:
        if ch.isdigit():
            current = "d"
            run = 0
            continue
        cls = "a" if ch.isalpha() else ("s" if ch.isspace() else "o")
        if cls == current:
            run += 1
            if run >= limit:
                return True
        else:
            current = cls
            run = 1
    return False


class TiktokenCounter(BaseTokenizer):
    """Token counter using tiktoken (OpenAI's tokenizer).

    This is the most accurate tokenizer for OpenAI models and provides
    a good approximation for many other models that use similar BPE
    tokenization.

    Example:
        counter = TiktokenCounter("gpt-4o")
        tokens = counter.count_text("Hello, world!")
        print(f"Token count: {tokens}")
    """

    # OpenAI-specific message overhead
    MESSAGE_OVERHEAD = 3
    REPLY_OVERHEAD = 3

    # count_text memoization: the router / min_ratio gate re-count the SAME
    # multi-MB content many times per compress() (measured ~18x on a 33 MB
    # Chrome trace — tiktoken is O(n), so that dominated latency). Cache only
    # large strings (small ones are ~unique, not worth the memory) and bound the
    # cache to a few entries. The memo is a pure function of the text for a fixed
    # encoding, so a cache hit is always the exact count — accounting is unchanged.
    _COUNT_CACHE_MIN_LEN = 65_536
    _COUNT_CACHE_MAX_BYTES = 64 * 1024 * 1024

    def __init__(self, model: str = "gpt-4o", *, proxy_for: str | None = None):
        """Initialize tiktoken counter.

        The encoding is resolved EAGERLY here — with a hard deadline
        (see `_load_encoding_bounded`) — so a load failure or a stalled
        vocab download surfaces at construction, where
        ``TokenizerRegistry.get()`` degrades the call to the estimation
        fallback WITHOUT caching the failure (COR-40c: the next
        ``get()`` retries the real tokenizer). Lazy loading deferred
        failures to the first ``count_text`` — after the registry had
        already cached this instance as a success — and put an
        unbounded network wait on compress()'s hot path.

        Args:
            model: Model name to determine encoding.
                   Defaults to 'gpt-4o' (o200k_base encoding).
            proxy_for: Set by the registry (T10) when this counter's
                encoding is standing in for a vendor with no public
                tokenizer — e.g. ``"anthropic"`` for claude-* models
                routed through o200k_base, the same encoding as gpt-4o.
                ``None`` (the default) means this counter is counting
                real tokens for its own vendor, e.g. an actual gpt-4o
                call. Exposed as ``self.proxy_for`` and included in
                ``__repr__`` so the approximation is visible on the
                object itself, not just in a docstring or a log line —
                see ``furl_ctx.tokenizers.registry.ANTHROPIC_O200K_PROXY_NOTE``
                for the documented error band.

        Raises:
            TimeoutError: Encoding did not load within
                ``ENCODING_LOAD_TIMEOUT_SECONDS``.
            ValueError: tiktoken does not know the encoding (e.g. a
                tiktoken build older than the model's encoding).
        """
        self.model = model
        self.proxy_for = proxy_for
        # Lowercase once: encoding lookup is case-sensitive, and callers pass
        # names like "GPT-4o" which must not fall through to the default.
        self.encoding_name = get_encoding_for_model(model.lower())
        self._encoding: _Encoding | None = _load_encoding_bounded(self.encoding_name)
        # Bounded memo, capped by total cached BYTES (oldest evicted first). A
        # plain clear-on-overflow thrashed at scale — the hottest string (a
        # 33 MB doc re-counted many times) got evicted and re-encoded. Bounding
        # by bytes keeps the working set resident, so each string is encoded once.
        self._count_cache: dict[str, int] = {}
        self._count_cache_bytes = 0

    @property
    def encoding(self) -> _Encoding:
        """The tiktoken encoding (loaded at construction).

        The bounded reload below only serves legacy paths that bypass
        ``__init__`` or cleared the cached encoding.
        """
        if self._encoding is None:
            self._encoding = _load_encoding_bounded(self.encoding_name)
        return self._encoding

    def _encode_tolerant(self, text: str) -> list[int]:
        """Encode, treating literal special-token strings as ordinary text.

        tiktoken's default ``encode`` raises ValueError when the text
        contains a literal special-token string (``<|endoftext|>`` and
        friends — common in scraped / LLM-adjacent tool output). Retry
        with ``disallowed_special=()`` so the literal is tokenized as
        plain text: when counting someone else's content the string is
        data, not a control token (P0-6).
        """
        try:
            return self.encoding.encode(text)
        except ValueError:
            return self.encoding.encode(text, disallowed_special=())

    def count_text(self, text: str) -> int:
        """Count tokens in text using tiktoken.

        Large strings are memoized (see ``_COUNT_CACHE_MIN_LEN``): the router and
        the ``min_ratio`` gate re-count the same multi-MB content many times per
        compress(), and tiktoken is O(n). A cache hit returns the exact count (a
        pure function of the text for this encoding), so accounting is unchanged —
        only the number of encode passes drops.

        Args:
            text: Text to tokenize.

        Returns:
            Number of tokens.
        """
        if not text:
            return 0
        if len(text) < self._COUNT_CACHE_MIN_LEN:
            return len(self._encode_tolerant(text))
        cached = self._count_cache.get(text)
        if cached is not None:
            return cached
        # Refuse backtracking-prone input BEFORE the (super-linear / stack-blowing)
        # encode, so the decline is loud and identical on every platform rather
        # than a raise-here / silent-passthrough-there split (see
        # _MAX_SAFE_SAME_CLASS_RUN). The raise rides compress()'s fail-open path
        # into result.error with the original content returned byte-exact.
        if _has_backtracking_prone_run(text, _MAX_SAFE_SAME_CLASS_RUN):
            raise ValueError(
                "tokenizer input has an unbroken same-class run of at least "
                f"{_MAX_SAFE_SAME_CLASS_RUN} characters, which triggers catastrophic "
                "regex backtracking in the tokenizer (a platform-dependent stack "
                "overflow); declining compression and returning the original unchanged."
            )
        n = len(self._encode_tolerant(text))
        self._count_cache[text] = n
        self._count_cache_bytes += len(text)
        while self._count_cache_bytes > self._COUNT_CACHE_MAX_BYTES and len(self._count_cache) > 1:
            oldest = next(iter(self._count_cache))
            self._count_cache_bytes -= len(oldest)
            del self._count_cache[oldest]
        return n

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        """Count tokens in messages using OpenAI's exact formula.

        This matches OpenAI's token counting for chat completions.

        Args:
            messages: List of chat messages.

        Returns:
            Total token count.
        """
        total = 0

        for message in messages:
            # Every message has overhead for role and formatting
            total += self.MESSAGE_OVERHEAD

            for key, value in message.items():
                if value is None:
                    continue

                if key == "content":
                    if isinstance(value, str):
                        total += self.count_text(value)
                    elif isinstance(value, list):
                        # Multi-part content
                        for part in value:
                            if isinstance(part, dict):
                                if part.get("type") == "text":
                                    total += self.count_text(part.get("text", ""))
                                elif part.get("type") == "image_url":
                                    # Image tokens vary by detail level
                                    detail = part.get("image_url", {}).get("detail", "auto")
                                    if detail == "low":
                                        total += 85
                                    else:
                                        total += 170  # Base for high detail
                                else:
                                    # Delegate all other part types (Anthropic
                                    # image/tool_result blocks, Strands SDK parts,
                                    # audio, documents) to the shared handler.
                                    # Stringifying them here would count base64
                                    # payloads as text — a 200KB image part became
                                    # ~100K tokens instead of the fixed image cost.
                                    total += self._count_content_parts([part])
                            elif isinstance(part, str):
                                total += self.count_text(part)
                elif key == "role":
                    total += self.count_text(value)
                elif key == "name":
                    total += self.count_text(value)
                    total += 1  # Name adds 1 token
                elif key == "tool_calls":
                    for tool_call in value:
                        total += 3  # Tool call overhead
                        if "function" in tool_call:
                            func = tool_call["function"]
                            total += self.count_text(func.get("name", ""))
                            total += self.count_text(func.get("arguments", ""))
                        if "id" in tool_call:
                            total += self.count_text(tool_call["id"])
                elif key == "tool_call_id":
                    total += self.count_text(value)
                elif key == "function_call":
                    total += self.count_text(value.get("name", ""))
                    total += self.count_text(value.get("arguments", ""))

        # Every reply is primed with assistant
        total += self.REPLY_OVERHEAD

        return total

    def encode(self, text: str) -> list[int]:
        """Encode text to token IDs.

        Literal special-token strings in ``text`` are tokenized as
        ordinary text rather than raising (see `_encode_tolerant`).

        Args:
            text: Text to encode.

        Returns:
            List of token IDs.
        """
        return self._encode_tolerant(text)

    def decode(self, tokens: list[int]) -> str:
        """Decode token IDs to text.

        Args:
            tokens: List of token IDs.

        Returns:
            Decoded text.
        """
        return self.encoding.decode(tokens)

    def __repr__(self) -> str:
        if self.proxy_for is not None:
            return (
                f"TiktokenCounter(model={self.model!r}, encoding={self.encoding_name!r}, "
                f"proxy_for={self.proxy_for!r})"
            )
        return f"TiktokenCounter(model={self.model!r}, encoding={self.encoding_name!r})"
