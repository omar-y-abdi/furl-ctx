"""No-op telemetry shim for the compression pipeline.

The full OpenTelemetry/Langfuse observability stack was removed from this
fork. The compression pipeline (``headroom/transforms/pipeline.py``) still
emits tracing spans and metrics through a small, stable call surface:

- ``get_headroom_tracer().start_as_current_span(name, attributes=...)`` —
  a context manager yielding a span.
- the yielded span supports ``.is_recording()`` and ``.set_attribute(...)``.
- ``get_otel_metrics().record_pipeline_run(**kwargs)``.

These shims provide that exact surface as no-ops. ``is_recording()`` returns
``False``, so the pipeline's attribute-setting branches (already guarded by
``if span is not None and span.is_recording()``) are skipped. The net effect
is byte-for-byte identical to running the pipeline with telemetry disabled.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class _NoopSpan:
    """Span object exposing the subset of the OTel span API the pipeline uses."""

    __slots__ = ()

    def is_recording(self) -> bool:
        return False

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: D401 - no-op
        return None


class _NoopTracer:
    """Tracer exposing ``start_as_current_span`` as a no-op context manager."""

    __slots__ = ()

    @contextmanager
    def start_as_current_span(
        self,
        name: str,
        *,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[_NoopSpan]:
        yield _NoopSpan()


class _NoopMetrics:
    """Metrics recorder exposing ``record_pipeline_run`` as a no-op."""

    __slots__ = ()

    def record_pipeline_run(self, **kwargs: Any) -> None:  # noqa: D401 - no-op
        return None


_TRACER = _NoopTracer()
_METRICS = _NoopMetrics()


def get_headroom_tracer() -> _NoopTracer:
    return _TRACER


def get_otel_metrics() -> _NoopMetrics:
    return _METRICS
