"""Message-level routing policy for the content router (Pass-1 gate chain).

Extracted from ``content_router.py`` (§4.1 S3). Owns WHAT happens to one
message — the :class:`MessageDisposition` ADT and :func:`classify_message`,
the verbatim move of ``apply()``'s Pass-1 protection-gate chain (minus the
cache lookup, which stays on the router facade) — plus the message-shape
helpers ``build_tool_name_map`` / ``get_tool_bias`` / ``detect_analysis_intent``
and the classification predicates they lean on.

GATE-CHAIN ORDER IS BEHAVIOR: frozen prefix → content-block dispatch (with its
own excluded-tool window) → non-string → excluded-tool window → user-skip →
system-skip → size floor → error protection → already-compressed pinning →
detection (lazy) → recent-code → analysis-context → retrieval-feedback → bias
merge. PERF-2 hoisted the pinning gate above detection (a pinned message ships
verbatim either way — paying the detect round-trip for it bought nothing) and
made detection LAZY: it runs only when a gate that consumes it is live (the
recent-code window covers this message, analysis protection is armed, or the
retrieval-feedback lane needs the content-type tag). The route_counts
whole-dict matrix in ``test_router_decomposition_pins.py`` pins the observable
outcome per lane.

This is a TRUE leaf module: it never imports ``content_router`` (no cycle).
Everything is a pure function of its arguments; the impure edges come in as
injected callables, resolved by the facade per call so the test suite's
monkeypatches keep biting (the same per-call idiom as ``StrategyDispatcher``):

* ``count_text`` — the request tokenizer's counter (size floor unit);
* ``detect_content`` — the facade passes its module-global ``_detect_content``
  so ``monkeypatch.setattr(content_router_module, "_detect_content", ...)``
  stays effective;
* ``get_tool_bias`` / ``get_feedback_hints`` — router methods, resolved fresh;
* ``result_cache_key`` — the facade's COR-18 option-aware key builder.

The config parameter is typed as the narrow :class:`MessagePolicyConfig`
protocol of exactly the fields the gate chain reads (TYPE-3);
``ContentRouterConfig`` satisfies it structurally.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..ccr import marker_grammar
from ..ccr.marker_grammar import CCR_TOOL_NAME
from ..config import is_tool_excluded
from ..utils import concat_text_parts
from .content_detector import ContentType, DetectionResult
from .error_detection import content_has_strong_error_indicators
from .router_cache import CacheKey

# Engine-emitted retrieval hints always carry a real 12/24-hex hash; content
# that merely talks about the grammar (docs, this repo's own source read back
# as tool output) uses placeholders and never matches.
_RETRIEVE_HINT_PATTERN = re.compile(
    r"Retrieve (?:more|original): hash=[0-9a-fA-F]{12}(?:[0-9a-fA-F]{12})?(?![0-9a-fA-F])"
)

# Genuine analysis verbs (and analysis-question phrases) only, matched on
# word boundaries (COR-16). The previous substring scan over a much broader
# set — fix/error/bug/issue/problem/wrong/broken/improve/"clean up"/
# security/vulnerability — tripped on virtually every coding-agent message
# ("fix" matched *prefix*, "error" matched any mention), so analysis_intent
# was ~always true and SOURCE_CODE was ~never compressed (protection 3).
# refactor/optimize stay: precise code-work verbs with low ambient
# frequency whose requests need full code fidelity.
_ANALYSIS_INTENT_KEYWORDS: tuple[str, ...] = (
    "analyze",
    "analyse",
    "audit",
    "debug",
    "explain",
    "how does",
    "inspect",
    "optimize",
    "refactor",
    "review",
    "understand",
    "what does",
)

_ANALYSIS_INTENT_PATTERN = re.compile(
    r"\b(?:"
    + "|".join(
        r"\s+".join(map(re.escape, keyword.split())) for keyword in _ANALYSIS_INTENT_KEYWORDS
    )
    + r")\b"
)

# Roles whose message content is a tool output. OpenAI's current API uses
# ``tool``; the legacy function-calling API uses ``function`` (name carried on
# ``message["name"]``, no tool_call_id). Mirrors cross_message_dedup's
# eligibility set so the router's protection gates (excluded tools, error
# outputs, tool bias) fire for BOTH shapes (COR-48).
_TOOL_ROLES = frozenset({"tool", "function"})

# Retrieval-loop guard (P0-5): the CCR retrieval tool's outputs ARE the
# originals the engine previously compressed. Compressing them again would
# mint a fresh retrieval marker for content the model just asked to see —
# a compress → retrieve → compress ping-pong. These names are excluded
# UNCONDITIONALLY: unioned into every effective exclusion set (even a
# caller-supplied ``exclude_tools`` override, including ``set()``) and
# immune to the read-protection-window age decay. Covers both retrieval
# channels: direct tool injection (``furl_retrieve``) and the MCP server
# under any server alias (``mcp__<server>__furl_retrieve``).
ALWAYS_EXCLUDE_TOOLS: frozenset[str] = frozenset(
    {
        CCR_TOOL_NAME,
        f"mcp__*__{CCR_TOOL_NAME}",
    }
)


class MessagePolicyConfig(Protocol):
    """The config fields the Pass-1 gate chain reads (a structural subset of
    ``ContentRouterConfig``)."""

    protect_error_outputs: bool
    error_protection_max_chars: int
    enable_retrieval_feedback: bool


class FeedbackHintsLike(Protocol):
    """The two retrieval-feedback fields the gate chain reads (structural —
    ``furl_ctx.cache.retrieval_feedback.FeedbackHints``, a FROZEN dataclass,
    satisfies it without this leaf module importing the cache package;
    read-only properties so frozen attributes type-check)."""

    @property
    def skip_compression(self) -> bool: ...

    @property
    def keep_budget_multiplier(self) -> float: ...


def _is_retrieval_tool(tool_name: str) -> bool:
    """True iff *tool_name* is the CCR retrieval tool on any channel."""
    return is_tool_excluded(tool_name, ALWAYS_EXCLUDE_TOOLS)


def _is_unstructured_error_output(content: str) -> bool:
    """True when *content* is a raw error dump that must ship verbatim.

    Structured JSON is never a traceback: rows that merely mention errors
    (git logs full of ``fix:`` subjects) route to SmartCrusher, whose
    error-keyword preservation keeps genuine error rows visible.
    """
    if not content_has_strong_error_indicators(content):
        return False
    if content.lstrip()[:1] in ("[", "{"):
        try:
            json.loads(content)
            return False
        except (json.JSONDecodeError, ValueError):
            pass
    return True


def _looks_like_ccr_output(content: str) -> bool:
    """True when *content* carries a real engine-emitted CCR marker (strict
    grammar — content that merely mentions the marker text stays
    compressible)."""
    return bool(
        _RETRIEVE_HINT_PATTERN.search(content)
        or marker_grammar.DOUBLE_ANGLE_PATTERN.search(content)
    )


# ---------------------------------------------------------------------------
# MessageDisposition ADT.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Frozen:
    """Inside the provider's prefix cache (``frozen_message_count``): ship
    byte-identical; book NOTHING — not even a counter."""


@dataclass(frozen=True)
class ProtectedMsg:
    """A protection gate fired: ship the original and book exactly one
    transform string + one route counter. The transform string IS the reason
    (``router:excluded:tool`` / ``router:protected:{...}`` /
    ``router:feedback:skip``) — carried as data so the facade reproduces the
    historical strings byte-for-byte, including the role-dependent
    ``router:protected:{role}_message`` form."""

    transform: str
    counter: str


@dataclass(frozen=True)
class Small:
    """Empty content or under the per-request min-token floor: ship original,
    book ``small``."""


@dataclass(frozen=True)
class NonString:
    """Content is neither ``str`` nor ``list`` (unknown payload shape): ship
    original, book ``non_string``."""


@dataclass(frozen=True)
class ContentBlocks:
    """Anthropic-format content-block list: the caller walks the blocks
    (``ContentBlockWalker`` plane) and books ``content_blocks``."""


@dataclass(frozen=True)
class AlreadyCompressed:
    """Carries a real engine-emitted CCR marker — compression pinning: ship
    original (recompressing would break prefix caching), book
    ``already_compressed``."""


@dataclass(frozen=True)
class Compressible:
    """Every gate passed: proceed to the two-tier cache with the merged
    per-message *bias* (tool profile × hook × retrieval-feedback) and the
    option-aware *content_key* built from it (COR-18).

    ``detection`` carries the classify-time content detection when a live
    gate already paid for it (PERF-2c) — ``None`` when every detection
    consumer was inert. The facade threads it into ``compress()`` so the
    engine never re-runs the Rust detect round-trip on the same bytes.
    """

    bias: float
    content_key: CacheKey
    detection: DetectionResult | None = None


# The gate chain resolves every message to exactly one of seven dispositions.
# The five empty variants carry no data, so they are shared module singletons:
# Pass-1 resolves one per message and must not allocate for the common lanes.
# ``ProtectedMsg`` / ``Compressible`` hold per-message payload and stay fresh.
MessageDisposition = (
    Frozen | ProtectedMsg | Small | NonString | ContentBlocks | AlreadyCompressed | Compressible
)
_FROZEN = Frozen()
_SMALL = Small()
_NON_STRING = NonString()
_CONTENT_BLOCKS = ContentBlocks()
_ALREADY_COMPRESSED = AlreadyCompressed()


def classify_message(
    message: dict[str, Any],
    *,
    index: int,
    num_messages: int,
    frozen_message_count: int,
    tool_name_map: dict[str, str],
    excluded_tool_ids: set[str],
    exclude_tools: frozenset[str],
    read_protection_window: int,
    skip_user: bool,
    skip_system: bool,
    min_tokens: int,
    protect_recent: int,
    protect_analysis: bool,
    analysis_intent: bool,
    hook_biases: dict[int, float],
    config: MessagePolicyConfig,
    count_text: Callable[[str], int],
    detect_content: Callable[[str], DetectionResult],
    get_tool_bias: Callable[[str], float],
    get_feedback_hints: Callable[..., FeedbackHintsLike],
    result_cache_key: Callable[[str, float], CacheKey],
) -> MessageDisposition:
    """Resolve one message to its :class:`MessageDisposition` — the Pass-1
    gate chain. Returns WHAT to do; the facade owns the HOW (slot
    assignment, transform bookkeeping, the cache gate, and Pass-2 deferral).

    Effects are injection-only: this function touches no router state and no
    module singletons. ``detect_content`` (a Rust FFI round-trip) is LAZY
    (PERF-2): it runs after the size/error/pinning gates, at most once, and
    only when a live gate consumes the result — the recent-code window
    covers this message, analysis protection is armed, or the
    retrieval-feedback lane needs the content-type tag. When it ran, the
    result rides out on ``Compressible.detection`` so the engine can skip
    its own detect (PERF-2c).
    """
    # Skip frozen messages (in provider's prefix cache).
    # Modifying these would invalidate the cache, replacing a 90%
    # read discount with a 25% write penalty (Anthropic).
    if index < frozen_message_count:
        return _FROZEN

    role = message.get("role", "")
    content = message.get("content", "")
    bias = 1.0  # Default bias, may be overridden for tool messages
    msg_tool_name = ""  # Resolved for tool/function messages below

    messages_from_end = num_messages - index

    # Handle list content (Anthropic format with content blocks)
    if isinstance(content, list):
        # Message-level exclusion for OpenAI-style tool/function
        # messages whose content is a parts LIST (COR-48): the block
        # walker checks exclusion only via block-level
        # ``tool_use_id``, which this shape doesn't carry — without
        # this gate, excluded-tool protection vanished for it.
        # Honors the same age-based decay as the string path.
        if role in _TOOL_ROLES:
            tool_call_id = message.get("tool_call_id", "")
            tool_name = tool_name_map.get(tool_call_id, "") or str(message.get("name", "") or "")
            if (
                tool_call_id in excluded_tool_ids
                or (tool_name and is_tool_excluded(tool_name, exclude_tools))
            ) and (
                messages_from_end <= read_protection_window
                # Retrieval-loop guard: retrieval outputs never age
                # out of protection (recompression re-opens the loop).
                or _is_retrieval_tool(tool_name)
            ):
                return ProtectedMsg("router:excluded:tool", "excluded_tool")
        return _CONTENT_BLOCKS

    # Skip non-string content (other types)
    if not isinstance(content, str):
        return _NON_STRING

    # Skip OpenAI-style tool/function messages for excluded tools
    # BUT: allow compression of old excluded-tool outputs beyond the
    # adaptive protection window (age-based decay). Legacy
    # function-role messages carry no tool_call_id — their name rides
    # on ``message["name"]`` (COR-48), which also backstops tool-role
    # messages whose call id was never mapped.
    if role in _TOOL_ROLES:
        tool_call_id = message.get("tool_call_id", "")
        tool_name = tool_name_map.get(tool_call_id, "") or str(message.get("name", "") or "")
        msg_tool_name = tool_name
        if tool_call_id in excluded_tool_ids or (
            tool_name and is_tool_excluded(tool_name, exclude_tools)
        ):
            if messages_from_end <= read_protection_window or _is_retrieval_tool(tool_name):
                # Recent — protect as before. Retrieval-tool outputs
                # are protected regardless of age: recompressing them
                # re-opens the compress→retrieve→compress loop.
                return ProtectedMsg("router:excluded:tool", "excluded_tool")
            # Old excluded-tool output — fall through to compression
            # (the LLM is unlikely to need exact content from this far back,
            # and CCR provides retrieval if it does)
        # Look up tool-specific compression bias for tool/function messages
        bias = get_tool_bias(tool_name) if tool_name else 1.0

    # Protection 1: Never compress user messages (unless overridden)
    if skip_user and role == "user":
        return ProtectedMsg("router:protected:user_message", "user_msg")

    # Protection 1b: Never compress system/developer messages unless
    # explicitly opted in. These are cache-hot instruction bytes.
    if skip_system and role in {"system", "developer"}:
        return ProtectedMsg(f"router:protected:{role}_message", "system_msg")

    if not content or count_text(content) < min_tokens:
        # Skip small content
        return _SMALL

    # Protection: failed tool calls / error outputs stay verbatim.
    # The model needs exact tracebacks to recover.
    # Strong (>=2 distinct indicators) match only — a single
    # keyword false-positives on benign outputs that mention
    # errors. Above the size cap, fall through — LogCompressor
    # preserves error lines in big logs.
    if (
        config.protect_error_outputs
        and role in _TOOL_ROLES
        and len(content) <= config.error_protection_max_chars
        and _is_unstructured_error_output(content)
    ):
        return ProtectedMsg("router:protected:error_output", "error_protected")

    # Compression pinning: if this message was already compressed
    # (carries a real engine-emitted CCR retrieval marker), skip
    # recompression. Recompressing would change byte content and
    # break provider prefix caching with no meaningful further
    # reduction. Strict grammar match — raw content that merely
    # MENTIONS the marker text (docs, or this engine's own source
    # read back as a tool output) is not pinned and stays
    # compressible. Checked BEFORE detection (PERF-2a): a pinned
    # message ships verbatim regardless of its content type, so the
    # detect round-trip for it bought nothing.
    if _looks_like_ccr_output(content):
        return _ALREADY_COMPRESSED

    # Detect content type for protection decisions — LAZY (PERF-2b):
    # the Rust FFI round-trip runs only when a live gate consumes the
    # result. With the recent-code window not covering this message,
    # analysis protection unarmed, AND the feedback lane inert, no gate
    # reads it — the engine detects once inside compress() instead.
    detection: DetectionResult | None = None
    recent_code_live = protect_recent > 0 and messages_from_end <= protect_recent
    analysis_live = protect_analysis and analysis_intent
    if recent_code_live or analysis_live:
        detection = detect_content(content)
        is_code = detection.content_type == ContentType.SOURCE_CODE

        # Protection 2: Don't compress recent CODE
        if recent_code_live and is_code:
            return ProtectedMsg("router:protected:recent_code", "recent_code")

        # Protection 3: Don't compress CODE when analysis intent detected
        if analysis_live and is_code:
            return ProtectedMsg("router:protected:analysis_context", "analysis_ctx")

    # Retrieval-feedback protection (Engine P2-13, opt-in): a shape
    # the model keeps retrieving from the CCR store gets gentler
    # routing — skip compression entirely at sustained pressure, or
    # fold a keep-more multiplier into the bias below. Flag off
    # (default) never consults the aggregator: byte-identical routing.
    feedback_multiplier = 1.0
    if config.enable_retrieval_feedback and role in _TOOL_ROLES:
        if detection is None:
            detection = detect_content(content)
        hints = get_feedback_hints(
            msg_tool_name, content, content_type_tag=detection.content_type.value
        )
        if hints.skip_compression:
            return ProtectedMsg("router:feedback:skip", "feedback_skip")
        feedback_multiplier = hints.keep_budget_multiplier

    # Route and compress based on content detection
    # Merge tool-specific bias with hook-provided bias (multiplicative)
    msg_bias = bias if role in _TOOL_ROLES else 1.0
    if index in hook_biases:
        msg_bias *= hook_biases[index]
    if feedback_multiplier != 1.0:
        msg_bias *= feedback_multiplier

    # The key carries the per-request bias and a length guard — see
    # ``_result_cache_key`` (COR-18). Any detection a live gate already
    # paid for rides along so compress() never re-detects (PERF-2c).
    return Compressible(
        bias=msg_bias,
        content_key=result_cache_key(content, msg_bias),
        detection=detection,
    )


# ---------------------------------------------------------------------------
# Message-shape helpers (pure functions of their arguments).
# ---------------------------------------------------------------------------


def build_tool_name_map(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Build mapping from tool_call_id to tool_name.

    Scans assistant messages to find tool calls and extract their names.
    Supports both OpenAI and Anthropic message formats.
    """
    mapping: dict[str, str] = {}

    for msg in messages:
        if msg.get("role") != "assistant":
            continue

        # OpenAI format: tool_calls array. `or []`: the key is
        # present-but-None in openai-python model_dump() output —
        # iterating None would TypeError and fail-open every request.
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                tc_id = tc.get("id", "")
                name = tc.get("function", {}).get("name", "")
                if tc_id and name:
                    mapping[tc_id] = name

        # Anthropic format: content blocks with type=tool_use
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tc_id = block.get("id", "")
                    name = block.get("name", "")
                    if tc_id and name:
                        mapping[tc_id] = name

    return mapping


def get_tool_bias(tool_profiles: dict[str, Any] | None, tool_name: str) -> float:
    """Look up compression bias for a tool name.

    Checks user-configured *tool_profiles* first, then DEFAULT_TOOL_PROFILES.
    Returns 1.0 (moderate) if no profile is configured.
    """
    from ..config import DEFAULT_TOOL_PROFILES

    # Check user-configured profiles
    if tool_profiles:
        profile = tool_profiles.get(tool_name)
        if profile:
            return float(profile.bias)

    # Check default profiles
    default_profile = DEFAULT_TOOL_PROFILES.get(tool_name)
    if default_profile:
        return default_profile.bias

    return 1.0  # Default: moderate


def detect_analysis_intent(
    messages: list[dict[str, Any]],
    *,
    logger: logging.Logger,
) -> bool:
    """Detect if user wants to analyze/review code.

    Looks at the most recent user message for genuine analysis verbs,
    matched on word boundaries against ``_ANALYSIS_INTENT_PATTERN``
    (COR-16 — the old substring scan over a broader set tripped on
    e.g. "fix" in "prefix" and left SOURCE_CODE ~never compressed).
    Both plain-string and block-format user content are scanned (text
    parts concatenated — COR-53). The matched keyword is DEBUG-logged
    so over-breadth stays visible.

    Args:
        messages: Conversation messages.
        logger: The router's logger — records keep their historical
            ``furl_ctx.transforms.content_router`` logger name.

    Returns:
        True if analysis intent detected.
    """
    # Find most recent user message
    for message in reversed(messages):
        if message.get("role") == "user":
            content = concat_text_parts(message.get("content", ""))
            if content:
                match = _ANALYSIS_INTENT_PATTERN.search(content.lower())
                if match:
                    logger.debug(
                        "analysis_intent: keyword %r matched in latest user message",
                        match.group(0),
                    )
                    return True
            break

    return False
