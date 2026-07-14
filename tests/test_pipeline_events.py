"""TEST-25: PipelineExtensionManager dispatch — fail-open + smoke coverage.

``compress()`` emits lifecycle events through this manager on every call;
its fail-open contract (a raising extension must never break the emit, and
therefore never break compression) had no direct test — a regression there
would fire exactly when a user wires a buggy hook.
"""

from __future__ import annotations

from dataclasses import replace

from furl_ctx.pipeline import (
    PipelineEvent,
    PipelineExtensionManager,
    PipelineStage,
    summarize_routing_markers,
)


class _RecordingExtension:
    def __init__(self) -> None:
        self.events: list[PipelineEvent] = []

    def on_pipeline_event(self, event: PipelineEvent) -> None:
        self.events.append(event)


class _RaisingExtension:
    def __init__(self) -> None:
        self.calls = 0

    def on_pipeline_event(self, event: PipelineEvent) -> None:
        self.calls += 1
        raise RuntimeError("INJECTED extension failure")


class _RewritingExtension:
    def on_pipeline_event(self, event: PipelineEvent) -> PipelineEvent:
        return replace(event, metadata={**event.metadata, "rewritten": True})


def test_no_extensions_manager_is_disabled_and_emit_is_total() -> None:
    manager = PipelineExtensionManager()
    assert not manager.enabled
    event = manager.emit(PipelineStage.INPUT_RECEIVED, operation="compress")
    assert event.stage is PipelineStage.INPUT_RECEIVED
    assert event.operation == "compress"


def test_extensions_receive_events_in_order() -> None:
    recorder = _RecordingExtension()
    manager = PipelineExtensionManager(hooks=recorder)
    assert manager.enabled

    manager.emit(PipelineStage.INPUT_RECEIVED, operation="compress", model="gpt-4o")
    manager.emit(PipelineStage.INPUT_COMPRESSED, operation="compress", model="gpt-4o")

    assert [e.stage for e in recorder.events] == [
        PipelineStage.INPUT_RECEIVED,
        PipelineStage.INPUT_COMPRESSED,
    ]
    assert recorder.events[0].model == "gpt-4o"


def test_rewriting_extension_replaces_the_event() -> None:
    # Frozen events: handlers return a replacement; emit adopts it.
    manager = PipelineExtensionManager(hooks=_RewritingExtension())
    event = manager.emit(PipelineStage.INPUT_COMPRESSED, operation="compress")
    assert event.metadata == {"rewritten": True}


def test_raising_extension_fails_open() -> None:
    """The fail-open contract: an extension that raises is logged and
    SKIPPED — the emit completes. This is the path that protects
    compress() from user hooks."""
    raiser = _RaisingExtension()
    manager = PipelineExtensionManager(hooks=raiser)

    event = manager.emit(PipelineStage.INPUT_ROUTED, operation="compress")

    assert raiser.calls == 1, "the raising extension was actually invoked"
    assert event.stage is PipelineStage.INPUT_ROUTED, "emit returns despite the raise"


def test_hooks_object_with_handler_is_resolved_as_extension() -> None:
    recorder = _RecordingExtension()
    manager = PipelineExtensionManager(hooks=recorder)
    assert manager.enabled
    manager.emit(PipelineStage.INPUT_RECEIVED, operation="compress")
    assert len(recorder.events) == 1


def test_hooks_object_without_handler_is_ignored() -> None:
    manager = PipelineExtensionManager(hooks=object())
    assert not manager.enabled


def test_summarize_routing_markers_filters_router_prefix() -> None:
    transforms = ["cache_aligner", "router:smart_crusher:0.12", "router:log:0.4", "dedup"]
    assert summarize_routing_markers(transforms) == [
        "router:smart_crusher:0.12",
        "router:log:0.4",
    ]
