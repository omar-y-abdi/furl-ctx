"""Furl compression lifecycle stages and the extension contract.

Only the three stages the standalone ``compress()`` path actually emits survive
here — ``INPUT_RECEIVED`` → ``INPUT_ROUTED`` → ``INPUT_COMPRESSED``. The former
send/response/proxy stages and the entry-point extension-discovery path were
removed with the proxy in the standalone excise; ``compress.py`` is the sole
emitter and passes ``hooks`` directly, so nothing discovers or consumes them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class PipelineStage(str, Enum):
    """The compression lifecycle stages emitted by ``compress()``."""

    INPUT_RECEIVED = "input_received"
    INPUT_ROUTED = "input_routed"
    INPUT_COMPRESSED = "input_compressed"


@dataclass(frozen=True)
class PipelineEvent:
    """Immutable event emitted at a canonical pipeline stage.

    The event is frozen: extensions MUST NOT mutate it in place. To change
    ``messages`` or ``metadata``, return a replacement ``PipelineEvent`` from
    ``on_pipeline_event`` (e.g. via
    ``dataclasses.replace(event, messages=...)``); ``emit`` adopts whatever the
    handler returns. This keeps the prompt-cache-sensitive ``messages`` list free
    of the hidden in-place effects the project's RULES.md forbids.
    """

    stage: PipelineStage
    operation: str
    model: str = ""
    messages: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def summarize_routing_markers(transforms_applied: list[str]) -> list[str]:
    """Return the routed transform markers emitted by ContentRouter."""

    return [item for item in transforms_applied if item.startswith("router:")]


class PipelineExtensionManager:
    """Dispatch canonical pipeline events to configured hooks."""

    def __init__(self, *, hooks: Any = None) -> None:
        self._hook = hooks if hasattr(hooks, "on_pipeline_event") else None

    @property
    def enabled(self) -> bool:
        return self._hook is not None

    def emit(
        self,
        stage: PipelineStage,
        *,
        operation: str,
        model: str = "",
        messages: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PipelineEvent:
        event = PipelineEvent(stage, operation, model, messages, metadata or {})
        if self._hook:
            try:
                updated = self._hook.on_pipeline_event(event)
                if isinstance(updated, PipelineEvent):
                    event = updated
            except Exception as e:
                log.warning("hook failed during %s: %s", stage.value, e)
        return event
