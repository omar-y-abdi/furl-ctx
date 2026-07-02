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

Two tiers, both pointer-backed:

* **Exact** — a later occurrence byte-identical to an earlier one is fully
  replaced by the sentinel.
* **Near** — a later JSON dict-array sharing a meaningful set of
  byte-identical rows with an earlier kept-verbatim array ships only its
  DIFFERING rows; the shared rows are elided with a sentinel naming the
  message that still shows them, and the full original stays recoverable
  under the surfaced hash. Reference sources are always kept-verbatim
  units, so every elided row remains visible somewhere in the
  conversation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import TransformResult
from ..tokenizer import Tokenizer
from .base import Transform

logger = logging.getLogger(__name__)

# Minimum content size (chars) for dedup to be worth a pointer. The sentinel
# itself is ~190 chars; below this the replacement saves nothing.
MIN_DEDUP_CHARS = 256

# Near-duplicate gates: a later JSON dict-array is rewritten only when the
# byte-identical row overlap with an earlier kept-verbatim array is
# meaningful (count, absolute bytes, fraction of the payload) AND the
# rewrite beats the COUNTERFACTUAL, not just the raw original: a unit left
# untouched still gets per-message lossless compression downstream (the
# CSV-schema table typically lands near ~50% on row-shaped data — measured
# 50-56% on the real disk/search benchmarks). The near rendering is pinned
# against further compression, so it must cost LESS than that rendering
# would — i.e. come in under ~45% of the original bytes. Measured the hard
# way: without this gate the real drifted `df -k` pair (5/9 rows changed)
# regressed 347 -> ~430 tokens because 5 raw JSON rows + sentinel outweigh
# the 9-row lossless table.
NEAR_DUP_MIN_SHARED_ROWS = 2
NEAR_DUP_MIN_SHARED_BYTES = 256
NEAR_DUP_MIN_SHARED_FRACTION = 0.3
NEAR_DUP_MIN_SAVED_BYTES = 128
NEAR_DUP_MAX_RENDERING_FRACTION = 0.45

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


@dataclass(frozen=True)
class _ArraySource:
    """A kept-verbatim JSON dict-array unit usable as a near-dup reference.

    Only units that ship verbatim are registered, so a pointer at
    ``message_index`` always names a message where the shared rows remain
    visible.
    """

    message_index: int
    row_signatures: frozenset[str]


@dataclass
class _DedupState:
    """Per-``apply`` scan state (never carried across calls)."""

    query_context: str
    seen: dict[str, _FirstOccurrence] = field(default_factory=dict)
    array_sources: list[_ArraySource] = field(default_factory=list)
    exact_replaced: int = 0
    near_replaced: int = 0


def _content_hash(content: str) -> str:
    """SHA-256[:24] of the exact content bytes (the store's own default)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:_MARKER_HASH_LEN]


def _row_signature(row: Any) -> str:
    """Canonical signature for byte-identical-row matching across arrays."""
    return json.dumps(row, sort_keys=True, ensure_ascii=False)


def _parse_dict_array(content: str) -> list[dict[str, Any]] | None:
    """Parse ``content`` as a JSON array of objects; ``None`` otherwise."""
    if not content.lstrip().startswith("["):
        return None
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, list) or len(parsed) < NEAR_DUP_MIN_SHARED_ROWS:
        return None
    if not all(isinstance(row, dict) for row in parsed):
        return None
    return parsed


def duplicate_sentinel(ccr_hash: str, n_bytes: int, first_message_index: int) -> str:
    """Render the recoverable replacement for an elided exact duplicate.

    A JSON OBJECT string (not an array) so downstream compressors route it
    to the no-op object path. Carries the ``_ccr_dropped`` sentinel key
    (the engine's drop grammar), a ``<<ccr:HASH ...>>`` pointer resolvable
    in the CCR store, and the ``Retrieve original: hash=`` phrase that pins
    the content against any further compression pass.
    """
    marker = f"<<ccr:{ccr_hash} {n_bytes}_bytes_duplicate>>"
    note = (
        "duplicate tool output elided - byte-identical to the tool output in "
        f"message {first_message_index} of this conversation; full original: "
        f"{marker} (Retrieve original: hash={ccr_hash})"
    )
    return json.dumps({_SENTINEL_KEY: note}, ensure_ascii=False)


def near_duplicate_rendering(
    changed_rows: list[dict[str, Any]],
    *,
    ccr_hash: str,
    n_bytes: int,
    n_shared: int,
    n_total: int,
    source_message_index: int,
) -> str:
    """Render a near-duplicate array: differing rows + trailing sentinel.

    Mirrors the engine's lossy-output shape (data rows then a
    ``{"_ccr_dropped": ...}`` sentinel row) and carries the pinning phrase
    so no later pass re-compresses the pointer away.
    """
    marker = f"<<ccr:{ccr_hash} {n_bytes}_bytes_near_duplicate>>"
    note = (
        f"{n_shared} of {n_total} rows in this tool output are byte-identical to "
        f"rows shown in the tool output in message {source_message_index} of this "
        f"conversation and were elided; the {len(changed_rows)} differing rows are "
        f"kept above; full original: {marker} (Retrieve original: hash={ccr_hash})"
    )
    return json.dumps([*changed_rows, {_SENTINEL_KEY: note}], ensure_ascii=False)


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

        state = _DedupState(query_context=query_context)
        new_messages: list[dict[str, Any]] = []

        for index, message in enumerate(messages):
            replaceable = index > 0 and index >= frozen_count
            new_message = self._process_message(
                message,
                index=index,
                replaceable=replaceable,
                state=state,
            )
            new_messages.append(new_message)

        if state.exact_replaced == 0 and state.near_replaced == 0:
            tokens = tokenizer.count_messages(messages)
            return TransformResult(
                messages=messages,
                tokens_before=tokens,
                tokens_after=tokens,
                transforms_applied=[],
            )

        applied: list[str] = []
        if state.exact_replaced:
            applied.append(f"cross_message_dedup:exact:{state.exact_replaced}")
        if state.near_replaced:
            applied.append(f"cross_message_dedup:near:{state.near_replaced}")

        tokens_before = tokenizer.count_messages(messages)
        tokens_after = tokenizer.count_messages(new_messages)
        return TransformResult(
            messages=new_messages,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            transforms_applied=applied,
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
        state: _DedupState,
    ) -> dict[str, Any]:
        """Handle one message; returns the (possibly new) message."""
        role = message.get("role", "")
        content = message.get("content", "")

        # OpenAI-style tool message with string content.
        if role in _TOOL_ROLES and isinstance(content, str):
            if message.get("is_error"):
                return message
            new_content = self._dedup_unit(
                content,
                message_index=index,
                replaceable=replaceable,
                state=state,
                tool_name=str(message.get("name", "") or "") or None,
            )
            if new_content is None:
                return message
            return {**message, "content": new_content}

        # Anthropic-style content blocks: dedup tool_result blocks only.
        if isinstance(content, list):
            return self._process_blocks(
                message,
                content,
                index=index,
                replaceable=replaceable,
                state=state,
            )

        return message

    def _process_blocks(
        self,
        message: dict[str, Any],
        blocks: list[Any],
        *,
        index: int,
        replaceable: bool,
        state: _DedupState,
    ) -> dict[str, Any]:
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
                state=state,
                tool_name=None,
            )
            if new_content is None:
                new_blocks.append(block)
                continue
            new_blocks.append({**block, "content": new_content})
            replaced += 1
        if replaced == 0:
            return message
        return {**message, "content": new_blocks}

    # ------------------------------------------------------------------ #
    # The dedup unit: register first occurrence / replace later ones.
    # ------------------------------------------------------------------ #

    def _dedup_unit(
        self,
        content: str,
        *,
        message_index: int,
        replaceable: bool,
        state: _DedupState,
        tool_name: str | None,
    ) -> str | None:
        """Returns the replacement string, or ``None`` to keep the unit as-is.

        Tier order: exact match first (cheapest, biggest win), then the
        near-duplicate row-overlap tier. Units kept verbatim are registered
        as reference sources; replaced units are NOT (a pointer must always
        name a message that still shows the referenced bytes/rows).
        Store-before-replace holds for both tiers.
        """
        if len(content) < MIN_DEDUP_CHARS:
            return None

        ccr_hash = _content_hash(content)
        first = state.seen.get(ccr_hash)
        if first is not None and first.content == content:
            if replaceable:
                replacement = self._replace_exact(
                    content,
                    ccr_hash=ccr_hash,
                    first=first,
                    state=state,
                    tool_name=tool_name,
                )
                if replacement is not None:
                    return replacement
            return None

        rows = _parse_dict_array(content)
        if replaceable and rows is not None:
            replacement = self._replace_near_duplicate(
                content,
                rows,
                ccr_hash=ccr_hash,
                state=state,
                tool_name=tool_name,
            )
            if replacement is not None:
                return replacement

        # Kept verbatim — register as a reference source.
        if first is None:
            state.seen[ccr_hash] = _FirstOccurrence(message_index=message_index, content=content)
        if rows is not None:
            state.array_sources.append(
                _ArraySource(
                    message_index=message_index,
                    row_signatures=frozenset(_row_signature(r) for r in rows),
                )
            )
        return None

    def _replace_exact(
        self,
        content: str,
        *,
        ccr_hash: str,
        first: _FirstOccurrence,
        state: _DedupState,
        tool_name: str | None,
    ) -> str | None:
        """Exact tier: full replacement by the duplicate sentinel."""
        sentinel = duplicate_sentinel(ccr_hash, len(content.encode("utf-8")), first.message_index)
        if not self._persist_original(
            content,
            sentinel,
            ccr_hash=ccr_hash,
            query_context=state.query_context,
            tool_name=tool_name,
        ):
            return None
        state.exact_replaced += 1
        return sentinel

    def _replace_near_duplicate(
        self,
        content: str,
        rows: list[dict[str, Any]],
        *,
        ccr_hash: str,
        state: _DedupState,
        tool_name: str | None,
    ) -> str | None:
        """Near tier: ship only differing rows + a sentinel for shared rows.

        Fires only when the byte-identical row overlap with the best earlier
        kept-verbatim array passes every gate (row count, absolute shared
        bytes, shared fraction, real byte savings) — otherwise the unit is
        left untouched and becomes a reference source itself.
        """
        signatures = [_row_signature(r) for r in rows]
        best: _ArraySource | None = None
        best_shared = 0
        for source in state.array_sources:
            shared = sum(1 for sig in signatures if sig in source.row_signatures)
            if shared > best_shared:
                best_shared = shared
                best = source

        if best is None or best_shared < NEAR_DUP_MIN_SHARED_ROWS:
            return None

        shared_bytes = sum(
            len(sig.encode("utf-8")) for sig in signatures if sig in best.row_signatures
        )
        total_bytes = len(content.encode("utf-8"))
        if shared_bytes < NEAR_DUP_MIN_SHARED_BYTES:
            return None
        if shared_bytes / total_bytes < NEAR_DUP_MIN_SHARED_FRACTION:
            return None

        changed_rows = [row for row, sig in zip(rows, signatures) if sig not in best.row_signatures]
        rendering = near_duplicate_rendering(
            changed_rows,
            ccr_hash=ccr_hash,
            n_bytes=total_bytes,
            n_shared=best_shared,
            n_total=len(rows),
            source_message_index=best.message_index,
        )
        rendering_bytes = len(rendering.encode("utf-8"))
        if total_bytes - rendering_bytes < NEAR_DUP_MIN_SAVED_BYTES:
            return None
        # Counterfactual gate: beat what per-message lossless compression
        # would achieve on the untouched unit, not just the raw original.
        if rendering_bytes > total_bytes * NEAR_DUP_MAX_RENDERING_FRACTION:
            return None
        if not self._persist_original(
            content,
            rendering,
            ccr_hash=ccr_hash,
            query_context=state.query_context,
            tool_name=tool_name,
        ):
            return None
        state.near_replaced += 1
        return rendering

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
