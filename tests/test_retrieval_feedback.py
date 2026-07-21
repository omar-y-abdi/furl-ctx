"""Unit tests for the local retrieval-feedback aggregator (Engine P2-13).

``furl_ctx.cache.retrieval_feedback`` closes the ACON-style loop the Great
Excision deleted with the telemetry plane — rebuilt to today's constraints:
LOCAL-ONLY (no telemetry, no disk ledger), signal source = the store's own
retrieval bookkeeping, hysteresis (N retrievals within a sliding window before
a hint fires) and decay (events age out of the window → hints relax), driven
by an injectable monotonic clock.

These tests pin the pure aggregation semantics; the store/router wiring is
pinned in ``test_retrieval_feedback_store_wiring.py`` and
``test_retrieval_feedback_router.py``.
"""

from __future__ import annotations

import dataclasses
import threading

import pytest

from furl_ctx.cache.retrieval_feedback import (
    DEFAULT_HINT_MIN_RETRIEVALS,
    DEFAULT_KEEP_BUDGET_MULTIPLIER,
    DEFAULT_SKIP_MIN_RETRIEVALS,
    DEFAULT_WINDOW_SECONDS,
    NEUTRAL_HINTS,
    FeedbackHints,
    RetrievalFeedback,
    ShapeKey,
    entry_shape_key,
    get_retrieval_feedback,
    record_retrieval_signal,
    reset_retrieval_feedback,
    routing_shape_key,
    set_retrieval_feedback,
)


class FakeClock:
    """Injectable monotonic clock for deterministic hysteresis/decay tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _isolated_global_feedback():
    reset_retrieval_feedback()
    yield
    reset_retrieval_feedback()


def _feedback(clock: FakeClock, **overrides) -> RetrievalFeedback:
    params = {
        "window_seconds": 600.0,
        "hint_min_retrievals": 3,
        "skip_min_retrievals": 6,
        "keep_budget_multiplier": 1.5,
        "clock": clock,
    }
    params.update(overrides)
    return RetrievalFeedback(**params)


SHAPE = ShapeKey(tool="queryapi", content_type="json_array")


# ---------------------------------------------------------------------------
# Neutral default
# ---------------------------------------------------------------------------


def test_fresh_aggregator_returns_neutral_hints() -> None:
    fb = _feedback(FakeClock())

    hints = fb.get_hints(SHAPE)

    assert hints is NEUTRAL_HINTS
    assert hints.keep_budget_multiplier == 1.0
    assert hints.skip_compression is False
    assert hints.retrievals_in_window == 0


def test_defaults_are_the_documented_sane_constants() -> None:
    assert DEFAULT_WINDOW_SECONDS == 600.0
    assert DEFAULT_HINT_MIN_RETRIEVALS == 3
    assert DEFAULT_SKIP_MIN_RETRIEVALS == 6
    assert DEFAULT_KEEP_BUDGET_MULTIPLIER == 1.5
    assert DEFAULT_SKIP_MIN_RETRIEVALS >= DEFAULT_HINT_MIN_RETRIEVALS >= 1


# ---------------------------------------------------------------------------
# Hysteresis: N retrievals in window before a hint fires; N-1 does not
# ---------------------------------------------------------------------------


def test_hint_fires_at_threshold_not_below() -> None:
    fb = _feedback(FakeClock())

    for _ in range(2):  # hint_min_retrievals - 1
        fb.record_retrieval(SHAPE)
    below = fb.get_hints(SHAPE)
    assert below.keep_budget_multiplier == 1.0
    assert below.skip_compression is False
    assert below.retrievals_in_window == 2

    fb.record_retrieval(SHAPE)  # N-th retrieval
    at = fb.get_hints(SHAPE)
    assert at.keep_budget_multiplier == 1.5
    assert at.skip_compression is False
    assert at.retrievals_in_window == 3


def test_skip_fires_at_skip_threshold() -> None:
    fb = _feedback(FakeClock())

    for _ in range(5):  # skip_min_retrievals - 1
        fb.record_retrieval(SHAPE)
    assert fb.get_hints(SHAPE).skip_compression is False

    fb.record_retrieval(SHAPE)
    hints = fb.get_hints(SHAPE)
    assert hints.skip_compression is True
    assert hints.keep_budget_multiplier == 1.5
    assert hints.retrievals_in_window == 6


# ---------------------------------------------------------------------------
# Decay: advancing the injected clock relaxes hints
# ---------------------------------------------------------------------------


def test_decay_relaxes_hint_after_window() -> None:
    clock = FakeClock()
    fb = _feedback(clock)

    for _ in range(3):
        fb.record_retrieval(SHAPE)
    assert fb.get_hints(SHAPE).keep_budget_multiplier == 1.5

    clock.advance(600.0 + 1.0)
    assert fb.get_hints(SHAPE) is NEUTRAL_HINTS


def test_partial_decay_steps_hint_back_down() -> None:
    clock = FakeClock()
    fb = _feedback(clock)

    fb.record_retrieval(SHAPE)
    clock.advance(500.0)
    fb.record_retrieval(SHAPE)
    fb.record_retrieval(SHAPE)
    assert fb.get_hints(SHAPE).keep_budget_multiplier == 1.5

    # First event ages out; only 2 remain in-window -> below threshold.
    clock.advance(150.0)
    relaxed = fb.get_hints(SHAPE)
    assert relaxed.keep_budget_multiplier == 1.0
    assert relaxed.retrievals_in_window == 2


# ---------------------------------------------------------------------------
# Shape-key semantics
# ---------------------------------------------------------------------------


def test_named_records_do_not_leak_to_other_tools() -> None:
    fb = _feedback(FakeClock())
    for _ in range(6):
        fb.record_retrieval(ShapeKey(tool="toola", content_type="json_array"))

    assert fb.get_hints(ShapeKey(tool="toolb", content_type="json_array")) is NEUTRAL_HINTS


def test_content_type_isolation() -> None:
    fb = _feedback(FakeClock())
    for _ in range(6):
        fb.record_retrieval(ShapeKey(tool="queryapi", content_type="json_array"))

    assert fb.get_hints(ShapeKey(tool="queryapi", content_type="build")) is NEUTRAL_HINTS


def test_anonymous_tool_records_count_toward_named_lookup() -> None:
    # Most live CCR producers store with tool_name=None (the SmartCrusher
    # mirrors, the sidecar compressor stores). Those signals land in the
    # tool-anonymous bucket and must still inform routing decisions for the
    # named tool whose output has the same content shape.
    fb = _feedback(FakeClock())
    for _ in range(3):
        fb.record_retrieval(ShapeKey(tool="", content_type="json_array"))

    hints = fb.get_hints(ShapeKey(tool="queryapi", content_type="json_array"))
    assert hints.keep_budget_multiplier == 1.5
    assert hints.retrievals_in_window == 3


def test_exact_and_anonymous_buckets_merge() -> None:
    fb = _feedback(FakeClock())
    for _ in range(2):
        fb.record_retrieval(ShapeKey(tool="queryapi", content_type="json_array"))
    fb.record_retrieval(ShapeKey(tool="", content_type="json_array"))

    hints = fb.get_hints(ShapeKey(tool="queryapi", content_type="json_array"))
    assert hints.retrievals_in_window == 3
    assert hints.keep_budget_multiplier == 1.5


def test_routing_shape_key_normalizes() -> None:
    assert routing_shape_key("QueryAPI", "JSON_ARRAY") == ShapeKey(
        tool="queryapi", content_type="json_array"
    )
    assert routing_shape_key(None, None) == ShapeKey(tool="", content_type="")
    assert routing_shape_key("  Bash  ", " build ") == ShapeKey(tool="bash", content_type="build")


def test_entry_shape_key_maps_strategies_to_content_type_tags() -> None:
    # SmartCrusher mirrors record heterogeneous "smart_crusher*" strategy
    # strings; all of them are JSON-array compressions.
    assert entry_shape_key("t", "smart_crusher_row_drop").content_type == "json_array"
    assert entry_shape_key("t", "smart_crusher_compact_document").content_type == "json_array"
    assert entry_shape_key("t", "smart_crusher").content_type == "json_array"
    # Sidecar routes use their CompressionStrategy value.
    assert entry_shape_key("t", "search").content_type == "search"
    assert entry_shape_key("t", "log").content_type == "build"
    assert entry_shape_key("t", "diff").content_type == "diff"
    assert entry_shape_key("t", "text").content_type == "text"
    assert entry_shape_key("t", "code_aware").content_type == "source_code"
    # Unattributable strategies stay in the unknown bucket.
    assert entry_shape_key("t", "mcp_compress").content_type == ""
    assert entry_shape_key("t", "ccr_offload").content_type == ""
    assert entry_shape_key("t", None).content_type == ""
    assert entry_shape_key(None, None) == ShapeKey(tool="", content_type="")


def test_content_type_tags_pinned_to_detector_enum_values() -> None:
    # The feedback module deliberately does NOT import furl_ctx.transforms
    # (dependency-light cache module, mirrors router_policy's design note).
    # Its local tag strings MUST stay equal to the ContentType enum values the
    # router passes at lookup time — this pin is the coupling contract.
    from furl_ctx.transforms.content_detector import ContentType
    from furl_ctx.transforms.router_policy import (
        CompressionStrategy,
        content_type_from_strategy,
    )

    assert entry_shape_key("t", "smart_crusher").content_type == ContentType.JSON_ARRAY.value
    for strategy in (
        CompressionStrategy.SMART_CRUSHER,
        CompressionStrategy.SEARCH,
        CompressionStrategy.LOG,
        CompressionStrategy.DIFF,
        CompressionStrategy.TEXT,
        CompressionStrategy.CODE_AWARE,
    ):
        assert (
            entry_shape_key("t", strategy.value).content_type
            == content_type_from_strategy(strategy).value
        ), f"strategy {strategy.value} maps to a tag the router would never look up"


# ---------------------------------------------------------------------------
# Types: hints are immutable; invalid tunables rejected loudly
# ---------------------------------------------------------------------------


def test_hints_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        NEUTRAL_HINTS.skip_compression = True  # type: ignore[misc]

    with pytest.raises(dataclasses.FrozenInstanceError):
        FeedbackHints(keep_budget_multiplier=1.5).keep_budget_multiplier = 9.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"window_seconds": 0.0},
        {"window_seconds": -1.0},
        {"hint_min_retrievals": 0},
        {"skip_min_retrievals": 2, "hint_min_retrievals": 3},
        {"keep_budget_multiplier": 0.9},
        {"max_events_per_shape": 2},  # below skip threshold: skip could never fire
        {"max_shapes": 0},
    ],
)
def test_invalid_tunables_rejected(overrides) -> None:
    with pytest.raises(ValueError):
        _feedback(FakeClock(), **overrides)


# ---------------------------------------------------------------------------
# Bounded memory
# ---------------------------------------------------------------------------


def test_shape_count_is_bounded() -> None:
    fb = _feedback(FakeClock(), max_shapes=4)
    shapes = [ShapeKey(tool=f"tool{i}", content_type="json_array") for i in range(10)]
    for shape in shapes:
        fb.record_retrieval(shape)

    tracked = sum(1 for shape in shapes if fb.get_hints(shape).retrievals_in_window > 0)
    assert 0 < tracked <= 4


def test_events_per_shape_are_bounded() -> None:
    fb = _feedback(FakeClock(), max_events_per_shape=8)
    for _ in range(100):
        fb.record_retrieval(SHAPE)

    assert fb.get_hints(SHAPE).retrievals_in_window == 8


# ---------------------------------------------------------------------------
# Thread-safety smoke: concurrent record + read
# ---------------------------------------------------------------------------


def test_thread_safety_smoke_concurrent_record_and_read() -> None:
    fb = _feedback(FakeClock(), max_shapes=16, max_events_per_shape=512)
    shapes = [ShapeKey(tool=f"tool{i}", content_type="json_array") for i in range(4)]
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def writer(shape: ShapeKey) -> None:
        try:
            start.wait(timeout=5)
            for _ in range(200):
                fb.record_retrieval(shape)
        except BaseException as e:  # pragma: no cover - failure path
            errors.append(e)

    def reader(shape: ShapeKey) -> None:
        try:
            start.wait(timeout=5)
            for _ in range(200):
                hints = fb.get_hints(shape)
                assert hints.retrievals_in_window >= 0
        except BaseException as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(s,)) for s in shapes] + [
        threading.Thread(target=reader, args=(s,)) for s in shapes
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    # Every writer wrote 200 events into a distinct shape; all must be visible.
    for shape in shapes:
        assert fb.get_hints(shape).retrievals_in_window == 200


# ---------------------------------------------------------------------------
# Global singleton plumbing (the store/router meet here)
# ---------------------------------------------------------------------------


def test_global_singleton_roundtrip() -> None:
    first = get_retrieval_feedback()
    assert get_retrieval_feedback() is first

    injected = _feedback(FakeClock())
    set_retrieval_feedback(injected)
    assert get_retrieval_feedback() is injected

    reset_retrieval_feedback()
    assert get_retrieval_feedback() is not injected


def test_record_retrieval_signal_records_into_global() -> None:
    clock = FakeClock()
    set_retrieval_feedback(_feedback(clock))

    for _ in range(3):
        record_retrieval_signal(tool_name=None, compression_strategy="smart_crusher_row_drop")

    hints = get_retrieval_feedback().get_hints(
        routing_shape_key("anytool", "json_array"),
    )
    assert hints.keep_budget_multiplier == 1.5
    assert hints.retrievals_in_window == 3
