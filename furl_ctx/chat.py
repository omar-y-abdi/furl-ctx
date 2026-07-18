"""Cross-turn / whole-history presets — thin wrappers over :func:`compress`.

Two library-only helpers for compressing a full multi-turn conversation
(``messages[]``), the context the single-message CLI hook never passes:

* :func:`compress_chat_history` — the chat-history preset:
  ``compress_user_messages=True`` (user turns often carry the biggest tool
  outputs across a conversation), ``protect_recent=2`` (keep the live tail
  intact), and the router's retrieval-feedback loop on (adaptive routing from
  the CCR store's own retrieval bookkeeping). Feeding the FULL history through
  this path is also what *activates* :class:`ReadLifecycleManager` — it is
  already wired at ``router_engine`` and enabled by default, but stale/
  superseded Read detection is a no-op on a single message and only has
  something to do once multi-turn history is present.

* :func:`compress_with_cache` — a prompt-cache-aware helper: freeze the first
  ``freeze_up_to_n`` messages (the provider's stable prefix) so Furl never
  rewrites them and the prompt cache keeps hitting, while everything after the
  breakpoint is compressed normally.

Both return a :class:`CompressResult` and inherit ``compress()``'s fail-open
contract — neither raises on the happy path; a swallowed failure lands in
``result.error``. Neither mutates the caller's ``messages``.
"""

from __future__ import annotations

import copy
from typing import Any

from .compress import CompressResult, compress
from .transforms import ContentRouter, ContentRouterConfig
from .transforms.pipeline import TransformPipeline


def _retrieval_feedback_pipeline() -> TransformPipeline:
    """Default pipeline with the ContentRouter's retrieval-feedback loop on.

    Reuses ``TransformPipeline``'s own ``_build_default_transforms`` (so the
    transform order and every other transform stay exactly as the default),
    then surgically swaps the one ``ContentRouter`` for one whose config sets
    ``enable_retrieval_feedback=True``. Building via a swap rather than
    hand-mirroring the order means a future change to the default order is
    picked up automatically. ``read_lifecycle`` stays default-enabled in the
    new router config, so ``ReadLifecycleManager`` still fires.

    Built per call: the router's feedback aggregator/registry are lazy and
    the surface is new, so this stays bench-neutral and shares no mutable
    state between calls.
    """
    base = TransformPipeline()
    transforms = [
        ContentRouter(ContentRouterConfig(enable_retrieval_feedback=True))
        if isinstance(transform, ContentRouter)
        else transform
        for transform in base.transforms
    ]
    return TransformPipeline(transforms=transforms)


def compress_chat_history(messages: list[dict[str, Any]], **kwargs: Any) -> CompressResult:
    """Compress a full multi-turn conversation (chat-history preset).

    Equivalent to ``compress`` with ``compress_user_messages=True``,
    ``protect_recent=2``, and the router's retrieval-feedback loop enabled.
    Passing the whole history (not one message) is what gives the already-wired
    :class:`ReadLifecycleManager` something to do — stale/superseded Read
    outputs across turns become replaceable.

    Extra ``**kwargs`` forward to :func:`compress` (e.g. ``model``, ``config``,
    or a ``protect_recent`` override), so the preset's defaults can be tuned
    per call. Explicit ``compress_user_messages`` / ``protect_recent`` in
    ``kwargs`` win over the preset defaults.

    Args:
        messages: Full conversation in Anthropic or OpenAI format.
        **kwargs: Forwarded to :func:`compress`.

    Returns:
        :class:`CompressResult` — fail-open, never raises on the happy path.
    """
    preset: dict[str, Any] = {"compress_user_messages": True, "protect_recent": 2}
    preset.update(kwargs)
    preset.setdefault("pipeline", _retrieval_feedback_pipeline())
    return compress(messages, **preset)


def compress_with_cache(
    messages: list[dict[str, Any]],
    freeze_up_to_n: int,
    **kwargs: Any,
) -> CompressResult:
    """Compress while freezing the first ``freeze_up_to_n`` messages.

    Marks message ``freeze_up_to_n - 1`` with an Anthropic ``cache_control``
    breakpoint, so ``compress`` treats messages ``[0, freeze_up_to_n)`` as the
    provider's frozen prefix: those bytes are never rewritten (the prompt cache
    keeps hitting) and everything after the breakpoint is compressed normally.

    String-content messages are lifted to a single ``text`` block carrying the
    marker (``compress`` only reads ``cache_control`` off list-content blocks);
    without this a plain ``{"role", "content": str}`` message would freeze
    nothing. Only the one marked message is copied — the caller's ``messages``
    and the other message objects are never mutated.

    ``freeze_up_to_n <= 0`` marks nothing (plain :func:`compress`); a value past
    the end is clamped to the last message. Extra ``**kwargs`` forward to
    :func:`compress`.

    Args:
        messages: Full conversation in Anthropic or OpenAI format.
        freeze_up_to_n: Number of leading messages to freeze.
        **kwargs: Forwarded to :func:`compress`.

    Returns:
        :class:`CompressResult` — fail-open, never raises on the happy path.
    """
    if not messages or freeze_up_to_n <= 0:
        return compress(messages, **kwargs)

    mark_index = min(freeze_up_to_n, len(messages)) - 1
    frozen = list(messages)
    frozen[mark_index] = _with_cache_marker(messages[mark_index])
    return compress(frozen, **kwargs)


def _with_cache_marker(message: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *message* carrying an Anthropic ``cache_control`` marker.

    The marker goes on the LAST content block (list content), or on a lifted
    single ``text`` block (string content). Pure and total: never mutates the
    input; unknown content shapes are copied and marked defensively so the
    frozen-prefix count still advances.
    """
    marked = copy.deepcopy(message)
    content = marked.get("content")

    if isinstance(content, str):
        # An EMPTY string would produce an empty ``"text": ""`` block, which the
        # Anthropic API rejects with a 400 (Bug-5) — anchor a single space
        # instead so the cache_control marker still rides a valid block.
        marked["content"] = [
            {"type": "text", "text": content or " ", "cache_control": {"type": "ephemeral"}}
        ]
        return marked

    if isinstance(content, list) and content:
        # Mark the LAST dict block (searching from the end): attaching
        # cache_control to a real block avoids appending an empty text block —
        # which the Anthropic API rejects with a 400 (Bug-5).
        for block in reversed(content):
            if isinstance(block, dict):
                block["cache_control"] = {"type": "ephemeral"}
                return marked
        # A list with no dict block at all: append a MINIMAL NON-EMPTY anchor.
        content.append({"type": "text", "text": " ", "cache_control": {"type": "ephemeral"}})
        return marked

    # No usable content block to hang the marker on (empty/None/other): add a
    # minimal NON-EMPTY text block so the frozen-prefix floor still advances past
    # this message. Non-empty is required — an empty ``"text": ""`` block is a 400
    # from the Anthropic API (Bug-5). lazy: covers only the shapes compress()
    # itself reads; exotic provider block layouts fall back to this benign block.
    marked["content"] = [{"type": "text", "text": " ", "cache_control": {"type": "ephemeral"}}]
    return marked
