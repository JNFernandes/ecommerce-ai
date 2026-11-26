"""OpenTelemetry SDK setup: the scrubbing exporter wrapper (plan D4).

``ScrubbingSpanExporter`` sits between the span processor and the real exporter. A finished span
is immutable, so the wrapper rebuilds each one with only the allowlisted attributes
(``telemetry.attributes.ALLOWED_KEYS``), no events, links without attributes, a status without
description and a resource reduced to ``service.*``. Exporting fails open: any exporter error
becomes ``FAILURE`` and a warning naming its class, at most once per ``warn_interval_seconds``.
"""

import threading
import time
from collections.abc import Callable, Sequence
from typing import Final

import structlog
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Status

from ecommerce_ai.telemetry.attributes import ALLOWED_KEYS, RESOURCE_KEYS

_logger = structlog.get_logger(__name__)

WARN_INTERVAL_SECONDS: Final = 60.0
EXPORT_FAILED: Final = "ExportFailed"
"""``error_type`` logged when the wrapped exporter reports failure without raising."""


def scrub_resource(resource: Resource) -> Resource:
    """Return ``resource`` with only the ``service.name``/``service.version`` attributes."""
    kept = {key: value for key, value in resource.attributes.items() if key in RESOURCE_KEYS}
    return Resource(kept)


def scrub_span(span: ReadableSpan) -> ReadableSpan:
    """Rebuild ``span`` with allowlisted attributes only, no events and no free text."""
    attributes = {
        key: value for key, value in (span.attributes or {}).items() if key in ALLOWED_KEYS
    }
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=scrub_resource(span.resource),
        attributes=attributes,
        events=(),
        links=tuple(Link(link.context) for link in span.links),
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class ScrubbingSpanExporter(SpanExporter):
    """Scrub every span before ``inner`` sees it; never let an exporter error escape."""

    def __init__(
        self,
        inner: SpanExporter,
        *,
        clock: Callable[[], float] = time.monotonic,
        warn_interval_seconds: float = WARN_INTERVAL_SECONDS,
    ) -> None:
        """Wrap ``inner``; ``clock`` and ``warn_interval_seconds`` pace the failure warning."""
        self._inner = inner
        self._clock = clock
        self._warn_interval = warn_interval_seconds
        self._last_warning: float | None = None
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Export the scrubbed spans; an error or a failed export returns ``FAILURE``."""
        try:
            result = self._inner.export([scrub_span(span) for span in spans])
        except Exception as error:
            self._warn("export", type(error).__name__)
            return SpanExportResult.FAILURE
        if result is not SpanExportResult.SUCCESS:
            self._warn("export", EXPORT_FAILED)
        return result

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Flush ``inner``; an error returns ``False``."""
        try:
            return self._inner.force_flush(timeout_millis)
        except Exception as error:
            self._warn("flush", type(error).__name__)
            return False

    def shutdown(self) -> None:
        """Shut ``inner`` down; an error is logged, never raised."""
        try:
            self._inner.shutdown()
        except Exception as error:
            self._warn("shutdown", type(error).__name__)

    def _warn(self, operation: str, error_type: str) -> None:
        now = self._clock()
        with self._lock:
            last = self._last_warning
            if last is not None and now - last < self._warn_interval:
                return
            self._last_warning = now
        _logger.warning("trace_export_failed", operation=operation, error_type=error_type)
