"""OpenTelemetry SDK setup: exporter selection (D3, D5) and the scrubbing exporter wrapper (D4).

``build_tracer_provider`` reads the standard ``OTEL_TRACES_EXPORTER`` itself, defaulting to
``none`` (the SDK's own default is ``otlp``): nothing leaves the process unless asked. ``console``
prints spans to stdout, ``otlp`` sends them over OTLP/HTTP (endpoint, headers and timeout from the
standard ``OTEL_EXPORTER_OTLP_*`` variables). ``OTEL_SDK_DISABLED=true`` turns tracing off; an
unknown exporter turns it off with one warning. Spans go through a ``BatchSpanProcessor`` (a
background thread with a bounded queue that drops when full), so exporting never delays a request.

``ScrubbingSpanExporter`` sits between the span processor and the real exporter. A finished span
is immutable, so the wrapper rebuilds each one with only the allowlisted attributes
(``telemetry.attributes.ALLOWED_KEYS``), no events, links without attributes, a status without
description and a resource reduced to ``service.*``. Exporting fails open: any exporter error
becomes ``FAILURE`` and a warning naming its class, at most once per ``warn_interval_seconds``.
"""

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from importlib import metadata
from typing import Final

import structlog
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Link, Status

from ecommerce_ai.telemetry.attributes import ALLOWED_KEYS, RESOURCE_KEYS

_logger = structlog.get_logger(__name__)

SERVICE_NAME: Final = "ecommerce-ai"
DISTRIBUTION: Final = "ecommerce-ai"

type ExporterFactory = Callable[[], SpanExporter]

EXPORTERS: Final[Mapping[str, ExporterFactory]] = {
    "console": ConsoleSpanExporter,
    "otlp": OTLPSpanExporter,
}
"""``OTEL_TRACES_EXPORTER`` values this service supports, besides ``none``."""


def build_tracer_provider(
    env: Mapping[str, str],
    *,
    exporters: Mapping[str, ExporterFactory] = EXPORTERS,
) -> TracerProvider | None:
    """Build the provider ``env`` asks for, or ``None`` when tracing stays off.

    ``OTEL_TRACES_EXPORTER`` may list several exporters separated by commas; each gets its own
    batch processor behind a ``ScrubbingSpanExporter``. ``none`` (the default) or
    ``OTEL_SDK_DISABLED=true`` gives ``None``; any unknown name gives ``None`` and one warning.
    """
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None
    names = [
        name.strip().lower()
        for name in env.get("OTEL_TRACES_EXPORTER", "none").split(",")
        if name.strip()
    ]
    names = [name for name in names if name != "none"]
    if not names:
        return None
    unknown = sorted(name for name in names if name not in exporters)
    if unknown:
        _logger.warning("tracing_disabled", reason="unknown_exporter", exporters=unknown)
        return None
    provider = TracerProvider(resource=_resource(env), shutdown_on_exit=False)
    for name in names:
        provider.add_span_processor(BatchSpanProcessor(ScrubbingSpanExporter(exporters[name]())))
    _logger.info("tracing_enabled", exporters=names)
    return provider


def shutdown_tracer_provider(provider: TracerProvider | None) -> None:
    """Flush and shut ``provider`` down; an exporter that fails or hangs only delays shutdown.

    The scrubbing wrapper already turns exporter errors into a warning; anything raised by the
    SDK itself is logged by class name and not raised, so stopping the app always completes.
    """
    if provider is None:
        return
    try:
        provider.force_flush()
        provider.shutdown()
    except Exception as error:
        _logger.warning("tracing_shutdown_failed", error_type=type(error).__name__)


def _resource(env: Mapping[str, str]) -> Resource:
    try:
        version = metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError:  # pragma: no cover - the project is always installed
        version = "unknown"
    name = env.get("OTEL_SERVICE_NAME", "").strip() or SERVICE_NAME
    return Resource({"service.name": name, "service.version": version})


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
