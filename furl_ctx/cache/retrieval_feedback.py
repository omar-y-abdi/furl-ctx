"""Local retrieval-feedback loop for adaptive compression routing (Engine P2-13).

When the model retrieves CCR content, that retrieval is a SIGNAL that the
compression was too aggressive for that content shape. This module closes the
loop: the store's own retrieval bookkeeping (the honest access bump COR-37
scoped to real hits) feeds an in-process aggregator, and the router consults
it at routing time — content shapes under recent retrieval pressure get a
higher keep budget or skip compression entirely.

Lineage and constraints: the original ``cache/compression_feedback.py`` (613
lines, ACON-style retrieval-rate hints) was deleted in the Great Excision as
part of the telemetry plane — it consumed TOIN. This rebuild keeps the useful
ideas (skip-compression hint, minimum-sample hysteresis, a global accessor
mirroring the store singleton) under today's constraints:

* LOCAL-ONLY — no telemetry, no cross-user plane, no disk ledger beyond what
  the store already keeps. State is one bounded in-memory dict.
* Signal source = OUR OWN retrieve calls — ``CompressionStore.retrieve`` and
  ``CompressionStore._record_search_access`` emit exactly where they bump
  ``retrieval_count`` (zero-result search probes never emit, per COR-37);
  engine-internal verification reads opt out
  (``retrieve(..., record_feedback_signal=False)``).
* Window hysteresis instead of a retrieval-RATE denominator — the old module
  divided retrievals by recorded compressions, a second bookkeeping plane this
  rebuild deliberately does not keep. A hint fires only after
  ``hint_min_retrievals`` retrievals of a shape within ``window_seconds``, and
  DECAYS as events age out of the sliding window. Time is monotonic and the
  clock is injectable for deterministic tests.

Shape key: ``(tool, content_type)``. At RECORD time the key derives from the
entry's stored compression metadata (``tool_name`` + ``compression_strategy``,
via :func:`entry_shape_key`); at LOOKUP time the router builds it from the
tool name and the detected content type (:func:`routing_shape_key`). Most live
CCR producers store with ``tool_name=None``, so those signals land in the
tool-ANONYMOUS bucket (``tool=""``) — :meth:`RetrievalFeedback.get_hints`
merges that bucket into every named-tool lookup of the same content type,
which keeps the loop live today and lets producers that do thread a tool name
get per-tool granularity for free.

Thread model: consulted from router executor threads and updated from
retrieve paths. A single :class:`threading.Lock` around the event dict is the
justified choice over lock-free-append + aggregate-on-read: every critical
section is O(bounded-deque) with no I/O, retrieval signals arrive at
model-call cadence (a few per minute) and router consults at message cadence,
so contention is negligible — while a lock-free design would need per-shape
append buffers plus a reconciling reader for no measurable win. The lock is a
LEAF: this module never calls back into the store or router, so no lock-order
cycle can form (the store emits AFTER releasing its own lock).

Behavior is default-NEUTRAL: the router consults hints only when
``ContentRouterConfig.enable_retrieval_feedback`` is True (default False), and
an empty window yields :data:`NEUTRAL_HINTS` — no retrievals, no hints,
byte-identical routing.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

# Sliding window and thresholds. 600 s spans a retrieval burst within an
# agentic session while decaying well before the session ends; 3 retrievals
# of one shape says "the model keeps needing this back" (hint: keep more);
# 6 says compression of this shape is actively fighting the model (skip).
DEFAULT_WINDOW_SECONDS = 600.0
DEFAULT_HINT_MIN_RETRIEVALS = 3
DEFAULT_SKIP_MIN_RETRIEVALS = 6
# bias > 1 = keep more (see ContentRouter.compress); 1.5 mirrors the strongest
# built-in tool-profile bias rather than inventing a new scale.
DEFAULT_KEEP_BUDGET_MULTIPLIER = 1.5
# Memory bounds: 256 shapes x 64 timestamps = a few hundred KB worst case.
DEFAULT_MAX_EVENTS_PER_SHAPE = 64
DEFAULT_MAX_SHAPES = 256

# Content-type tags — the ``ContentType`` enum VALUES the router passes at
# lookup time (``detection.content_type.value``). Kept as local strings so the
# cache module stays dependency-light (same design note as router_policy:
# never import across the transforms boundary at module level); the equality
# is pinned by test_retrieval_feedback.py::
# test_content_type_tags_pinned_to_detector_enum_values.
TAG_UNKNOWN = ""
TAG_JSON_ARRAY = "json_array"
TAG_SOURCE_CODE = "source_code"
TAG_SEARCH_RESULTS = "search"
TAG_BUILD_OUTPUT = "build"
TAG_GIT_DIFF = "diff"
TAG_PLAIN_TEXT = "text"

# Entry ``compression_strategy`` → content-type tag. SmartCrusher mirrors
# record heterogeneous "smart_crusher*" strings (row_drop, compact_document,
# Rust strategy_info fallbacks) — all JSON-array compressions, matched by
# prefix below. The sidecar routes record their CompressionStrategy value.
# Strategies that don't attribute to one routed shape ("mcp_compress",
# "ccr_offload", "cross_message_dedup", "read_lifecycle:*") map to
# TAG_UNKNOWN: their signals stay in the unknown bucket rather than biasing a
# shape they don't describe.
_SMART_CRUSHER_STRATEGY_PREFIX = "smart_crusher"
_STRATEGY_CONTENT_TYPE_TAGS: dict[str, str] = {
    _SMART_CRUSHER_STRATEGY_PREFIX: TAG_JSON_ARRAY,
    "search": TAG_SEARCH_RESULTS,
    "log": TAG_BUILD_OUTPUT,
    "diff": TAG_GIT_DIFF,
    "text": TAG_PLAIN_TEXT,
    "code_aware": TAG_SOURCE_CODE,
}

Clock = Callable[[], float]


def _normalize(value: str | None) -> str:
    """Total normalizer for shape-key components: ``None`` → ``""``,
    else trimmed lowercase (tool names match case-insensitively everywhere
    else in the engine — see ``is_tool_excluded``)."""
    return (value or "").strip().lower()


def _content_type_tag_for_strategy(compression_strategy: str | None) -> str:
    """Map a stored ``compression_strategy`` string to a content-type tag.

    Total: unknown/None strategies map to :data:`TAG_UNKNOWN`.
    """
    normalized = _normalize(compression_strategy)
    if not normalized:
        return TAG_UNKNOWN
    if normalized.startswith(_SMART_CRUSHER_STRATEGY_PREFIX):
        return TAG_JSON_ARRAY
    return _STRATEGY_CONTENT_TYPE_TAGS.get(normalized, TAG_UNKNOWN)


@dataclass(frozen=True)
class ShapeKey:
    """Identity of one content shape: ``(tool, content_type)``.

    ``tool=""`` is the tool-anonymous bucket (producer stored no tool name);
    ``content_type=""`` is the unknown-shape bucket. Build via the smart
    constructors — :func:`routing_shape_key` (router lookup side) and
    :func:`entry_shape_key` (store record side) — so both ends normalize
    identically.
    """

    tool: str
    content_type: str


def routing_shape_key(tool_name: str | None, content_type_tag: str | None) -> ShapeKey:
    """Shape key as the ROUTER sees it: tool name + detected content type."""
    return ShapeKey(tool=_normalize(tool_name), content_type=_normalize(content_type_tag))


def entry_shape_key(tool_name: str | None, compression_strategy: str | None) -> ShapeKey:
    """Shape key as a STORE ENTRY describes it: tool name + the content-type
    tag its compression strategy implies."""
    return ShapeKey(
        tool=_normalize(tool_name),
        content_type=_content_type_tag_for_strategy(compression_strategy),
    )


@dataclass(frozen=True)
class FeedbackHints:
    """Routing hints for one shape. Immutable; neutral by construction.

    ``keep_budget_multiplier`` multiplies the compression bias (>1 = keep
    more); ``skip_compression`` asks the router to serve the original
    verbatim. ``retrievals_in_window``/``reason`` are observability fields.
    """

    keep_budget_multiplier: float = 1.0
    skip_compression: bool = False
    retrievals_in_window: int = 0
    reason: str = ""


#: Shared neutral verdict — the hot no-signals path allocates nothing.
NEUTRAL_HINTS = FeedbackHints()


class RetrievalFeedback:
    """Bounded, thread-safe aggregator of retrieval signals per content shape.

    See the module docstring for the design (window hysteresis + decay,
    anonymous-bucket merge, lock-choice justification).
    """

    def __init__(
        self,
        *,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        hint_min_retrievals: int = DEFAULT_HINT_MIN_RETRIEVALS,
        skip_min_retrievals: int = DEFAULT_SKIP_MIN_RETRIEVALS,
        keep_budget_multiplier: float = DEFAULT_KEEP_BUDGET_MULTIPLIER,
        max_events_per_shape: int = DEFAULT_MAX_EVENTS_PER_SHAPE,
        max_shapes: int = DEFAULT_MAX_SHAPES,
        clock: Clock = time.monotonic,
    ) -> None:
        """Validate tunables (fail fast) and initialize empty state.

        Raises:
            ValueError: on any tunable that would make the loop incoherent —
                a non-positive window, thresholds below 1 or inverted
                (skip below hint), a multiplier below 1.0 (hints exist to be
                LESS aggressive, never more), or an event bound too small for
                the skip threshold to ever fire.
        """
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be > 0, got {window_seconds!r}")
        if hint_min_retrievals < 1:
            raise ValueError(f"hint_min_retrievals must be >= 1, got {hint_min_retrievals!r}")
        if skip_min_retrievals < hint_min_retrievals:
            raise ValueError(
                f"skip_min_retrievals ({skip_min_retrievals!r}) must be >= "
                f"hint_min_retrievals ({hint_min_retrievals!r})"
            )
        if keep_budget_multiplier < 1.0:
            raise ValueError(
                f"keep_budget_multiplier must be >= 1.0 (hints only relax "
                f"compression), got {keep_budget_multiplier!r}"
            )
        if max_events_per_shape < skip_min_retrievals:
            raise ValueError(
                f"max_events_per_shape ({max_events_per_shape!r}) must be >= "
                f"skip_min_retrievals ({skip_min_retrievals!r}) or the skip "
                f"hint can never fire"
            )
        if max_shapes < 1:
            raise ValueError(f"max_shapes must be >= 1, got {max_shapes!r}")

        self._window_seconds = float(window_seconds)
        self._hint_min_retrievals = int(hint_min_retrievals)
        self._skip_min_retrievals = int(skip_min_retrievals)
        self._keep_budget_multiplier = float(keep_budget_multiplier)
        self._max_events_per_shape = int(max_events_per_shape)
        self._max_shapes = int(max_shapes)
        self._clock = clock
        self._lock = threading.Lock()
        # shape -> monotonic timestamps of retrievals, oldest first. Each
        # deque is bounded by maxlen; the dict is bounded by _max_shapes.
        self._events: dict[ShapeKey, deque[float]] = {}

    def record_retrieval(self, shape: ShapeKey) -> None:
        """Record one model-driven retrieval of *shape* at the current clock."""
        now = self._clock()
        with self._lock:
            events = self._events.get(shape)
            if events is None:
                if len(self._events) >= self._max_shapes:
                    self._evict_one_shape_locked(now)
                events = deque(maxlen=self._max_events_per_shape)
                self._events[shape] = events
            events.append(now)

    def get_hints(self, shape: ShapeKey) -> FeedbackHints:
        """Return the hints the current window supports for *shape*.

        Counts the shape's own bucket plus — for named-tool lookups — the
        tool-anonymous bucket of the same content type (see module docstring).
        Returns :data:`NEUTRAL_HINTS` when the window holds no signal.
        """
        cutoff = self._clock() - self._window_seconds
        with self._lock:
            count = self._count_in_window_locked(shape, cutoff)
            if shape.tool:
                anonymous = ShapeKey(tool="", content_type=shape.content_type)
                count += self._count_in_window_locked(anonymous, cutoff)

        if count >= self._skip_min_retrievals:
            return FeedbackHints(
                keep_budget_multiplier=self._keep_budget_multiplier,
                skip_compression=True,
                retrievals_in_window=count,
                reason=(
                    f"{count} retrievals of shape {shape} within "
                    f"{self._window_seconds:.0f}s (>= skip threshold "
                    f"{self._skip_min_retrievals}): skip compression"
                ),
            )
        if count >= self._hint_min_retrievals:
            return FeedbackHints(
                keep_budget_multiplier=self._keep_budget_multiplier,
                skip_compression=False,
                retrievals_in_window=count,
                reason=(
                    f"{count} retrievals of shape {shape} within "
                    f"{self._window_seconds:.0f}s (>= hint threshold "
                    f"{self._hint_min_retrievals}): raise keep budget"
                ),
            )
        if count == 0:
            return NEUTRAL_HINTS
        return FeedbackHints(retrievals_in_window=count)

    def clear(self) -> None:
        """Drop all recorded signals. Mainly for testing."""
        with self._lock:
            self._events.clear()

    # -- internals (call with self._lock held) --------------------------------

    def _count_in_window_locked(self, shape: ShapeKey, cutoff: float) -> int:
        """Count in-window events for *shape*, pruning aged-out ones.

        Timestamps are appended in lock order from a monotonic clock, so each
        deque is non-decreasing and the aged head can be popped outright —
        this is the decay: pruned events are gone, hints relax.
        """
        events = self._events.get(shape)
        if events is None:
            return 0
        while events and events[0] <= cutoff:
            events.popleft()
        if not events:
            del self._events[shape]
            return 0
        return len(events)

    def _evict_one_shape_locked(self, now: float) -> None:
        """Make room for one new shape: reap fully-decayed shapes first, then
        (if still at capacity) evict the shape with the oldest newest event —
        the one closest to decaying on its own."""
        cutoff = now - self._window_seconds
        decayed = [
            shape for shape, events in self._events.items() if not events or events[-1] <= cutoff
        ]
        for shape in decayed:
            del self._events[shape]
        if len(self._events) < self._max_shapes:
            return
        stalest = min(self._events, key=lambda shape: self._events[shape][-1])
        del self._events[stalest]


# ---------------------------------------------------------------------------
# Global accessor — one aggregator per process, mirroring the compression
# store's lazy singleton so the store (record side) and router (lookup side)
# meet without explicit plumbing.
# ---------------------------------------------------------------------------

_retrieval_feedback: RetrievalFeedback | None = None
_feedback_lock = threading.Lock()


def get_retrieval_feedback() -> RetrievalFeedback:
    """Get the process-wide retrieval-feedback aggregator (lazy init)."""
    global _retrieval_feedback
    if _retrieval_feedback is None:
        with _feedback_lock:
            if _retrieval_feedback is None:
                _retrieval_feedback = RetrievalFeedback()
    return _retrieval_feedback


def set_retrieval_feedback(feedback: RetrievalFeedback | None) -> None:
    """Install a specific aggregator (tests inject clocks/thresholds here);
    ``None`` restores lazy default construction on next access."""
    global _retrieval_feedback
    with _feedback_lock:
        _retrieval_feedback = feedback


def reset_retrieval_feedback() -> None:
    """Clear and drop the global aggregator. Mainly for testing."""
    global _retrieval_feedback
    with _feedback_lock:
        if _retrieval_feedback is not None:
            _retrieval_feedback.clear()
        _retrieval_feedback = None


def record_retrieval_signal(
    *,
    tool_name: str | None,
    compression_strategy: str | None,
) -> None:
    """Record one model-driven retrieval into the global aggregator.

    The store's retrieval choke points call this with the retrieved entry's
    compression metadata; the shape key derives via :func:`entry_shape_key`.
    """
    get_retrieval_feedback().record_retrieval(entry_shape_key(tool_name, compression_strategy))
