"""Furl compression lifecycle stages and the extension contract."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class PipelineStage(str, Enum):
    """The compression lifecycle stages emitted by compress()."""

    INPUT_RECEIVED = "input_received"
    INPUT_ROUTED = "input_routed"
    INPUT_COMPRESSED = "input_compressed"


@dataclass(frozen=True)
class PipelineEvent:
    """Immutable event emitted at a canonical pipeline stage."""

    stage: PipelineStage
    operation: str
    model: str = ""
    messages: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class PipelineExtensionManager:
    """Dispatch canonical pipeline events to configured extensions."""

    def __init__(self, *, hooks: Any = None, extensions: list[Any] | None = None) -> None:
        self._extensions: list[Any] = []
        if hooks is not None and callable(getattr(hooks, "on_pipeline_event", None)):
            self._extensions.append(hooks)
        if extensions:
            self._extensions.extend(extensions)

    @property
    def enabled(self) -> bool:
        return bool(self._extensions)

    def emit(
        self,
        stage: PipelineStage,
        *,
        operation: str,
        model: str = "",
        messages: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PipelineEvent:
        """Emit a canonical lifecycle event and return the final event state."""
        event = PipelineEvent(
            stage=stage,
            operation=operation,
            model=model,
            messages=messages,
            metadata=metadata or {},
        )

        for extension in self._extensions:
            handler = getattr(extension, "on_pipeline_event", None)
            if not callable(handler):
                continue
            try:
                updated = handler(event)
                if isinstance(updated, PipelineEvent):
                    event = updated
            except Exception as exc:
                log.warning(
                    "pipeline extension %r failed during %s: %s",
                    type(extension).__name__,
                    stage.value,
                    exc,
                )
        return event
