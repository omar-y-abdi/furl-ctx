"""Configuration models for Furl SDK."""

from __future__ import annotations

import fnmatch
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CacheAlignerConfig:
    """Configuration for cache alignment.

    The CacheAligner transform is detector-only: it reads only ``.enabled``
    and emits prefix-stability warnings; it never rewrites the prompt.

    SAFE: Only applied to SYSTEM messages, not user/assistant/tool content.
    """

    enabled: bool = False  # Disabled by default — prefix stability gains are marginal in practice


# Default tools to exclude from compression (local file/code tools)
# Read: Returns exact file content needed for Edit tool's old_string matching.
#   Compressing would break the edit workflow.
# Glob: Returns compact file path lists used for navigation. Low token count,
#   not worth compressing.
# Tool outputs that are reference data and must NOT be compressed.
# Read/Glob/Grep contain exact file contents/search results the agent needs for edits.
# Write/Edit record what changes were made — compressing them causes duplicate/conflicting edits.
# Bash is NOT excluded — its outputs (build logs, test output) are ideal compression targets.
# Entries are matched via is_tool_excluded(): case-insensitive, fnmatch-style
# globs supported (e.g. "mcp__*").
DEFAULT_EXCLUDE_TOOLS: frozenset[str] = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "Write",
        "Edit",
        # Lowercase variants for case-insensitive matching
        "read",
        "glob",
        "grep",
        "write",
        "edit",
    }
)


def is_tool_excluded(tool_name: str, exclude_tools: Collection[str]) -> bool:
    """Total, pure exclusion check: does *tool_name* match any entry?

    Restores the upstream matching semantics that plain ``in`` lost:

    * **Exact entries** keep working unchanged (fast-path membership test).
    * **Case-insensitive**: ``"READ"`` matches an entry ``"Read"``.
    * **fnmatch-style globs**: an entry like ``"mcp__*"`` matches every
      MCP-prefixed tool name, case-insensitively.

    An empty *tool_name* never matches — an unnamed tool cannot be excluded
    by name (and must not match a bare ``"*"`` entry by accident).
    """
    if not tool_name:
        return False
    if tool_name in exclude_tools:
        return True
    folded = tool_name.casefold()
    return any(fnmatch.fnmatchcase(folded, pattern.casefold()) for pattern in exclude_tools)


# Tool names recognized as Read/Edit/Write for lifecycle tracking
_READ_TOOL_NAMES: frozenset[str] = frozenset({"Read", "read"})
_EDIT_TOOL_NAMES: frozenset[str] = frozenset({"Edit", "edit"})
_WRITE_TOOL_NAMES: frozenset[str] = frozenset({"Write", "write"})
_MUTATING_TOOL_NAMES: frozenset[str] = _EDIT_TOOL_NAMES | _WRITE_TOOL_NAMES


@dataclass
class ReadLifecycleConfig:
    """Event-driven Read lifecycle management.

    Detects stale and superseded Read outputs in the conversation and replaces
    them with compact markers + CCR hashes. Fresh Reads are never touched.

    A Read is STALE when the file was subsequently edited (content is factually
    wrong). A Read is SUPERSEDED when the file was subsequently re-Read (content
    is redundant). Both are provably safe to compress.

    Operates as a pre-processing pass before ContentRouter, independent of
    tool exclusion logic. Read remains in DEFAULT_EXCLUDE_TOOLS — fresh Read
    outputs still bypass ContentRouter compression.
    """

    enabled: bool = True  # On by default: stale/superseded Reads are provably safe to compress
    compress_stale: bool = True  # Replace Reads of files that were later edited
    compress_superseded: bool = False  # Disabled: busts Anthropic prompt cache prefix
    min_size_bytes: int = 512  # Skip tiny Read outputs (not worth the overhead)


@dataclass
class CompressionProfile:
    """Per-tool compression bias applied to statistically-determined K.

    Instead of hardcoding max_items=15, the adaptive sizer computes the optimal K
    via information saturation (Kneedle on unique bigram coverage). This profile
    applies a bias multiplier: >1 keeps more items (conservative), <1 keeps fewer
    (aggressive).

    ``bias`` is the ONLY field: the consumer (``ContentRouter._get_tool_bias``)
    reads nothing else. The historical ``min_k``/``max_k`` fields were unread —
    the real keep-floor lives in the Rust adaptive sizer (API-14; the
    "conservative" preset's ``min_k=5`` promise did nothing).
    """

    bias: float = 1.0  # 0.7=aggressive, 1.0=moderate, 1.5=conservative


# Named presets for convenience
PROFILE_PRESETS: dict[str, CompressionProfile] = {
    "conservative": CompressionProfile(bias=1.5),
    "moderate": CompressionProfile(bias=1.0),
    "aggressive": CompressionProfile(bias=0.7),
}

# Default per-tool profiles: tools not listed here use moderate (bias=1.0)
DEFAULT_TOOL_PROFILES: dict[str, CompressionProfile] = {
    # Search results: keep more matches for accuracy
    "Grep": PROFILE_PRESETS["conservative"],
    "grep": PROFILE_PRESETS["conservative"],
    # Logs/output: balanced compression
    "Bash": PROFILE_PRESETS["moderate"],
    "bash": PROFILE_PRESETS["moderate"],
    # Web pages are verbose, compress aggressively
    "WebFetch": PROFILE_PRESETS["aggressive"],
    "webfetch": PROFILE_PRESETS["aggressive"],
}


@dataclass
class CCRConfig:
    """Configuration for Compress-Cache-Retrieve architecture.

    CCR makes compression REVERSIBLE: when SmartCrusher compresses tool outputs,
    the original data is cached. If the LLM needs more data, it can retrieve it.

    Key insight from research: REVERSIBLE compression beats irreversible compression.
    - Phil Schmid: "Prefer raw > Compaction > Summarization"
    - Factory.ai: "Cutting context too aggressively can backfire"

    How CCR works:
    1. COMPRESS: SmartCrusher compresses array from 1000 to 20 items
    2. CACHE: Original 1000 items stored in CompressionStore
    3. INJECT: Marker added to tell LLM how to retrieve more
    4. RETRIEVE: If LLM needs more, it calls furl_retrieve(hash, query)

    Benefits:
    - Zero-risk compression: worst case = LLM retrieves what it needs

    GOTCHAS:
    - Cache has TTL (default 1800 seconds) - retrieval fails after expiration
    - Memory usage: ~1KB per cached entry
    - Only works with array compression (not string truncation)

    HONEST FLAG SEMANTICS (neither flag is a data-loss switch): whenever
    the engine drops or substitutes a distinct item, the recovery pointer
    is surfaced and the store is written UNCONDITIONALLY — regardless of
    these flags (a drop without its pointer is silent loss, which the
    recovery invariant forbids; pinned by
    tests/test_ccr_recovery_invariant.py). The flags are the
    retrieval-tool advertisement preference: they flow to the Rust
    ``advertise_retrieval_tool`` field and are preserved on
    ``SmartCrusher._ccr_config`` for embedders that read them back when
    deciding whether to inject the ``furl_retrieve`` tool; the router
    additionally requires them for the optional CCR-offload fallback.
    To get output with no ``<<ccr:`` pointers at all, use the router's
    ``lossless_only`` mode — it never drops, so nothing needs a pointer.
    """

    enabled: bool = True  # Retrieval-tool advertisement preference (see above)
    inject_retrieval_marker: bool = True  # Retrieval-tool advertisement preference (see above)


@dataclass
class FurlConfig:
    """Main configuration for FurlClient.

    (API-14: the ``ccr: CCRConfig`` field had zero readers and was
    deleted — the live retrieval-advertisement flags are
    ``ContentRouterConfig.ccr_*`` via ``compressor_registry``; pass a
    ``CCRConfig`` to ``SmartCrusher(ccr_config=...)`` directly when
    constructing one by hand.)
    """

    cache_aligner: CacheAlignerConfig = field(default_factory=CacheAlignerConfig)

    # Cross-message dedup: replace later byte-identical tool outputs with a
    # recoverable <<ccr:HASH>> pointer to the first occurrence. Operates on
    # content WITHIN messages only (never drops/reorders messages, never
    # touches index 0, frozen prefixes, or cache_control blocks).
    cross_message_dedup_enabled: bool = True


@dataclass
class CachePrefixMetrics:
    """Detailed cache prefix metrics for debugging cache misses.

    Log these per-request to understand why caching is or isn't working.
    Compare stable_prefix_hash across requests - any change means cache miss.
    """

    stable_prefix_bytes: int  # Byte length of static prefix
    stable_prefix_tokens_est: int  # Estimated token count of static prefix
    stable_prefix_hash: str  # Hash of canonicalized prefix (16 chars)
    prefix_changed: (
        bool  # True iff stable_prefix_hash differs from the caller-supplied previous hash
    )
    previous_hash: str | None = (
        None  # The previous_prefix_hash the caller threaded in (None = not supplied)
    )


#: The minimum token count for a message to be compressed, used as the single
#: default for ``CompressRequest.min_tokens_to_compress``. Defined here so that
#: BOTH entry paths agree: ``compress()`` (which forwards
#: ``CompressConfig.min_tokens_to_compress``, itself defaulting to this value)
#: and direct ``TransformPipeline.apply(**kwargs)`` callers (who omit it).
#: Before this constant, the two paths disagreed — ``compress()`` callers got
#: 250 while direct callers silently got 50 inside ``ContentRouter.apply``.
DEFAULT_MIN_TOKENS_TO_COMPRESS = 250


@dataclass(frozen=True)
class CompressRequest:
    """Typed, immutable per-request compression options.

    Built ONCE at the pipeline boundary (``TransformPipeline.apply``) from the
    loosely-typed ``**kwargs`` bag and then threaded explicitly through the
    transforms, replacing untyped ``kwargs.get(...)`` reads for the options it
    owns. The public ``**kwargs`` entry surface is preserved for back-compat
    (``TransformPipeline`` is in ``__all__`` and direct callers pass kwargs);
    this object is constructed FROM those kwargs at the single boundary, so the
    default lives in exactly one place.

    Scope: currently owns ``min_tokens_to_compress`` — the one per-request
    option whose default genuinely diverged between entry paths.
    """

    min_tokens_to_compress: int = DEFAULT_MIN_TOKENS_TO_COMPRESS
    """Minimum token count for a message to be compressed. Messages shorter
    than this are left unchanged. Defaults to
    ``DEFAULT_MIN_TOKENS_TO_COMPRESS`` (250)."""

    @classmethod
    def from_kwargs(cls, kwargs: dict[str, Any]) -> CompressRequest:
        """Build the typed request from a loose kwargs bag (boundary smart
        constructor).

        Reads only the keys this request owns, applying the single unified
        default for any that are absent. Unknown keys are ignored here — the
        kwargs bag still flows through for options not yet migrated.
        """
        return cls(
            min_tokens_to_compress=kwargs.get(
                "min_tokens_to_compress", DEFAULT_MIN_TOKENS_TO_COMPRESS
            ),
        )


@dataclass
class TransformResult:
    """Output of a transform operation."""

    messages: list[dict[str, Any]]
    tokens_before: int
    tokens_after: int
    transforms_applied: list[str]
    markers_inserted: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cache_metrics: CachePrefixMetrics | None = None  # Populated by CacheAligner
    timing: dict[str, float] = field(default_factory=dict)  # transform_name → ms
