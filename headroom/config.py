"""Configuration models for Headroom SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class CacheAlignerConfig:
    """Configuration for cache alignment.

    The CacheAligner transform is detector-only: it reads only ``.enabled``
    and emits prefix-stability warnings; it never rewrites the prompt.

    SAFE: Only applied to SYSTEM messages, not user/assistant/tool content.
    """

    enabled: bool = False  # Disabled by default — prefix stability gains are marginal in practice


@dataclass
class RelevanceScorerConfig:
    """Configuration for BM25 relevance scoring in SmartCrusher.

    Relevance scoring determines which items to keep when compressing tool
    outputs: relevance(item, context) -> [0, 1]. The Python surface is
    BM25-only (zero dependencies, fast keyword matching) — the semantic /
    embedding / hybrid scorers were retired with the public SDK surface.

    NOTE: in the standalone build the live compression core scores items in
    Rust with a fixed threshold; ``SmartCrusher`` raises ``NotImplementedError``
    if a custom ``relevance_config`` / ``scorer`` is supplied. These fields are
    the documented defaults, not per-call tunables.
    """

    tier: Literal["bm25"] = "bm25"

    # BM25 parameters
    bm25_k1: float = 1.5  # Term frequency saturation
    bm25_b: float = 0.75  # Length normalization

    # Scoring threshold
    # Lower threshold = safer (keeps more items), higher = more aggressive.
    relevance_threshold: float = 0.25  # Keep items above this score


@dataclass
class AnchorConfig:
    """Configuration for dynamic anchor allocation in SmartCrusher.

    Anchor selection determines which array positions are preserved during
    compression. Different data patterns benefit from different anchor strategies:
    - Search results: Front-heavy (top results are most relevant)
    - Logs: Back-heavy (recent entries matter most)
    - Time series: Balanced (need both ends to show trends)
    - Generic: Distributed (no assumption about order importance)

    The anchor budget is a percentage of max_items allocated to position-based
    anchors. The remaining budget goes to relevance-scored items.
    """

    # Base anchor budget as percentage of max_items
    anchor_budget_pct: float = 0.25  # 25% of slots for position anchors

    # Minimum and maximum anchor slots
    min_anchor_slots: int = 3
    max_anchor_slots: int = 12

    # Default distribution weights (sum to 1.0)
    default_front_weight: float = 0.5
    default_back_weight: float = 0.4
    default_middle_weight: float = 0.1

    # Pattern-specific overrides
    search_front_weight: float = 0.75  # Search results: front-heavy
    search_back_weight: float = 0.15
    logs_front_weight: float = 0.15  # Logs: back-heavy (recent)
    logs_back_weight: float = 0.75

    # Query keyword detection for dynamic adjustment
    recency_keywords: tuple[str, ...] = (
        "latest",
        "recent",
        "last",
        "newest",
        "current",
        "now",
    )
    historical_keywords: tuple[str, ...] = (
        "first",
        "oldest",
        "earliest",
        "original",
        "initial",
        "beginning",
    )

    # Information density selection
    use_information_density: bool = True
    candidate_multiplier: int = 3  # Consider 3x candidates per slot
    dedup_identical_items: bool = True  # Don't waste slots on identical items


# Default tools to exclude from compression (local file/code tools)
# Read: Returns exact file content needed for Edit tool's old_string matching.
#   Compressing would break the edit workflow.
# Glob: Returns compact file path lists used for navigation. Low token count,
#   not worth compressing.
# Tool outputs that are reference data and must NOT be compressed.
# Read/Glob/Grep contain exact file contents/search results the agent needs for edits.
# Write/Edit record what changes were made — compressing them causes duplicate/conflicting edits.
# Bash is NOT excluded — its outputs (build logs, test output) are ideal compression targets.
DEFAULT_EXCLUDE_TOOLS: frozenset[str] = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "Write",
        "Edit",
        "Bash",
        # Lowercase variants for case-insensitive matching
        "read",
        "glob",
        "grep",
        "write",
        "edit",
        "bash",
    }
)

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
    """

    bias: float = 1.0  # 0.7=aggressive, 1.0=moderate, 1.5=conservative
    min_k: int = 3  # Never keep fewer than this
    max_k: int | None = None  # Cap (None = no cap, let statistics decide)


# Named presets for convenience
PROFILE_PRESETS: dict[str, CompressionProfile] = {
    "conservative": CompressionProfile(bias=1.5, min_k=5),
    "moderate": CompressionProfile(bias=1.0, min_k=3),
    "aggressive": CompressionProfile(bias=0.7, min_k=3),
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
class SmartCrusherConfig:
    """Configuration for smart statistical crusher (DEFAULT).

    Uses statistical analysis to intelligently compress tool outputs while
    PRESERVING THE ORIGINAL JSON SCHEMA. Output contains only items from
    the original array - no wrappers, no generated text, no metadata.

    Handles ALL JSON types:
    - Arrays of dicts, strings, numbers, mixed types
    - Flat objects with many keys
    - Nested objects (recursive compression)

    Safety guarantees (consistent across all types):
    - First K, last K items always kept (K is adaptive via Kneedle algorithm)
    - Error items never dropped
    - Anomalous numeric items (> 2 std from mean) always kept
    - Items matching query context via RelevanceScorer

    GOTCHAS:
    - Adds ~5-10ms overhead per tool output for statistical analysis
    - Change point detection uses fixed window (5 items) - may miss:
      - Very gradual changes
      - Patterns in smaller arrays
    - TOP_N for search results assumes higher score = more relevant
      (may not be true for all APIs)

    SAFER SETTINGS:
    - Increase max_items_after_crush for critical data
    - Set variance_threshold lower (1.5) to catch more change points
    """

    enabled: bool = True  # Enabled by default — sole tool-output compressor
    min_items_to_analyze: int = 5  # Don't analyze tiny arrays
    min_tokens_to_crush: int = 200  # Only crush if > N tokens
    variance_threshold: float = 2.0  # Std devs for change point detection
    uniqueness_threshold: float = 0.1  # Below this = nearly constant
    similarity_threshold: float = 0.8  # For clustering similar strings
    max_items_after_crush: int = 15  # Target max items in output
    preserve_change_points: bool = True
    factor_out_constants: bool = False  # Disabled - preserves original schema
    include_summaries: bool = False  # Disabled - no generated text

    # Feedback loop integration (TOIN - Tool Output Intelligence Network)
    use_feedback_hints: bool = True  # Use learned patterns to adjust compression

    # TOIN confidence threshold (configurable).
    # Minimum confidence required to apply TOIN recommendations
    toin_confidence_threshold: float = 0.3

    # Relevance scoring configuration
    relevance: RelevanceScorerConfig = field(default_factory=RelevanceScorerConfig)

    # Anchor selection configuration (dynamic position-based preservation)
    anchor: AnchorConfig = field(default_factory=AnchorConfig)

    # Content deduplication - prevents wasting slots on identical items
    # When multiple preservation mechanisms (anchors, anomalies, outliers) add
    # the same item, only one copy is kept. This is critical for arrays where
    # many items have identical content (e.g., repeated status messages).
    dedup_identical_items: bool = True

    # Adaptive K boundary allocation (fraction of total K for first/last items)
    # The remaining fraction is filled by importance scoring (errors, anomalies, etc.)
    first_fraction: float = 0.3  # 30% of K from start of array
    last_fraction: float = 0.15  # 15% of K from end of array

    # Lossless compaction only replaces the original when it saves at
    # least this byte fraction vs the (minified) input. Mirrors the
    # Rust default.
    lossless_min_savings_ratio: float = 0.30


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
    4. RETRIEVE: If LLM needs more, it calls headroom_retrieve(hash, query)

    Benefits:
    - Zero-risk compression: worst case = LLM retrieves what it needs
    - Feedback loop: track what gets retrieved to improve compression
    - Network effect: retrieval patterns improve compression for all users

    GOTCHAS:
    - Cache has TTL (default 300 seconds) - retrieval fails after expiration
    - Memory usage: ~1KB per cached entry
    - Only works with array compression (not string truncation)
    """

    enabled: bool = True  # Enable CCR (cache + retrieval markers)
    inject_retrieval_marker: bool = True  # Add retrieval hint to compressed output


@dataclass
class HeadroomConfig:
    """Main configuration for HeadroomClient."""

    cache_aligner: CacheAlignerConfig = field(default_factory=CacheAlignerConfig)
    ccr: CCRConfig = field(default_factory=CCRConfig)  # Compress-Cache-Retrieve

    # Cross-message dedup: replace later byte-identical tool outputs with a
    # recoverable <<ccr:HASH>> pointer to the first occurrence. Operates on
    # content WITHIN messages only (never drops/reorders messages, never
    # touches index 0, frozen prefixes, or cache_control blocks).
    cross_message_dedup_enabled: bool = True

    # Debugging - opt-in diff artifact generation
    generate_diff_artifact: bool = False  # Enable to get detailed transform diffs


@dataclass
class Block:
    """Atomic unit of context analysis."""

    kind: Literal["system", "user", "assistant", "tool_call", "tool_result", "rag", "unknown"]
    text: str
    tokens_est: int
    content_hash: str
    source_index: int  # Position in original messages
    flags: dict[str, Any] = field(default_factory=dict)


@dataclass
class WasteSignals:
    """Detected waste signals in a request."""

    json_bloat_tokens: int = 0  # JSON blocks > 500 tokens
    html_noise_tokens: int = 0  # HTML tags/comments
    base64_tokens: int = 0  # Base64 encoded blobs
    whitespace_tokens: int = 0  # Repeated whitespace
    dynamic_date_tokens: int = 0  # Dynamic dates in system prompt
    repetition_tokens: int = 0  # Repeated content
    reread_tokens: int = 0  # Tool results re-served after already appearing earlier

    def total(self) -> int:
        """Total waste tokens detected."""
        return (
            self.json_bloat_tokens
            + self.html_noise_tokens
            + self.base64_tokens
            + self.whitespace_tokens
            + self.dynamic_date_tokens
            + self.repetition_tokens
            + self.reread_tokens
        )

    def to_dict(self) -> dict[str, int]:
        """Convert to dictionary for storage."""
        return {
            "json_bloat": self.json_bloat_tokens,
            "html_noise": self.html_noise_tokens,
            "base64": self.base64_tokens,
            "whitespace": self.whitespace_tokens,
            "dynamic_date": self.dynamic_date_tokens,
            "repetition": self.repetition_tokens,
            "reread": self.reread_tokens,
        }


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
    option whose default genuinely diverged between entry paths. The three
    per-request runtime options on ``ContentRouter`` (``target_ratio`` /
    ``force_kompress`` / ``kompress_model``) live in their own frozen
    ``RouterRuntime`` value, threaded by argument down the compress() call
    chain: that value-by-argument isolation is pinned by
    ``test_runtime_options_thread_safety`` (concurrent compress() calls with
    distinct runtimes, spied at the ML consumer) and they stay there rather
    than here.
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
    diff_artifact: DiffArtifact | None = None  # Populated if generate_diff_artifact=True
    cache_metrics: CachePrefixMetrics | None = None  # Populated by CacheAligner
    timing: dict[str, float] = field(default_factory=dict)  # transform_name → ms
    waste_signals: WasteSignals | None = None  # Detected waste in original messages

    @property
    def transforms_summary(self) -> dict[str, int]:
        """Counted summary of transforms_applied (e.g. {'router:tool_result:text': 4})."""
        from collections import Counter

        return dict(Counter(self.transforms_applied))


@dataclass
class TransformDiff:
    """Diff info for a single transform (for debugging/perf)."""

    transform_name: str
    tokens_before: int
    tokens_after: int
    tokens_saved: int
    items_removed: int = 0
    items_kept: int = 0
    details: str = ""  # Human-readable description of what changed
    duration_ms: float = 0.0  # Wall-clock time for this transform


@dataclass
class DiffArtifact:
    """Complete diff artifact for debugging transform pipeline.

    Opt-in via HeadroomConfig.generate_diff_artifact = True.
    Useful for understanding what each transform did to your messages.
    """

    request_id: str
    original_tokens: int
    optimized_tokens: int
    total_tokens_saved: int
    transforms: list[TransformDiff] = field(default_factory=list)
