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
from typing import Any, Protocol

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
    ``messages``, ``tools``, ``headers``, or ``metadata``, return a replacement
    ``PipelineEvent`` from ``on_pipeline_event`` (e.g. via
    ``dataclasses.replace(event, messages=...)``); ``emit`` adopts whatever the
    handler returns. This keeps the prompt-cache-sensitive ``messages`` list free
    of the hidden in-place effects the project's RULES.md forbids.
    """

    stage: PipelineStage
    operation: str
    request_id: str = ""
    provider: str = ""
    model: str = ""
    messages: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    headers: dict[str, str] | None = None
    response: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


class PipelineExtension(Protocol):
    """Request lifecycle extension contract for the canonical pipeline."""

    def on_pipeline_event(self, event: PipelineEvent) -> PipelineEvent | None:
        """Handle a canonical pipeline event."""


def summarize_routing_markers(transforms_applied: list[str]) -> list[str]:
    """Return the routed transform markers emitted by ContentRouter."""

    return [item for item in transforms_applied if item.startswith("router:")]


class PipelineExtensionManager:
    """Dispatch canonical pipeline events to configured extensions."""

    def __init__(
        self,
        *,
        hooks: Any = None,
        extensions: list[Any] | None = None,
    ) -> None:
        resolved: list[Any] = []
        if hooks is not None and callable(getattr(hooks, "on_pipeline_event", None)):
            resolved.append(hooks)
        if extensions:
            resolved.extend(extensions)
        self._extensions = resolved

    @property
    def enabled(self) -> bool:
        return bool(self._extensions)

    def emit(
        self,
        stage: PipelineStage,
        *,
        operation: str,
        request_id: str = "",
        provider: str = "",
        model: str = "",
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
        response: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> PipelineEvent:
        """Emit a canonical lifecycle event and return the final event state."""

        event = PipelineEvent(
            stage=stage,
            operation=operation,
            request_id=request_id,
            provider=provider,
            model=model,
            messages=messages,
            tools=tools,
            headers=headers,
            response=response,
            metadata=metadata or {},
        )

        for extension in self._extensions:
            handler = getattr(extension, "on_pipeline_event", None)
            if not callable(handler):
                continue
            try:
                updated = handler(event)
            except Exception as exc:  # noqa: BLE001 - preserve hook fail-open behavior
                log.warning(
                    "pipeline extension %r failed during %s: %s",
                    type(extension).__name__,
                    stage.value,
                    exc,
                )
                continue
            if isinstance(updated, PipelineEvent):
                event = updated

        return event
