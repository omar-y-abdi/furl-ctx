"""Cross-message deduplication of repeated tool outputs.

The per-content compressors (ContentRouter and everything below it) operate
WITHIN one message; redundancy ACROSS the message list — the same tool
output repeated across turns (an agent re-running ``rg``/``git``/``df`` to
confirm state) — was never deduplicated, so every repetition paid full
price. This transform removes that waste at the conversation level:

* The FIRST occurrence of a tool output is NEVER modified.
* A later byte-identical occurrence is replaced by a small recoverable
  sentinel: the original is persisted in the CCR ``compression_store``
  FIRST, and only then is the content swapped for a
  ``{"_ccr_dropped": "... <<ccr:HASH ...>>"}`` pointer naming the message
  that still carries the bytes. If the store write fails, the content is
  left untouched — recoverability is a precondition of removal, never an
  afterthought.

Prompt-cache safety contract (P0 — pinned by tests/test_cross_message_dedup.py):

* Message COUNT, ORDER and roles never change. Replacement happens within
  message content only.
* The message at index 0 is never modified — not even duplicate blocks
  inside it.
* Messages inside the frozen prefix (``frozen_message_count``) are never
  modified.
* Any content block carrying ``cache_control`` is passed through
  byte-faithful — it is the client's explicit cache breakpoint.
* Only LATER occurrences are rewritten. Earlier messages (the ones a
  provider prefix cache could already hold) stay byte-identical, so dedup
  only ever changes the suffix of the conversation.

Eligibility is deliberately narrow: OpenAI-style ``role in {tool, function}``
messages with string content, and Anthropic-style ``tool_result`` blocks
with string content. User / system / developer prompts and assistant text
(echoed into provider auto-prefix caches) are never touched. Error-flagged
tool results (``is_error``) are never touched.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from ..config import TransformResult
from ..tokenizer import Tokenizer
from .base import Transform

logger = logging.getLogger(__name__)

# Minimum content size (chars) for dedup to be worth a pointer. The sentinel
# itself is ~190 chars; below this the replacement saves nothing.
MIN_DEDUP_CHARS = 256

# Roles whose string content is a tool output (OpenAI-style).
_TOOL_ROLES = frozenset({"tool", "function"})

# Hash length for the surfaced marker / store key. Matches the Python
# compression_store default (SHA-256[:24]) so the marker hash and the store
# key are the same string by construction.
_MARKER_HASH_LEN = 24

_SENTINEL_KEY = "_ccr_dropped"


@dataclass(frozen=True)
class _FirstOccurrence:
    """Where a distinct tool-output payload first appeared."""

    message_index: int
    content: str


def _content_hash(content: str) -> str:
    """SHA-256[:24] of the exact content bytes (the store's own default)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:_MARKER_HASH_LEN]


def duplicate_sentinel(ccr_hash: str, n_bytes: int, first_message_index: int) -> str:
    """Render the recoverable replacement for an elided duplicate.

    A JSON OBJECT string (not an array) so downstream compressors route it
    to the no-op object path and the pointer can never be re-compressed
    away. Carries the ``_ccr_dropped`` sentinel key (the engine's drop
    grammar) and a ``<<ccr:HASH ...>>`` pointer resolvable in the CCR store.
    """
    marker = f"<<ccr:{ccr_hash} {n_bytes}_bytes_duplicate>>"
    note = (
        "duplicate tool output elided - byte-identical to the tool output in "
        f"message {first_message_index} of this conversation; full original: {marker}"
    )
    return json.dumps({_SENTINEL_KEY: note}, ensure_ascii=False)


class CrossMessageDeduper(Transform):
    """Replace later byte-identical tool outputs with recoverable pointers."""

    name = "cross_message_dedup"

    def should_apply(
        self,
        messages: list[dict[str, Any]],
        tokenizer: Tokenizer,
        **kwargs: Any,
    ) -> bool:
        """Dedup needs at least two messages to have a cross-message pair."""
        return len(messages) >= 2

    def apply(
        self,
        messages: list[dict[str, Any]],
        tokenizer: Tokenizer,
        **kwargs: Any,
    ) -> TransformResult:
        """Scan messages in order; elide later exact duplicates of tool outputs.

        Never mutates the input list or its messages — replaced messages are
        new dicts, untouched messages are passed through by reference.
        """
        frozen_count = int(kwargs.get("frozen_message_count", 0) or 0)
        query_context = str(kwargs.get("context", "") or "")

        seen: dict[str, _FirstOccurrence] = {}
        new_messages: list[dict[str, Any]] = []
        replaced_count = 0

        for index, message in enumerate(messages):
            replaceable = index > 0 and index >= frozen_count
            new_message, n_replaced = self._process_message(
                message,
                index=index,
                replaceable=replaceable,
                seen=seen,
                query_context=query_context,
            )
            replaced_count += n_replaced
            new_messages.append(new_message)

        if replaced_count == 0:
            tokens = tokenizer.count_messages(messages)
            return TransformResult(
                messages=messages,
                tokens_before=tokens,
                tokens_after=tokens,
                transforms_applied=[],
            )

        tokens_before = tokenizer.count_messages(messages)
        tokens_after = tokenizer.count_messages(new_messages)
        return TransformResult(
            messages=new_messages,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            transforms_applied=[f"cross_message_dedup:exact:{replaced_count}"],
        )

    # ------------------------------------------------------------------ #
    # Per-message handling.
    # ------------------------------------------------------------------ #

    def _process_message(
        self,
        message: dict[str, Any],
        *,
        index: int,
        replaceable: bool,
        seen: dict[str, _FirstOccurrence],
        query_context: str,
    ) -> tuple[dict[str, Any], int]:
        """Handle one message; returns ``(possibly-new message, n replaced)``."""
        role = message.get("role", "")
        content = message.get("content", "")

        # OpenAI-style tool message with string content.
        if role in _TOOL_ROLES and isinstance(content, str):
            if message.get("is_error"):
                return message, 0
            new_content = self._dedup_unit(
                content,
                message_index=index,
                replaceable=replaceable,
                seen=seen,
                query_context=query_context,
                tool_name=str(message.get("name", "") or "") or None,
            )
            if new_content is None:
                return message, 0
            return {**message, "content": new_content}, 1

        # Anthropic-style content blocks: dedup tool_result blocks only.
        if isinstance(content, list):
            return self._process_blocks(
                message,
                content,
                index=index,
                replaceable=replaceable,
                seen=seen,
                query_context=query_context,
            )

        return message, 0

    def _process_blocks(
        self,
        message: dict[str, Any],
        blocks: list[Any],
        *,
        index: int,
        replaceable: bool,
        seen: dict[str, _FirstOccurrence],
        query_context: str,
    ) -> tuple[dict[str, Any], int]:
        """Dedup ``tool_result`` blocks inside a content-block message."""
        new_blocks: list[Any] = []
        replaced = 0
        for block in blocks:
            if (
                not isinstance(block, dict)
                or block.get("type") != "tool_result"
                or "cache_control" in block
                or block.get("is_error") is True
                or not isinstance(block.get("content"), str)
            ):
                new_blocks.append(block)
                continue
            new_content = self._dedup_unit(
                block["content"],
                message_index=index,
                replaceable=replaceable,
                seen=seen,
                query_context=query_context,
                tool_name=None,
            )
            if new_content is None:
                new_blocks.append(block)
                continue
            new_blocks.append({**block, "content": new_content})
            replaced += 1
        if replaced == 0:
            return message, 0
        return {**message, "content": new_blocks}, replaced

    # ------------------------------------------------------------------ #
    # The dedup unit: register first occurrence / replace later ones.
    # ------------------------------------------------------------------ #

    def _dedup_unit(
        self,
        content: str,
        *,
        message_index: int,
        replaceable: bool,
        seen: dict[str, _FirstOccurrence],
        query_context: str,
        tool_name: str | None,
    ) -> str | None:
        """Returns the replacement string, or ``None`` to keep the unit as-is.

        The first occurrence of a payload is always registered and never
        replaced. A later occurrence is replaced only when the message is
        replaceable (not index 0, not frozen) AND the original was persisted
        to the CCR store successfully — store-before-replace.
        """
        if len(content) < MIN_DEDUP_CHARS:
            return None

        ccr_hash = _content_hash(content)
        first = seen.get(ccr_hash)
        if first is None or first.content != content:
            # Unseen payload (or a 96-bit hash collision — keep both
            # verbatim rather than emit a wrong pointer).
            if first is None:
                seen[ccr_hash] = _FirstOccurrence(
                    message_index=message_index, content=content
                )
            return None

        if not replaceable:
            return None

        sentinel = duplicate_sentinel(
            ccr_hash, len(content.encode("utf-8")), first.message_index
        )
        if not self._persist_original(
            content,
            sentinel,
            ccr_hash=ccr_hash,
            query_context=query_context,
            tool_name=tool_name,
        ):
            return None
        return sentinel

    @staticmethod
    def _persist_original(
        content: str,
        sentinel: str,
        *,
        ccr_hash: str,
        query_context: str,
        tool_name: str | None,
    ) -> bool:
        """Persist the original under the marker hash. True on success.

        Recoverability invariant: the store write must succeed BEFORE any
        replacement ships. On any failure the caller keeps the original
        bytes in place (no silent loss, no dangling pointer).
        """
        try:
            from ..cache.compression_store import get_compression_store

            store = get_compression_store()
            store.store(
                original=content,
                compressed=sentinel,
                original_tokens=max(1, len(content) // 4),
                compressed_tokens=max(1, len(sentinel) // 4),
                tool_name=tool_name,
                query_context=query_context or None,
                compression_strategy="cross_message_dedup",
                explicit_hash=ccr_hash,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - any store failure must veto
            logger.warning(
                "cross_message_dedup: CCR persist failed (%s); keeping duplicate "
                "content in place (no replacement without recoverability)",
                exc,
            )
            return False
