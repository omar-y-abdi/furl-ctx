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
from functools import lru_cache
from typing import Any

from .base import BaseTokenizer

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
def _get_encoding(encoding_name: str):
    """Get tiktoken encoding, cached for performance."""
    import tiktoken

    return tiktoken.get_encoding(encoding_name)


def _load_encoding_bounded(encoding_name: str, timeout: float | None = None):
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
    outcome: dict[str, Any] = {}

    def _worker() -> None:
        try:
            outcome["encoding"] = _get_encoding(encoding_name)
        except Exception as exc:  # re-raised on the calling thread below
            outcome["error"] = exc

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
    if "error" in outcome:
        raise outcome["error"]
    encoding = outcome.get("encoding")
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

    def __init__(self, model: str = "gpt-4o"):
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

        Raises:
            TimeoutError: Encoding did not load within
                ``ENCODING_LOAD_TIMEOUT_SECONDS``.
            ValueError: tiktoken does not know the encoding (e.g. a
                tiktoken build older than the model's encoding).
        """
        self.model = model
        # Lowercase once: encoding lookup is case-sensitive, and callers pass
        # names like "GPT-4o" which must not fall through to the default.
        self.encoding_name = get_encoding_for_model(model.lower())
        self._encoding = _load_encoding_bounded(self.encoding_name)

    @property
    def encoding(self):
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

        Args:
            text: Text to tokenize.

        Returns:
            Number of tokens.
        """
        if not text:
            return 0
        return len(self._encode_tolerant(text))

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
        return f"TiktokenCounter(model={self.model!r}, encoding={self.encoding_name!r})"
