"""One-function compression API for Furl.

The simplest way to use Furl — no proxy, no config, just compress:

    from furl_ctx import compress

    result = compress(messages, model="claude-sonnet-4-5-20250929")
    result.messages          # Compressed messages (same format, fewer tokens)
    result.tokens_saved      # Tokens saved
    result.compression_ratio # e.g., 0.65 means 65% saved

Works with any LLM client, any proxy, any framework. Just compress
the messages before sending them.

Examples:

    # With Anthropic SDK
    from anthropic import Anthropic
    from furl_ctx import compress

    client = Anthropic()
    messages = [{"role": "user", "content": huge_tool_output}]
    compressed = compress(messages, model="claude-sonnet-4-5-20250929")
    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        messages=compressed.messages,
    )

    # With OpenAI SDK
    from openai import OpenAI
    from furl_ctx import compress

    client = OpenAI()
    messages = [{"role": "user", "content": "analyze this"}, {"role": "tool", "content": big_data}]
    compressed = compress(messages, model="gpt-4o")
    response = client.chat.completions.create(model="gpt-4o", messages=compressed.messages)

    # With LiteLLM
    import litellm
    from furl_ctx import compress

    messages = [...]
    compressed = compress(messages, model="bedrock/claude-sonnet")
    response = litellm.completion(model="bedrock/claude-sonnet", messages=compressed.messages)

    # With any HTTP client
    import httpx
    from furl_ctx import compress

    compressed = compress(messages, model="claude-sonnet-4-5-20250929")
    httpx.post("https://api.anthropic.com/v1/messages", json={
        "model": "claude-sonnet-4-5-20250929",
        "messages": compressed.messages,
    })
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from .ccr.marker_grammar import hashes_in_text
from .config import DEFAULT_MIN_TOKENS_TO_COMPRESS
from .pipeline import PipelineExtensionManager, PipelineStage, summarize_routing_markers
from .redaction import build_store_redactor, compose_redactors
from .utils import extract_user_query as _extract_user_query

if TYPE_CHECKING:
    from collections.abc import Callable

    from .hooks import CompressionHooks

logger = logging.getLogger(__name__)


# Lazy-initialized singleton pipeline
_pipeline = None
_pipeline_lock = threading.Lock()

# Warn when the frozen prefix swallows (nearly) the whole conversation:
# cache_control on the LAST message — the multi-turn idiom Anthropic's docs
# teach — freezes everything, every transform skips, and compression reports
# 0 saved with no error. > 0.9 also catches the "all but the newest turn"
# shape, where the same surprise applies.
_FROZEN_WARN_FRACTION = 0.9

# Roles whose string content is a tool output the replacement transforms
# rewrite (mirrors cross_message_dedup._TOOL_ROLES; ReadLifecycleManager
# handles the "tool" subset). Used by the frozen-prefix conflict detector.
_TOOL_OUTPUT_ROLES = frozenset({"tool", "function"})

# Per-retrieve token overhead used to price a CCR round trip: the tool name
# plus the hash argument a retrieve call spends, conservative. Matches the
# verify harness's RETRIEVE_CALL_OVERHEAD_TOKENS so opaque_offloads economics
# and the effective-savings benchmark price a round trip the same way.
_CCR_RETRIEVE_OVERHEAD_TOKENS = 12


@dataclass
class CompressConfig:
    """User-facing compression options.

    Controls what gets compressed, how aggressively, and with which model.
    Pass to ``compress()`` or any integration that uses furl_ctx.

    Examples::

        # Coding agent (default — skip user messages, protect recent)
        compress(messages, model="gpt-4o")

        # Document pipeline (compress everything)
        compress(messages, model="claude-opus-4-20250514",
            compress_user_messages=True,
            protect_recent=0,
        )
    """

    # What to compress
    compress_user_messages: bool = False
    """Compress user messages too (default: skip them for coding agents).
    Set True for document compression, RAG pipelines, or when user messages
    contain large tool outputs."""

    compress_system_messages: bool = True
    """Compress system messages (default: True).
    Set False to preserve system prompts exactly as-is. Useful for voice
    agents where tool definitions and instructions must not be altered."""

    protect_recent: int = 4
    """Protect the last N messages' CODE from compression and exempt those
    messages from cross-message dedup. This does NOT skip compression
    entirely for the window: non-code content (logs, JSON, search output)
    in recent messages is still compressed — covered by
    ``min_tokens_to_compress`` and CCR reversibility. Set 0 to disable
    the window and compress everything."""

    protect_analysis_context: bool = True
    """Detect 'analyze'/'review' intent and protect code from compression."""

    min_tokens_to_compress: int = DEFAULT_MIN_TOKENS_TO_COMPRESS
    """Minimum token count for a message to be compressed.
    Messages shorter than this are left unchanged. Default 250.
    Set lower for voice agents where turns are short."""

    redactor: Callable[[str], str] | None = None
    """Opt-in content redactor (B3 SECURITY): a PURE function
    ``raw content -> redacted content`` applied to every string message
    ``content`` BEFORE compression. Default ``None`` disables it — behavior is
    then byte-identical to today.

    FAIL-CLOSED — this is the security invariant. Redaction runs OUTSIDE
    ``compress()``'s fail-open boundary: if the redactor RAISES, the exception
    propagates and ``compress()`` raises, so unredacted content is never
    compressed, offloaded to the CCR store, returned, or swallowed by the
    fail-open path. On a redactor error you get no output rather than a leak.

    Because downstream compression/offload/store only ever see redacted
    content, a later ``retrieve()`` returns the REDACTED original — the secret
    is gone from the store BY DESIGN. Non-string content passes through
    untouched; the caller's input list/dicts are never mutated."""


@dataclass(frozen=True)
class OpaqueOffload:
    """A whole-blob CCR offload the compressor could not structurally shrink.

    When no transform can compress a piece of content, the router moves the
    entire blob to the CCR store behind a marker rather than applying a
    reversible in-place transform. The discriminator is the store entry's
    ``compression_strategy == "ccr_offload"``: that is the whole-blob fallback,
    as opposed to a granular per-row drop whose strategy is one of several other
    values such as ``smart_crusher_row_drop`` or ``smart_crusher_compact_document``.
    The marker's raw "savings" are almost entirely opaque offload: the bytes are
    in the store, not gone. Retrieving the content back returns the ENTIRE
    payload, so a retrieval round trip costs MORE than the offload saved
    (``net_negative_on_retrieval``). Source code is the canonical trigger: it
    does not compress structurally, so it reliably lands on this path. A granular
    per-row drop is different: a caller retrieves only the rows it needs and
    stays net-positive, so it is not reported here.

    Read this off ``CompressResult.opaque_offloads`` at whatever cadence your
    layer can afford. It is deliberately NOT a per-call log line: Furl's hooks
    spawn a fresh subprocess per tool call, so per-call stderr would explode
    into spam (see the ANTHROPIC_O200K_PROXY_NOTE precedent).

    Attributes:
        hash: CCR recovery hash, also present in ``CompressResult.ccr_hashes``.
        tool_name: Originating tool for the offloaded content (Furl's
            ``content_kind``), or None when unattributed.
        offloaded_tokens: Tokens moved to the store; the whole payload a
            retrieval brings back.
        preview_tokens: Tokens of the visible summary/preview left inline.
        net_tokens_if_retrieved: Signed net token change if the blob is
            retrieved back: what the offload saved
            (``offloaded_tokens - preview_tokens``) minus what retrieval pays
            back (``offloaded_tokens`` plus a per-call overhead). Negative means
            the round trip costs more than the offload saved.
        net_negative_on_retrieval: Derived, ``net_tokens_if_retrieved < 0``.
            True for a whole-blob offload, since retrieval always brings back the
            whole payload while the preview it saved is smaller.
    """

    hash: str
    tool_name: str | None
    offloaded_tokens: int
    preview_tokens: int
    net_tokens_if_retrieved: int
    net_negative_on_retrieval: bool


@dataclass
class CompressResult:
    """Result of compressing messages.

    Attributes:
        messages: The compressed messages (same format as input).
        tokens_before: Token count before compression.
        tokens_after: Token count after compression.
        tokens_saved: Tokens removed by compression.
        compression_ratio: Ratio of tokens saved (0.0 = no savings, 1.0 = 100% removed).
        transforms_applied: List of transforms that were applied.
        error: Failure description when compression failed and the original
            messages were returned unchanged (fail-open). None on success and
            on genuine no-ops (empty input, optimize=False). Lets callers tell
            a swallowed pipeline failure apart from a real "nothing to do".
        warnings: Non-fatal problems detected while compressing (each is also
            logged at WARNING). Aggregates transform-level warnings with
            compress()-level diagnostics — e.g. a ``cache_control`` breakpoint
            freezing the whole conversation (0 tokens can be saved), or a
            frozen message whose bytes Furl previously shipped compressed
            (a guaranteed provider prefix-cache miss). Empty on clean runs.
        opaque_offloads: Whole-blob CCR offloads THIS compression created where
            the marker replaced content nothing could structurally shrink (see
            :class:`OpaqueOffload`). The reported "savings" are mostly opaque
            offload, and a retrieval round trip is net-negative. Empty when
            every compression was a reversible structural transform or a cheap
            granular per-row drop. Read as a structured field, never logged
            per-call (the fresh-subprocess-per-call hook environment would turn
            a per-call log into stderr spam).
    """

    messages: list[dict[str, Any]]
    tokens_before: int = 0
    tokens_after: int = 0
    tokens_saved: int = 0
    compression_ratio: float = 0.0
    transforms_applied: list[str] = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    opaque_offloads: list[OpaqueOffload] = field(default_factory=list)

    @property
    def ccr_hashes(self) -> list[str]:
        """CCR marker hashes present in the compressed messages (first-seen order).

        Derived from ``messages`` so it can never drift from what shipped. Pass
        each to ``furl_ctx.retrieve`` / ``resolve_markers`` to recover the content.

        Total over every message shape: string content is scanned directly;
        non-string content (block lists, or the raw ``bytes`` ``compress()``
        passes through untouched) is serialized via
        ``json.dumps(..., default=str)`` and the RENDERING scanned — so a
        marker embedded in a text block still surfaces, and bytes whose repr
        carries literal marker text surface those hashes too (consistent with
        foreign marker text appearing in string content). Only content that
        fails to serialize even with ``default=str`` (circular references,
        tuple dict keys) contributes nothing — skipped rather than raising,
        mirroring the sibling scanner ``_surfaced_ccr_hashes``.
        """
        return _ordered_ccr_hashes(self.messages)


def _ordered_ccr_hashes(messages: list[dict[str, Any]]) -> list[str]:
    """CCR marker hashes in *messages*' content, first-seen order.

    Shared by :attr:`CompressResult.ccr_hashes` and the opaque-offload detector
    so the hashes a caller sees on ``ccr_hashes`` and on ``opaque_offloads``
    are extracted identically and never drift. String content is scanned
    directly; other content is serialized with ``default=str`` and the
    rendering scanned; content that fails to serialize contributes nothing.
    """
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        try:
            parts.append(json.dumps(content, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            continue
    return hashes_in_text("\n".join(parts))


def _detect_opaque_offloads(
    compressed_messages: list[dict[str, Any]],
    input_messages: list[dict[str, Any]],
) -> list[OpaqueOffload]:
    """Opaque whole-blob CCR offloads THIS compression created.

    An offload is opaque when the router could not structurally shrink the
    content and stored the whole blob under a marker, identified by the store
    entry's ``compression_strategy == CCR_OFFLOAD``. Retrieving it returns the
    entire payload, so the round trip is net-negative. Granular per-row drops
    carry a different strategy (one of several, such as ``smart_crusher_row_drop``
    or ``smart_crusher_compact_document``) and are cheap to retrieve, so they are
    excluded. Hashes the INPUT already carried (previous turns' markers) are
    excluded so only offloads created now are reported.

    Pure metadata lookups — no original content is fetched. Never raises: a
    diagnostic must not break a successful compression, and a single malformed
    store entry is skipped, not fatal. Returns first-seen order, matching
    ``ccr_hashes``.
    """
    surfaced = _ordered_ccr_hashes(compressed_messages)
    if not surfaced:
        return []
    try:
        from .cache.compression_store import get_compression_store
        from .transforms.router_policy import CompressionStrategy

        opaque_strategy = CompressionStrategy.CCR_OFFLOAD.value
        pre_existing = set(_ordered_ccr_hashes(input_messages))
        store = get_compression_store()
    except Exception:  # noqa: BLE001 - diagnostics must never break the request
        logger.debug("opaque-offload detection setup failed (non-fatal)", exc_info=True)
        return []

    offloads: list[OpaqueOffload] = []
    for ccr_hash in surfaced:
        if ccr_hash in pre_existing:
            continue
        # The whole per-hash body is guarded: a lookup failure OR a malformed
        # entry (e.g. a non-numeric token value) must skip that entry, never
        # turn a successful compression into a fail-open no-op.
        try:
            meta = store.get_metadata(ccr_hash)
            if not meta or meta.get("compression_strategy") != opaque_strategy:
                continue
            offloaded_tokens = int(meta.get("original_tokens") or 0)
            preview_tokens = int(meta.get("compressed_tokens") or 0)
            # Independently-computed terms: what the offload saved vs what a
            # retrieval pays back (the whole blob plus one call). The sign of
            # their difference is the round-trip economics.
            saved = offloaded_tokens - preview_tokens
            retrieval_cost = offloaded_tokens + _CCR_RETRIEVE_OVERHEAD_TOKENS
            # Signed net token change if this offloaded blob is retrieved back:
            # the two terms are INDEPENDENT inputs, so their difference is a
            # real economic comparison, not a restatement of either one. A
            # negative result means the round trip costs more than the
            # offload saved; a positive result means it is still ahead.
            net = saved - retrieval_cost
            offloads.append(
                OpaqueOffload(
                    hash=ccr_hash,
                    tool_name=meta.get("tool_name"),
                    offloaded_tokens=offloaded_tokens,
                    preview_tokens=preview_tokens,
                    net_tokens_if_retrieved=net,
                    net_negative_on_retrieval=net < 0,
                )
            )
        except Exception:  # noqa: BLE001 - a malformed entry is skipped, not fatal
            logger.debug("opaque-offload entry skipped (non-fatal)", exc_info=True)
            continue
    return offloads


def _compute_frozen_message_count(messages: list[dict[str, Any]]) -> int:
    """Return the frozen-prefix message count for a list of Anthropic messages.

    The authoritative frozen-count implementation (the former Rust
    ``cache_control::compute_frozen_count`` was orphaned — no PyO3 binding, no
    caller — and was removed in the standalone excise):

    - Walk ``messages[i].content[*]``; for each block (dict) that has a
      top-level ``cache_control`` key, record ``i`` as the highest marker index.
    - Return ``highest_index + 1`` (exclusive floor: the marked message is part
      of the cached prefix and must itself be frozen), or ``0`` if no marker.
    - String-content messages are skipped (no block list → no markers possible).
    - Only the ``messages`` field is inspected; ``system`` and ``tools`` markers
      are never passed here and never bump the floor.
    - ``cache_control: null`` counts as present (key-presence, not truthiness).

    Args:
        messages: List of message dicts in Anthropic format.

    Returns:
        Frozen message count (0 if no cache_control markers found).
    """
    highest_index: int | None = None
    for i, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            # String content: no block list, no cache_control possible.
            continue
        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                highest_index = i if highest_index is None else max(highest_index, i)
    return (highest_index + 1) if highest_index is not None else 0


def _frozen_prefix_warning(total_messages: int, frozen: int) -> str | None:
    """Warning when the cache_control floor freezes (nearly) every message.

    With ``cache_control`` on the LAST message — the multi-turn idiom
    Anthropic's docs teach — ``frozen == len(messages)``: every transform
    skips everything, 0 tokens are saved, ``error`` stays None, and without
    this warning there is no way to notice. Pure and total: returns the
    warning string, or None when the frozen fraction is unremarkable.

    Args:
        total_messages: Number of messages in the request.
        frozen: Frozen-prefix message count (see
            :func:`_compute_frozen_message_count`).

    Returns:
        Warning text when ``frozen == total_messages`` or the frozen fraction
        exceeds ``_FROZEN_WARN_FRACTION``; None otherwise.
    """
    if total_messages <= 0 or frozen <= 0:
        return None
    if frozen >= total_messages:
        return (
            f"cache_control on the last message freezes all {total_messages} "
            "messages: every transform skips the frozen prefix, so 0 tokens "
            "can be saved. Mark the cache breakpoint BEFORE the live zone you "
            "want compressed, or compress before marking."
        )
    fraction = frozen / total_messages
    if fraction > _FROZEN_WARN_FRACTION:
        return (
            f"cache_control freezes {frozen} of {total_messages} messages "
            f"({fraction:.0%}): nearly the whole conversation is skipped by "
            "every transform. Mark the cache breakpoint BEFORE the live zone "
            "you want compressed, or compress before marking."
        )
    return None


def _frozen_tool_output_units(message: dict[str, Any]) -> list[str]:
    """Tool-output strings in *message* that replacement transforms rewrite.

    OpenAI-style tool/function messages with string content, and
    Anthropic-style ``tool_result`` blocks with string content — the two
    shapes ``CrossMessageDeduper`` / ``ReadLifecycleManager`` replace. Pure
    and total: any other shape yields an empty list.
    """
    role = message.get("role", "")
    content = message.get("content", "")
    if role in _TOOL_OUTPUT_ROLES and isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        block["content"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_result"
        and isinstance(block.get("content"), str)
    ]


def _frozen_transformed_content_warning(messages: list[dict[str, Any]], frozen: int) -> str | None:
    """Detect a cache breakpoint moved PAST previously-compressed turns.

    The frozen prefix freezes the INPUT bytes, but what the provider cached
    last turn is whatever Furl SHIPPED last turn. A caller who re-sends
    the original (uncompressed) history each turn and moves the cache_control
    marker forward across a message that previously shipped transformed
    (dedup sentinel, read-lifecycle marker) gets (a) a guaranteed prefix-cache
    miss at the very message asserted cached, then (b) a permanent token
    regression — the message is frozen, so it ships uncompressed forever —
    with no signal. There is no exact stateless test for this, so this is a
    cheap best-effort detector over the CCR registry:

    * a frozen tool output that is a byte-identical LATER duplicate of an
      earlier frozen unit, with a CCR entry for those bytes → it previously
      shipped as a ``cross_message_dedup`` sentinel (dedup only ever rewrites
      later copies, and the earlier copy is in the frozen prefix too);
    * a frozen tool output whose CCR entry stored ``compressed_content == ""``
      → it previously shipped as a read-lifecycle marker (the only writer of
      empty compressed content).

    Best-effort on purpose: CCR entries expire with the store TTL, the store
    is process-global (a hit can come from a sibling conversation), and
    router-side lossy compressions are not covered — a None here is not proof
    of safety. The contract itself is documented on :func:`compress`.

    Returns:
        Warning text naming the suspect message indexes, or None. Never
        raises — diagnostics must not break the request.
    """
    if frozen <= 0 or not messages:
        return None
    try:
        from .cache.compression_store import get_compression_store
        from .transforms.cross_message_dedup import MIN_DEDUP_CHARS, _content_hash

        store = get_compression_store()
        seen_frozen_hashes: set[str] = set()
        suspect_indexes: list[int] = []

        for index in range(min(frozen, len(messages))):
            for unit in _frozen_tool_output_units(messages[index]):
                if len(unit) < MIN_DEDUP_CHARS:
                    # Below the smallest unit any replacement transform
                    # rewrites — and well-behaved callers' frozen sentinels
                    # land here, so the common path does zero store lookups.
                    continue
                unit_hash = _content_hash(unit)
                duplicate_of_frozen = unit_hash in seen_frozen_hashes
                seen_frozen_hashes.add(unit_hash)
                metadata = store.get_metadata(unit_hash)
                if metadata is None:
                    continue
                previously_dedup_sentinel = duplicate_of_frozen
                previously_lifecycle_marker = metadata.get("compressed_content") == ""
                if previously_dedup_sentinel or previously_lifecycle_marker:
                    suspect_indexes.append(index)
                    break  # One suspect unit per message is enough.

        if not suspect_indexes:
            return None
        indexes = ", ".join(str(i) for i in suspect_indexes)
        return (
            f"frozen prefix message(s) [{indexes}] contain tool output that "
            "Furl previously shipped in compressed form (CCR registry "
            "hit): the provider cached the TRANSFORMED bytes, so re-sending "
            "the original history with the cache breakpoint moved past that "
            "turn guarantees a prefix-cache miss there and pins the message "
            "uncompressed forever. Pass the previously returned "
            "result.messages back in, or do not move the cache_control "
            "marker past turns that already shipped compressed."
        )
    except Exception:  # noqa: BLE001 - best-effort diagnostics only
        logger.debug(
            "frozen-prefix transformed-content detection failed (non-fatal)",
            exc_info=True,
        )
        return None


def _surfaced_ccr_hashes(messages: list[dict[str, Any]]) -> set[str]:
    """Distinct CCR recovery-pointer hashes present in *messages*' content.

    Uses the owned marker scanner (``CcrMirror.extract_ccr_hashes``) so the
    parse grammar stays in one place. Block-format content is serialized to
    JSON text first, which the scanner walks structurally. Pure and total:
    unparseable/absent content contributes nothing.
    """
    from .transforms.router_ccr_mirror import CcrMirror

    hashes: set[str] = set()
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            hashes |= CcrMirror.extract_ccr_hashes(content)
        elif content is not None:
            try:
                text = json.dumps(content, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                continue
            hashes |= CcrMirror.extract_ccr_hashes(text)
    return hashes


def _event_ccr_hashes(
    markers_inserted: list[str],
    pre_existing: set[str],
    compressed_messages: list[dict[str, Any]],
) -> list[str]:
    """CCR hashes newly surfaced by THIS compression, for ``CompressEvent``.

    Union of (a) hashes scraped from the returned messages minus the ones the
    INPUT already carried (previous turns' markers must not be re-reported)
    and (b) the pipeline's ``markers_inserted`` — which carries the
    read-lifecycle store hashes whose marker text (shape I) the ``<<ccr:``
    scanner deliberately does not match. Filtered to the strict 12/24-hex
    consumer widths; sorted for determinism.
    """
    from .ccr.marker_grammar import is_valid_ccr_hash
    from .transforms.router_ccr_mirror import CcrMirror

    surfaced = _surfaced_ccr_hashes(compressed_messages) - pre_existing
    for marker in markers_inserted:
        if is_valid_ccr_hash(marker):
            surfaced.add(marker)
        else:
            surfaced |= CcrMirror.extract_ccr_hashes(marker)
    return sorted(h for h in surfaced if is_valid_ccr_hash(h))


def _redact_messages(
    messages: list[dict[str, Any]], redactor: Callable[[str], str]
) -> list[dict[str, Any]]:
    """Return NEW messages with every string ``content`` passed through *redactor*.

    FAIL-CLOSED helper for the B3 redaction step. Called BEFORE compress()'s
    fail-open boundary, so a ``redactor`` that RAISES propagates out of
    ``compress()`` and no unredacted content is ever compressed, stored, or
    returned. Immutable: builds new message dicts; non-string content (and the
    caller's original list/dicts) are left untouched.
    """
    redacted: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            redacted.append({**message, "content": redactor(content)})
        else:
            redacted.append(message)
    return redacted


def _unknown_model_warning(model: str) -> str | None:
    """A warning when *model* matches no known tokenizer family (F-alpha4).

    ``get_tokenizer`` silently falls back to generic character-estimation for an
    unrecognized model name, so token counts can be off with no signal. This
    surfaces that fallback through ``result.warnings``. Returns None for a
    recognized model, using the public ``list_supported_models`` pattern set as
    the single source of truth (the same patterns ``_detect_backend`` matches on),
    so a real model family is never flagged.
    """
    from furl_ctx.tokenizers import list_supported_models

    model_lower = model.lower()
    if any(re.match(pattern, model_lower) for pattern in list_supported_models()):
        return None
    return (
        f"model '{model}' is not a recognized tokenizer family, so token counts use "
        "the generic character-estimation fallback and can be off for this model. "
        "Pass a known model name, for example a gpt, claude, gemini, or command "
        "family name, or register a tokenizer for it."
    )


def compress(
    messages: list[dict[str, Any]],
    model: str = "claude-sonnet-4-5-20250929",
    model_limit: int = 200000,
    optimize: bool = True,
    hooks: CompressionHooks | None = None,
    config: CompressConfig | None = None,
    pipeline: Any | None = None,
    session_id: str | None = None,
    agent_id: str | None = None,
    tool_name: str | None = None,
    **kwargs: Any,
) -> CompressResult:
    """Compress messages using Furl's full compression pipeline.

    This is the simplest way to use Furl. No proxy, no config needed.
    Just pass messages and get compressed messages back.

    Args:
        messages: List of messages in Anthropic or OpenAI format.
        model: Model name (used for token counting and context limit).
        model_limit: Model's context window size in tokens.
        optimize: Whether to actually compress (False = passthrough for A/B testing).
        hooks: Optional CompressionHooks instance for custom behavior.
        config: Compression options (CompressConfig). Overrides defaults.
        pipeline: Pre-built ``TransformPipeline`` to run instead of the shared
            default singleton. Default ``None`` uses the process-wide singleton
            (unchanged behavior). Supplying one lets a caller select a
            differently-configured pipeline (e.g. a ``lossless_only`` or more
            aggressive ``ContentRouter``) for THIS call without disturbing the
            global default — the fail-open, token-counting, and hook wiring
            around it are identical either way.
        session_id: Optional tenant/session identifier. When set (or with
            ``FURL_CCR_NAMESPACE`` / ``agent_id``), this call reads and writes
            an ISOLATED per-namespace CCR store instead of the process-global
            one, so an entry another tenant stored is never retrievable here.
            Default ``None`` with no namespace env keeps today's global
            behavior byte-for-byte.
        agent_id: Optional sub-agent identifier, combined with ``session_id``
            and ``FURL_CCR_NAMESPACE`` to form the CCR isolation boundary. Same
            zero-change default as ``session_id``.
        tool_name: Originating tool for this content (e.g. "Bash",
            "mcp:furl_compress"). Bound request-scoped for the call so CCR
            entries this compression writes — including the router's offload and
            SmartCrusher, which have no per-message tool attribution for a single
            wrapped tool output — record it as their ``content_kind`` (surfaced
            by furl_list / furl_retrieve). Default ``None`` leaves entries
            unlabeled exactly as before.
        **kwargs: Shorthand for CompressConfig fields. These override config:
            compress_user_messages, compress_system_messages, protect_recent,
            protect_analysis_context, min_tokens_to_compress.

    Prompt caching (``cache_control``):
        Messages up to and including the HIGHEST message carrying an
        Anthropic ``cache_control`` marker form the frozen prefix — Furl
        never modifies them, so the provider's prompt cache keeps hitting.
        Two consequences follow:

        * Marking the LAST message freezes the entire conversation: every
          transform skips everything and 0 tokens are saved (``error`` stays
          None). Mark the breakpoint BEFORE the live zone you want
          compressed, or compress before marking. Detected and surfaced via
          ``result.warnings``.
        * The frozen prefix freezes the bytes you PASS IN, while the provider
          cached the bytes Furl SHIPPED last turn. On multi-turn
          conversations, pass the previously returned ``result.messages``
          (the compressed history) back in — or do not move the marker
          forward past turns that already shipped compressed. Re-sending
          original history with a forward-moved marker guarantees a
          prefix-cache miss at the previously compressed message and pins it
          uncompressed forever (it is frozen). Best-effort detected via
          ``result.warnings``.

    Returns:
        CompressResult with compressed messages and metrics.

    Examples::

        # Default (coding agent)
        result = compress(messages, model="gpt-4o")

        # Document pipeline (compress everything)
        result = compress(messages, model="claude-opus-4-20250514",
            compress_user_messages=True,
            protect_recent=0,
        )
    """
    # A6: validate the message-list shape at the boundary so a programmer error
    # (a bare string / dict instead of a list of message dicts) is ONE concise,
    # actionable TypeError — not a bare ``str.get`` AttributeError raised from the
    # redaction step, nor the doubled fail-open + token-count-fallback tracebacks
    # the hook and MCP paths otherwise spilled on every such call. Mirrors the
    # unexpected-kwarg TypeError raised at this same boundary below.
    if not isinstance(messages, list):
        raise TypeError(
            "compress() expects a list of message dicts "
            "(e.g. [{'role': 'tool', 'content': '...'}]), got "
            f"{type(messages).__name__}"
        )

    if not messages or not optimize:
        return CompressResult(messages=messages)

    # Build config from explicit config + kwargs. Never mutate the
    # caller's CompressConfig — a reused config object must not carry
    # one call's kwarg overrides into the next call.
    cfg = config or CompressConfig()
    config_fields = {f.name for f in cfg.__dataclass_fields__.values()}
    overrides = {key: value for key, value in kwargs.items() if key in config_fields}
    unknown = sorted(set(kwargs) - config_fields)
    if unknown:
        # Fail fast at the public boundary — a typo'd field (e.g.
        # ``target_ration``) silently defaulting would hand back differently
        # compressed output than the caller asked for. Matches the strict
        # ``ContentRouter.apply()`` contract (and Python's own unexpected-kwarg
        # behaviour) rather than warning-and-ignoring.
        raise TypeError(
            f"compress() got unexpected keyword argument(s) "
            f"{', '.join(unknown)}; valid CompressConfig fields: "
            f"{', '.join(sorted(config_fields))}"
        )
    if overrides:
        cfg = replace(cfg, **overrides)

    # B3 SECURITY — fail-closed content redaction. This runs BEFORE and OUTSIDE
    # the fail-open ``try/except BaseException`` boundary below ON PURPOSE: if a
    # configured redactor RAISES, the exception must propagate (compress()
    # raises) so unredacted content is NEVER compressed, offloaded to the CCR
    # store, returned, or swallowed by the fail-open path. That is the security
    # invariant: on redactor error, no output rather than a leak. When redaction
    # succeeds, every downstream step (pipeline, offload, store) only ever sees
    # redacted content — so a later retrieve() returns the REDACTED original.
    #
    # Three redactors compose here (all apply, defense in depth): the ON-by-default
    # built-in credential patterns (audit Crit-4 / B3) run FIRST, then the
    # env-expressible ``FURL_REDACT_PATTERNS`` redactor — the ONLY redaction
    # channels the env-configured Claude Code plugin (hook + MCP server) can reach
    # — then the library ``CompressConfig.redactor`` callback. ``build_store_redactor()``
    # is ``None`` only when the built-ins are opted out (``FURL_REDACT_BUILTINS=0``)
    # AND no env patterns are set, so a caller who disables both plus passes no
    # callback keeps byte-identical behavior; otherwise credentials are scrubbed
    # before anything is compressed, offloaded, or stored.
    _active_redactor = compose_redactors(build_store_redactor(), cfg.redactor)
    if _active_redactor is not None:
        messages = _redact_messages(messages, _active_redactor)

    # Per-tenant CCR isolation (B2). When a namespace is active
    # (``session_id`` / ``agent_id`` / ``FURL_CCR_NAMESPACE``) bind that
    # tenant's isolated store to the request ContextVar for the duration of
    # this call, so the inline get_compression_store() reads in the transforms
    # and the diagnostic helper above resolve the tenant store instead of the
    # global one. Default (no namespace) resolves to None and the ContextVar is
    # left untouched — today's global behavior, byte-for-byte. The token is
    # RESET (never cleared) in the finally so an outer middleware store is
    # restored, and reset-always keeps the fail-open path clean.
    from .cache.compression_store import (
        _request_ccr_store,
        _request_tool_name,
        resolve_ccr_namespace_store,
    )

    _ccr_token = None
    _tool_name_token = None
    try:
        # content_kind threading: bind the originating tool for the whole call
        # so every store.store() this compression triggers (router offload,
        # SmartCrusher, dedup) inherits it as its default tool_name. RESET (not
        # cleared) in finally so a nested/outer compress() binding is restored.
        if tool_name is not None:
            _tool_name_token = _request_tool_name.set(tool_name)
        # Namespace resolution + the ContextVar bind sit INSIDE the fail-open
        # boundary too: this is the first place compress() constructs a store,
        # and a store-construction failure (e.g. a bad workspace path) must
        # fail open like every other compression error, never raise out of the
        # namespaced call.
        _ccr_store = resolve_ccr_namespace_store(session_id, agent_id)
        if _ccr_store is not None:
            _ccr_token = _request_ccr_store.set(_ccr_store)

        # Pipeline construction sits INSIDE the fail-open boundary (COR-43):
        # the import chain behind TransformPipeline hard-requires the
        # furl_ctx._core extension, so a broken/missing wheel raises
        # ModuleNotFoundError at first request — exactly the deployment
        # where "worst case: passthrough" must hold. The BaseException
        # handler below turns that into a passthrough CompressResult with
        # `error` set instead of letting it escape to the host. (Nothing is
        # cached on failure, so a later fixed environment recovers without
        # a restart.)
        pipeline = pipeline if pipeline is not None else _get_pipeline()
        pipeline_extensions = PipelineExtensionManager(hooks=hooks)

        # Compute biases from hooks if provided. The user query is extracted
        # HERE, before the hook invocations, so bias hooks following this
        # module's own examples can score by relevance instead of seeing an
        # empty query forever (API-2). The pipeline re-extracts below because
        # pre_compress / INPUT_RECEIVED may rewrite the messages.
        biases = None
        if hooks:
            from furl_ctx.hooks import CompressContext

            ctx = CompressContext(model=model, user_query=_extract_user_query(messages))
            messages = hooks.pre_compress(messages, ctx)
            biases = hooks.compute_biases(messages, ctx)

        received_event = pipeline_extensions.emit(
            PipelineStage.INPUT_RECEIVED,
            operation="compress",
            model=model,
            messages=messages,
        )
        if received_event.messages is not None:
            messages = received_event.messages

        # Extract user query from messages so transforms can score by
        # relevance.  Without this, SmartCrusher selects items by statistics
        # alone (position, anomaly) and may drop relevant content.
        context = _extract_user_query(messages)

        # Compute the frozen-prefix count from cache_control markers.
        # Must run AFTER pre_compress hook and INPUT_RECEIVED event may have
        # rewritten messages, so the index aligns with what pipeline sees.
        # ``_compute_frozen_message_count`` (above) is the sole owner of the
        # contract: only messages[i].content[*].cache_control bumps the
        # floor; system/tools are never passed here.
        frozen = _compute_frozen_message_count(messages)

        # Snapshot the CCR hashes the INPUT already carries (previous turns'
        # markers) so the post_compress event can report only the hashes THIS
        # compression surfaces. Hook-path-only cost: one scan, skipped
        # entirely when no hooks are installed.
        pre_existing_ccr_hashes: set[str] = _surfaced_ccr_hashes(messages) if hooks else set()

        # cache_control interactions must be LOUD but non-fatal. A breakpoint
        # on (nearly) the last message freezes everything — every transform
        # skips, 0 tokens saved, error=None — and a breakpoint moved forward
        # past a turn that previously shipped compressed guarantees a
        # prefix-cache miss at the very message the caller asserts is cached.
        # Neither is a failure (fail-open stays for real failures); both
        # surface in CompressResult.warnings and the log.
        compress_warnings: list[str] = []
        frozen_floor_warning = _frozen_prefix_warning(len(messages), frozen)
        if frozen_floor_warning is not None:
            compress_warnings.append(frozen_floor_warning)
        frozen_content_warning = _frozen_transformed_content_warning(messages, frozen)
        if frozen_content_warning is not None:
            compress_warnings.append(frozen_content_warning)
        # F-alpha4: an unrecognized model silently falls back to generic
        # character-estimation for token counting. Surface that fallback so a
        # caller is not misled by counts computed for a model Furl does not know.
        unknown_model_warning = _unknown_model_warning(model)
        if unknown_model_warning is not None:
            compress_warnings.append(unknown_model_warning)
        for warning in compress_warnings:
            logger.warning("%s", warning)

        result = pipeline.apply(
            messages=messages,
            model=model,
            model_limit=model_limit,
            context=context,
            biases=biases,
            # Pass CompressConfig options through to transforms
            compress_user_messages=cfg.compress_user_messages,
            compress_system_messages=cfg.compress_system_messages,
            protect_recent=cfg.protect_recent,
            protect_analysis_context=cfg.protect_analysis_context,
            min_tokens_to_compress=cfg.min_tokens_to_compress,
            frozen_message_count=frozen,
        )

        tokens_before = result.tokens_before
        tokens_after = result.tokens_after
        compressed_messages = result.messages

        # Guard: if "optimization" inflated tokens, revert to originals.
        # The inflation guard the compression path always applies before
        # returning a result.
        if tokens_after > tokens_before:
            logger.warning(
                "Optimization inflated tokens (%d -> %d); reverting to original messages",
                tokens_before,
                tokens_after,
            )
            # The inflation revert is a success-path outcome the documented
            # A/B-testing and anomaly-detection hook use-cases must see
            # (API-2): report the REVERTED state (nothing shipped compressed,
            # nothing newly surfaced).
            if hooks:
                from furl_ctx.hooks import CompressEvent

                hooks.post_compress(
                    CompressEvent(
                        tokens_before=tokens_before,
                        tokens_after=tokens_before,
                        tokens_saved=0,
                        compression_ratio=0.0,
                        transforms_applied=["inflation_guard:reverted"],
                        model=model,
                        user_query=context,
                    )
                )
            return CompressResult(
                messages=messages,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                tokens_saved=0,
                compression_ratio=0.0,
                transforms_applied=["inflation_guard:reverted"],
                warnings=[*compress_warnings, *result.warnings],
            )

        routing_markers = summarize_routing_markers(result.transforms_applied)
        if routing_markers:
            routed_event = pipeline_extensions.emit(
                PipelineStage.INPUT_ROUTED,
                operation="compress",
                model=model,
                messages=compressed_messages,
                metadata={
                    "routing_markers": routing_markers,
                    "transforms_applied": result.transforms_applied,
                },
            )
            if routed_event.messages is not None:
                compressed_messages = routed_event.messages

        compressed_event = pipeline_extensions.emit(
            PipelineStage.INPUT_COMPRESSED,
            operation="compress",
            model=model,
            messages=compressed_messages,
            metadata={
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "transforms_applied": result.transforms_applied,
            },
        )
        if compressed_event.messages is not None:
            compressed_messages = compressed_event.messages

        tokens_saved = tokens_before - tokens_after
        ratio = tokens_saved / tokens_before if tokens_before > 0 else 0.0

        # Post-compress hook — fires on EVERY success-path completion, zero
        # savings included, so subclasses see the negative class too (API-2;
        # subclasses that assumed savings>0 now also receive zero-events).
        # ``ccr_hashes`` carries the recovery pointers newly surfaced by this
        # compression. Fail-open failures (the except path below) do not
        # emit an event — no compression happened.
        if hooks:
            from furl_ctx.hooks import CompressEvent

            hooks.post_compress(
                CompressEvent(
                    tokens_before=tokens_before,
                    tokens_after=tokens_after,
                    tokens_saved=tokens_saved,
                    compression_ratio=ratio,
                    transforms_applied=result.transforms_applied,
                    ccr_hashes=_event_ccr_hashes(
                        result.markers_inserted,
                        pre_existing_ccr_hashes,
                        compressed_messages,
                    ),
                    model=model,
                    user_query=context,
                )
            )

        # T9: surface opaque whole-blob CCR offloads as a typed field the caller
        # reads at its own cadence, a marker replacing content that nothing could
        # structurally shrink whose retrieval round trip is net-negative. Runs
        # INSIDE the request-scoped CCR store binding, so a namespaced call reads
        # its own store. Guarded here as well as internally: this is an
        # observation-only diagnostic, so a failure computing it must never
        # revert a successful compression to the fail-open no-op path.
        try:
            opaque_offloads = _detect_opaque_offloads(compressed_messages, messages)
        except Exception:  # noqa: BLE001 - the diagnostic must never break a success
            logger.debug("opaque-offload detection failed (non-fatal)", exc_info=True)
            opaque_offloads = []

        return CompressResult(
            messages=compressed_messages,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tokens_saved=tokens_saved,
            compression_ratio=ratio,
            transforms_applied=result.transforms_applied,
            # Transform warnings (TransformResult.warnings) were previously
            # dropped here; plumb them through alongside the compress()-level
            # frozen-prefix diagnostics so callers can actually see them.
            warnings=[*compress_warnings, *result.warnings],
            opaque_offloads=opaque_offloads,
        )

    except (KeyboardInterrupt, SystemExit):
        # NEVER swallow these: a Ctrl-C or an interpreter shutdown during
        # compression must tear down exactly as the operator intended, not be
        # masked as a fail-open no-op. Re-raise before the BaseException catch
        # below can reach them.
        raise
    except BaseException as e:  # noqa: BLE001
        # Fail-open: a compression bug must NEVER break the host's request, so
        # we return the ORIGINAL messages and do not re-raise. This covers
        # pipeline CONSTRUCTION too (COR-43): a broken/missing native
        # extension fails open here, not as a ModuleNotFoundError to the
        # host. But the failure must be LOUD and HONEST — log at ERROR with a
        # full traceback (this may be a genuine bug or a Rust panic, not a
        # benign no-op) and report the real input token count instead of a
        # fabricated 0, so a caller cannot mistake a swallowed failure for
        # "nothing to compress".
        #
        # We catch BaseException (not just Exception) on purpose: a Rust panic
        # crosses the PyO3 FFI as ``pyo3_runtime.PanicException``, which is a
        # ``BaseException`` and would otherwise escape ``except Exception`` —
        # crashing the host request, the exact class this fail-open exists for.
        # The bridge methods also convert panics to ``PyRuntimeError`` at the
        # Rust edge (see crates/furl-py/src/lib.rs); this is the
        # belt-and-braces backstop for any entry point not wrapped there.
        logger.error(
            "compress() failed; returning original messages (fail-open): %s",
            e,
            exc_info=True,
        )
        # Count the untouched input the same way the pipeline does, but never
        # let token counting break fail-open: if even counting fails, fall back
        # to 0 rather than crash the caller.
        try:
            from furl_ctx.tokenizers import get_tokenizer

            tokens_before = get_tokenizer(model).count_messages(messages)
        except Exception:  # noqa: BLE001 - honest metrics are best-effort
            logger.error(
                "compress(): token counting also failed on the fail-open path; "
                "reporting tokens_before=0",
                exc_info=True,
            )
            tokens_before = 0
        return CompressResult(
            messages=messages,
            tokens_before=tokens_before,
            tokens_after=0,
            tokens_saved=0,
            compression_ratio=0.0,
            error=str(e),
        )
    finally:
        # Restore the prior CCR store (reset the token, do not clear) on BOTH
        # the success and fail-open paths, so a per-tenant binding never leaks
        # past this call and an outer middleware store is preserved.
        if _ccr_token is not None:
            _request_ccr_store.reset(_ccr_token)
        # Same discipline for the request-scoped originating tool name.
        if _tool_name_token is not None:
            _request_tool_name.reset(_tool_name_token)


def _get_pipeline() -> Any:
    """Get or create the singleton compression pipeline."""
    global _pipeline

    if _pipeline is not None:
        return _pipeline

    with _pipeline_lock:
        if _pipeline is not None:
            return _pipeline

        from furl_ctx.transforms import TransformPipeline

        # Default pipeline: CrossMessageDeduper → ContentRouter.
        # CacheAligner is opt-in (FurlConfig.cache_aligner.enabled,
        # default False): when enabled it runs first and only WARNS about
        # unstable prefixes — detector-only, never rewrites the prompt.
        # ContentRouter: routes to the right compressor per content type
        #   (SmartCrusher for JSON; log/search/diff compressors;
        #   plain text and source code pass through)
        # There is no trailing context-management stage —
        # live-zone-only compression never drops messages.
        _pipeline = TransformPipeline()
        logger.debug("Furl compression pipeline initialized")
        return _pipeline
