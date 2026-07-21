"""Anthropic content-block walker for the content router.

Extracted from ``content_router.py`` (§4.1 S4). Owns the block plane:
:meth:`ContentBlockWalker.process_content_blocks` (the per-block cache-safety
walk), :meth:`ContentBlockWalker._compress_content_block` (the shared
two-tier-cache compression of one ``tool_result``/``text`` block), and
:meth:`ContentBlockWalker._compress_nested_tool_result` (the nested
``content: [{"type": "text", ...}]`` shape, COR-47).

This is a TRUE leaf module: it never imports ``content_router`` at runtime
(annotation-only ``TYPE_CHECKING`` imports excepted), so there is no import
cycle, and it never receives the router. Dependencies are injected explicitly
— the same per-call-resolution idiom ``StrategyDispatcher`` uses, so
monkeypatching router methods (``router.compress`` above all: six suites fake
it and then drive ``apply()``) still bites:

* ``config`` rides the constructor — lifetime-stable; typed as the narrow
  :class:`MessagePolicyConfig` protocol (TYPE-3): the walker enforces the SAME
  three protection gates at block level that the Pass-1 chain enforces at
  message level, and reads exactly those fields.
* ``lookup_disposition`` / ``store_disposition`` — the router facade's cache
  gate (``_lookup_cached_disposition`` / ``_store_disposition``), passed to
  :meth:`process_content_blocks` per call. Every cache mutation and counter
  bump stays inside the gate; the walker only formats the label-threaded
  ``router:{label}:{strategy}`` transform strings.
* ``compress_fn`` — the router facade's ``compress`` (resolved fresh at the
  delegator call site, so an instance monkeypatch is honored).
* ``get_tool_bias`` / ``get_feedback_hints`` — router methods, per call.
* ``result_cache_key`` — the facade's COR-18 option-aware key builder.

``time`` is imported here for the per-block compressor timing; tests patch
attributes ON the shared ``time`` module object (never the binding), so the
patch reaches this module unchanged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .router_cache import CacheDisposition, CacheKey, Recompute, ServeCached, ServeOriginal
from .router_message_policy import (
    _is_retrieval_tool,
    _is_unstructured_error_output,
    _looks_like_ccr_output,
)

if TYPE_CHECKING:
    # Annotation-only: the walker passes compression results through to the
    # injected store gate and reads their ratio/strategy fields; the class
    # itself lives in content_router (which imports THIS module at runtime).
    from ..cache.retrieval_feedback import FeedbackHints
    from .content_router import ContentRouterConfig, RouterCompressionResult


# Injected-callable shapes (keyword-arg precision intentionally elided —
# ``Callable[...]`` over single-implementer Protocols): the router facade's
# ``compress`` / ``_lookup_cached_disposition`` / ``_store_disposition``.
BlockCompressFn = Callable[..., "RouterCompressionResult"]
LookupDispositionFn = Callable[..., CacheDisposition]
StoreDispositionFn = Callable[..., bool]


class ContentBlockWalker:
    """Walks Anthropic-format content blocks and compresses the safe ones.

    Holds no reference to the :class:`ContentRouter`. Constructed once with
    the router's ``config``; the cache gate, the compression entry point, and
    the per-tool policy callables are supplied to
    :meth:`process_content_blocks` on each call.
    """

    def __init__(self, config: ContentRouterConfig) -> None:
        self.config = config

    def process_content_blocks(
        self,
        message: dict[str, Any],
        content_blocks: list[Any],
        context: str,
        transforms_applied: list[str],
        excluded_tool_ids: set[str],
        *,
        tool_name_map: dict[str, str] | None,
        route_counts: dict[str, int] | None,
        compressed_details: list[str] | None,
        min_ratio: float,
        read_protection_window: int,
        messages_from_end: int,
        compressor_timing: dict[str, float] | None,
        min_chars: int,
        skip_user: bool,
        skip_system: bool,
        compress_assistant_text_blocks: bool,
        token_counter: Callable[[str], int] | None,
        lookup_disposition: LookupDispositionFn,
        store_disposition: StoreDispositionFn,
        compress_fn: BlockCompressFn,
        get_tool_bias: Callable[[str], float],
        get_feedback_hints: Callable[..., FeedbackHints],
        result_cache_key: Callable[[str, float], CacheKey],
    ) -> dict[str, Any]:
        """Process content blocks (Anthropic format) for compression.

        Cache-safety contract:
          1. Any block carrying `cache_control` is the client's explicit
             cache breakpoint. Modifying any byte of such a block changes
             the cache key the upstream provider matches against, turning
             a 90% read discount into a 25% write penalty (Anthropic).
             We never modify cache_control'd blocks, regardless of role
             or block type.
          2. Assistant text blocks are echoed back by the client in
             subsequent turns and become part of the upstream provider's
             auto-prefix cache (DeepSeek, OpenAI). Default-skip; opt in
             via `compress_assistant_text_blocks` when the deployment
             knows the backend doesn't honor cache_control AND
             compression is byte-deterministic.
          3. User and system blocks carry the prompt the model is acting
             on; compressing them silently mutates the request. Always
             skipped per `skip_user` / `skip_system`.
          4. Tool / function blocks are tool outputs — semantically safe
             to compress (the model references them once, then moves on).

        Args:
            message: The original message.
            content_blocks: List of content blocks.
            context: Context for compression.
            transforms_applied: List to append transform names to.
            excluded_tool_ids: Tool IDs to skip compression for.
            tool_name_map: Mapping from tool_call_id to tool_name for profile lookup.
            route_counts: Optional routing reason counters to update.
            compressed_details: Optional list to append compression details to.
            min_ratio: Adaptive compression ratio threshold.
            read_protection_window: Messages from end within which excluded tools are protected.
            messages_from_end: How far this message is from the end of the conversation.
            compressor_timing: Optional per-strategy cumulative timing sink.
            min_chars: Minimum block content length (chars) to consider for compression.
            skip_user: If True, never compress text blocks in user-role messages.
            skip_system: If True, never compress text blocks in system-role messages.
            compress_assistant_text_blocks: If True, allow compressing text blocks in
                assistant-role messages. Default False (cache-safe).
            token_counter: Optional real token counter threaded into
                ``compress_fn``; ``None`` keeps word counts.
            lookup_disposition: The router's two-tier cache lookup gate.
            store_disposition: The router's two-tier cache store gate.
            compress_fn: The router's ``compress`` (per-call resolution).
            get_tool_bias: Per-tool compression bias lookup.
            get_feedback_hints: Retrieval-feedback hints lookup (P2-13).
            result_cache_key: The COR-18 option-aware cache key builder.

        Returns:
            Transformed message with compressed content blocks.
        """
        new_blocks = []
        any_compressed = False
        role = message.get("role", "")

        # Role-based gate for `text` blocks. Tool/function roles are tool
        # outputs and compress freely; assistant defaults to skip (cache
        # safety) with explicit opt-in; unknown roles default to skip.
        if (skip_user and role == "user") or (skip_system and role in {"system", "developer"}):
            protect_text_blocks = True
        elif role == "assistant" and not compress_assistant_text_blocks:
            protect_text_blocks = True
        elif role not in ("assistant", "tool", "function"):
            protect_text_blocks = True
        else:
            protect_text_blocks = False

        for block in content_blocks:
            if not isinstance(block, dict):
                new_blocks.append(block)
                continue

            # Defense in depth: cache_control marker is the client's
            # cache breakpoint. Frozen-message-count is a coarse
            # message-level approximation; this is the per-block
            # guarantee that we never bust an explicit cache key.
            if "cache_control" in block:
                new_blocks.append(block)
                if route_counts is not None:
                    route_counts.setdefault("cache_control_protected", 0)
                    route_counts["cache_control_protected"] += 1
                continue

            block_type = block.get("type")

            # Handle tool_result blocks
            if block_type == "tool_result":
                # Check if tool is excluded from compression
                tool_use_id = block.get("tool_use_id", "")
                if tool_use_id in excluded_tool_ids:
                    if messages_from_end <= read_protection_window or _is_retrieval_tool(
                        (tool_name_map or {}).get(tool_use_id, "")
                    ):
                        # Recent — protect as before. Retrieval-tool outputs
                        # never age out (retrieval-loop guard).
                        new_blocks.append(block)
                        transforms_applied.append("router:excluded:tool")
                        if route_counts is not None:
                            route_counts["excluded_tool"] += 1
                        continue
                    # Old excluded-tool output — fall through to compression

                # Look up tool-specific compression bias
                tool_name = (tool_name_map or {}).get(tool_use_id, "")
                bias = get_tool_bias(tool_name) if tool_name else 1.0

                tool_content = block.get("content", "")

                # Protection: failed tool calls / error outputs stay verbatim.
                # `is_error` is Anthropic's explicit failure
                # flag and suffices alone; the indicator scan catches error
                # text without the flag but requires >=2 distinct keywords
                # AND an unstructured (non-JSON) shape, so benign row data
                # mentioning errors doesn't skip compression.
                # Above the size cap, fall through — LogCompressor preserves
                # error lines in big logs.
                if (
                    self.config.protect_error_outputs
                    and isinstance(tool_content, str)
                    and len(tool_content) <= self.config.error_protection_max_chars
                    and (
                        block.get("is_error") is True or _is_unstructured_error_output(tool_content)
                    )
                ):
                    new_blocks.append(block)
                    transforms_applied.append("router:protected:error_output")
                    if route_counts is not None:
                        route_counts.setdefault("error_protected", 0)
                        route_counts["error_protected"] += 1
                    continue

                # String content: the flat tool_result payload shape.
                if isinstance(tool_content, str) and len(tool_content) > min_chars:
                    # Compression pinning: skip already-compressed content
                    # (strict marker grammar — see _looks_like_ccr_output).
                    if _looks_like_ccr_output(tool_content):
                        new_blocks.append(block)
                        if route_counts is not None:
                            route_counts.setdefault("already_compressed", 0)
                            route_counts["already_compressed"] += 1
                        continue

                    # Retrieval-feedback protection (Engine P2-13, opt-in) —
                    # mirror of the string path. The helper runs detection
                    # itself (this frame never detected); flag off never
                    # reaches it.
                    if self.config.enable_retrieval_feedback:
                        hints = get_feedback_hints(tool_name, tool_content)
                        if hints.skip_compression:
                            new_blocks.append(block)
                            transforms_applied.append("router:feedback:skip")
                            if route_counts is not None:
                                route_counts.setdefault("feedback_skip", 0)
                                route_counts["feedback_skip"] += 1
                            continue
                        if hints.keep_budget_multiplier != 1.0:
                            bias = bias * hints.keep_budget_multiplier

                    new_block, did = self._compress_content_block(
                        block,
                        tool_content,
                        block_key="content",
                        label="tool_result",
                        detail_prefix="tool",
                        context=context,
                        min_ratio=min_ratio,
                        bias=bias,
                        transforms_applied=transforms_applied,
                        route_counts=route_counts,
                        compressed_details=compressed_details,
                        compressor_timing=compressor_timing,
                        token_counter=token_counter,
                        lookup_disposition=lookup_disposition,
                        store_disposition=store_disposition,
                        compress_fn=compress_fn,
                        result_cache_key=result_cache_key,
                    )
                    new_blocks.append(new_block)
                    any_compressed = any_compressed or did
                    continue
                elif isinstance(tool_content, list):
                    # Nested parts list — the canonical Anthropic/MCP
                    # ``content: [{"type":"text","text": …}]`` shape (COR-47).
                    # Route each inner text part through the same two-tier
                    # cache; non-text parts (images, …) ship untouched.
                    # Previously this shape was booked as route_counts["small"]
                    # and never compressed.
                    new_block, did = self._compress_nested_tool_result(
                        block,
                        tool_content,
                        context=context,
                        min_ratio=min_ratio,
                        bias=bias,
                        transforms_applied=transforms_applied,
                        route_counts=route_counts,
                        compressed_details=compressed_details,
                        compressor_timing=compressor_timing,
                        min_chars=min_chars,
                        tool_name=tool_name,
                        token_counter=token_counter,
                        lookup_disposition=lookup_disposition,
                        store_disposition=store_disposition,
                        compress_fn=compress_fn,
                        get_feedback_hints=get_feedback_hints,
                        result_cache_key=result_cache_key,
                    )
                    new_blocks.append(new_block)
                    any_compressed = any_compressed or did
                    continue
                else:
                    if route_counts is not None:
                        route_counts["small"] += 1

            # Handle text blocks — compress for non-Anthropic clients (e.g.
            # OpenAI/DeepSeek via Cline) whose SDK normalizes content to
            # block-list form. Roles are gated above (user/system always
            # skipped; assistant default-skipped, opt-in via
            # `compress_assistant_text_blocks`).
            elif block_type == "text" and not protect_text_blocks:
                text_content = block.get("text", "")
                if isinstance(text_content, str) and len(text_content) > min_chars:
                    # Pinning: skip already-compressed content. The loose
                    # phrase substrings are kept for back-compat;
                    # _looks_like_ccr_output additionally pins engine-emitted
                    # ``<<ccr:HASH>>`` sentinels, which the smart-crusher path
                    # emits WITHOUT either phrase at default config (COR-31) —
                    # phrase-only pinning re-compressed those after result-cache
                    # expiry, and sentinel survival through a second crush is
                    # not contractual.
                    if (
                        "Retrieve more: hash=" in text_content
                        or "Retrieve original: hash=" in text_content
                        or _looks_like_ccr_output(text_content)
                    ):
                        new_blocks.append(block)
                        if route_counts is not None:
                            route_counts.setdefault("already_compressed", 0)
                            route_counts["already_compressed"] += 1
                        continue

                    new_block, did = self._compress_content_block(
                        block,
                        text_content,
                        block_key="text",
                        label="text_block",
                        detail_prefix="text",
                        context=context,
                        min_ratio=min_ratio,
                        bias=1.0,
                        transforms_applied=transforms_applied,
                        route_counts=route_counts,
                        compressed_details=compressed_details,
                        compressor_timing=compressor_timing,
                        token_counter=token_counter,
                        lookup_disposition=lookup_disposition,
                        store_disposition=store_disposition,
                        compress_fn=compress_fn,
                        result_cache_key=result_cache_key,
                    )
                    new_blocks.append(new_block)
                    any_compressed = any_compressed or did
                    continue
                else:
                    if route_counts is not None:
                        route_counts["small"] += 1

            # Keep block unchanged
            new_blocks.append(block)

        if any_compressed:
            return {**message, "content": new_blocks}
        return message

    def _compress_content_block(
        self,
        block: dict[str, Any],
        text: str,
        *,
        block_key: str,
        label: str,
        detail_prefix: str,
        context: str,
        min_ratio: float,
        bias: float,
        transforms_applied: list[str],
        route_counts: dict[str, int] | None,
        compressed_details: list[str] | None,
        compressor_timing: dict[str, float] | None,
        token_counter: Callable[[str], int] | None,
        lookup_disposition: LookupDispositionFn,
        store_disposition: StoreDispositionFn,
        compress_fn: BlockCompressFn,
        result_cache_key: Callable[[str, float], CacheKey],
    ) -> tuple[dict[str, Any], bool]:
        """Compress one cacheable content block (Anthropic ``tool_result`` or
        ``text``). Returns ``(new_block, did_compress)``.

        Single source for the two-tier-cache + CCR-backing + ratio-gate logic
        that the ``tool_result`` and ``text`` branches of
        ``process_content_blocks`` ran near-identically — they differ only in the
        block payload key (``content`` vs ``text``) and the log labels, threaded in
        as ``block_key`` / ``label`` / ``detail_prefix``. The caller has already
        done role-gating, error protection, already-compressed pinning, and the
        ``min_chars`` check before calling this.
        """
        content_key = result_cache_key(text, bias)
        match lookup_disposition(content_key, context, min_ratio, route_counts):
            case ServeOriginal():
                return block, False
            case ServeCached(compressed=served, strategy=strategy, ratio=ratio):
                transforms_applied.append(f"router:{label}:{strategy}")
                if compressed_details is not None:
                    compressed_details.append(f"{detail_prefix}:{strategy}:{ratio:.2f}")
                return {**block, block_key: served}, True
            case Recompute():
                pass  # fall through to full compression below
            case other:
                raise RuntimeError(
                    f"_lookup_cached_disposition returned unexpected CacheDisposition {other!r}"
                )

        # Recompute (cache miss or evicted stale sentinel). All cache bookkeeping
        # — skip/stale/miss counters and any eviction — already happened inside
        # the lookup gate; here we only (re)compress and store.
        t0 = time.perf_counter()
        result = compress_fn(text, context=context, bias=bias, token_counter=token_counter)
        compress_ms = (time.perf_counter() - t0) * 1000
        if compressor_timing is not None:
            key = f"compressor:{result.strategy_used.value}"
            compressor_timing[key] = compressor_timing.get(key, 0.0) + compress_ms
        if store_disposition(content_key, result, min_ratio, route_counts):
            transforms_applied.append(f"router:{label}:{result.strategy_used.value}")
            if compressed_details is not None:
                compressed_details.append(
                    f"{detail_prefix}:{result.strategy_used.value}:{result.compression_ratio:.2f}"
                )
            return {**block, block_key: result.compressed}, True
        # Didn't compress — key is in the skip set; serve the original block.
        return block, False

    def _compress_nested_tool_result(
        self,
        block: dict[str, Any],
        parts: list[Any],
        *,
        context: str,
        min_ratio: float,
        bias: float,
        transforms_applied: list[str],
        route_counts: dict[str, int] | None,
        compressed_details: list[str] | None,
        compressor_timing: dict[str, float] | None,
        min_chars: int,
        tool_name: str,
        token_counter: Callable[[str], int] | None,
        lookup_disposition: LookupDispositionFn,
        store_disposition: StoreDispositionFn,
        compress_fn: BlockCompressFn,
        get_feedback_hints: Callable[..., FeedbackHints],
        result_cache_key: Callable[[str, float], CacheKey],
    ) -> tuple[dict[str, Any], bool]:
        """Compress the inner ``type=="text"`` parts of a nested ``tool_result``
        (COR-47). Returns ``(new_block, did_compress)``.

        The canonical Anthropic/MCP tool_result shape carries its payload as
        ``content: [{"type": "text", "text": …}]``. Each qualifying inner text
        part routes through :meth:`_compress_content_block` with
        ``block_key="text"`` — the SAME two-tier cache the flat shapes use, so
        identical payloads share entries across shapes. Every part-level
        protection mirrors the string-content branch: the block-level
        ``is_error`` flag protects the whole block; per part, the error
        indicator scan, the ``min_chars`` floor, the already-compressed
        pinning, and a ``cache_control`` guard all apply. Non-text parts
        (images, …) always ship untouched. The caller has already run the
        block-level exclusion / cache_control gates.
        """

        def bump(*keys: str) -> None:
            if route_counts is not None:
                for k in keys:
                    route_counts[k] = route_counts.get(k, 0) + 1

        bump("nested_blocks")

        # Anthropic's explicit failure flag protects the whole block — the
        # string-content branch checks it before reaching compression; the
        # nested shape must not lose that protection.
        if self.config.protect_error_outputs and block.get("is_error") is True:
            transforms_applied.append("router:protected:error_output")
            bump("error_protected")
            return block, False

        new_parts: list[Any] = []
        any_did = False
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "text" or "cache_control" in part:
                new_parts.append(part)
                continue
            part_text = part.get("text", "")
            if not isinstance(part_text, str) or len(part_text) <= min_chars:
                new_parts.append(part)
                continue
            # Error indicator scan (mirror of the string-content branch).
            if (
                self.config.protect_error_outputs
                and len(part_text) <= self.config.error_protection_max_chars
                and _is_unstructured_error_output(part_text)
            ):
                new_parts.append(part)
                transforms_applied.append("router:protected:error_output")
                bump("error_protected")
                continue
            # Compression pinning (strict marker grammar).
            if _looks_like_ccr_output(part_text):
                new_parts.append(part)
                bump("already_compressed")
                continue
            # Retrieval-feedback protection (Engine P2-13, opt-in) — per
            # PART: each text part is its own compression unit with its own
            # detected shape. Flag off never consults.
            part_bias = bias
            if self.config.enable_retrieval_feedback:
                hints = get_feedback_hints(tool_name, part_text)
                if hints.skip_compression:
                    new_parts.append(part)
                    transforms_applied.append("router:feedback:skip")
                    bump("feedback_skip")
                    continue
                if hints.keep_budget_multiplier != 1.0:
                    part_bias = bias * hints.keep_budget_multiplier
            new_part, did = self._compress_content_block(
                part,
                part_text,
                block_key="text",
                label="tool_result",
                detail_prefix="tool",
                context=context,
                min_ratio=min_ratio,
                bias=part_bias,
                transforms_applied=transforms_applied,
                route_counts=route_counts,
                compressed_details=compressed_details,
                compressor_timing=compressor_timing,
                token_counter=token_counter,
                lookup_disposition=lookup_disposition,
                store_disposition=store_disposition,
                compress_fn=compress_fn,
                result_cache_key=result_cache_key,
            )
            new_parts.append(new_part)
            any_did = any_did or did

        if any_did:
            return {**block, "content": new_parts}, True
        return block, False
