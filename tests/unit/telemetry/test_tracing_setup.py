"""Tracing setup: the scrubber keeps only allowlisted data, exporting fails open, off by default."""

import io
import json
from collections.abc import Sequence

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from ecommerce_ai.infrastructure.health import DependencyStatus
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.tracing import (
    EXPORTERS,
    ScrubbingSpanExporter,
    build_tracer_provider,
    shutdown_tracer_provider,
)
from ecommerce_ai.main import create_app
from ecommerce_ai.telemetry import attributes
from ecommerce_ai.telemetry.attributes import RESOURCE_KEYS
from ecommerce_ai.telemetry.tracing import get_tracer, span, use_provider
from tests.support.probes import FakeProbe
from tests.support.tracing import SpanRecorder


class RaisingExporter(SpanExporter):
    """An exporter whose every operation raises with a message that must never be logged."""

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        raise ConnectionError("collector at http://user:secret-XYZ@collector refused")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        raise TimeoutError("secret-XYZ")

    def shutdown(self) -> None:
        raise RuntimeError("secret-XYZ")


class FailingExporter(SpanExporter):
    """An exporter that reports failure without raising, like OTLP when the collector is down."""

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        """Nothing to release."""


class Clock:
    """A settable monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


_PROBES = [FakeProbe("database", critical=True, status=DependencyStatus.UP)]


def _log_lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _export_one(exporter: SpanExporter) -> None:
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    use_provider(provider)
    try:
        with span("chat.run"):
            pass
    finally:
        use_provider(None)


def test_should_drop_unlisted_attributes_events_link_attributes_and_status_text(
    spans: SpanRecorder,
) -> None:
    with span("tool.call") as linked:
        pass
    with span("chat.run", {attributes.REQUEST_ID: "r-1", "message.text": "hi"}) as current:
        current.set_attribute("tool.arguments", '{"email": "jane@example.com"}')
        current.add_event("exception", {"exception.message": "secret-XYZ"})
        current.add_link(linked.get_span_context(), {"note": "secret-XYZ"})
        current.set_status(StatusCode.ERROR, "bearer eyJ.secret")

    finished = spans.one("chat.run")
    assert dict(finished.attributes or {}) == {attributes.REQUEST_ID: "r-1"}
    assert not finished.events
    assert [dict(link.attributes or {}) for link in finished.links] == [{}]
    assert finished.status.status_code is StatusCode.ERROR
    assert not finished.status.description
    assert set(finished.resource.attributes) <= RESOURCE_KEYS
    assert "secret" not in finished.to_json()


def test_should_keep_the_span_identity_and_timing(spans: SpanRecorder) -> None:
    with span("chat.run") as parent, span("node.agent"):
        pass

    child = spans.one("node.agent")
    assert child.parent is not None
    assert child.parent.span_id == parent.get_span_context().span_id
    assert child.start_time is not None
    assert child.end_time is not None
    assert child.end_time >= child.start_time


def test_should_return_failure_without_raising_when_the_exporter_raises() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    scrubber = ScrubbingSpanExporter(RaisingExporter())

    _export_one(scrubber)

    assert scrubber.export([]) is SpanExportResult.FAILURE
    assert scrubber.force_flush() is False
    scrubber.shutdown()
    assert "secret" not in stream.getvalue()
    (line,) = _log_lines(stream)  # one warning per minute, whatever failed
    assert line["event"] == "trace_export_failed"
    assert line["error_type"] == "ConnectionError"


def test_should_warn_at_most_once_per_interval() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    clock = Clock()
    scrubber = ScrubbingSpanExporter(FailingExporter(), clock=clock, warn_interval_seconds=60)

    scrubber.export([])
    clock.now = 59.0
    scrubber.export([])
    clock.now = 60.0
    scrubber.export([])

    lines = _log_lines(stream)
    assert [line["error_type"] for line in lines] == ["ExportFailed", "ExportFailed"]


def test_should_pass_through_a_successful_export() -> None:
    memory = InMemorySpanExporter()
    scrubber = ScrubbingSpanExporter(memory)

    _export_one(scrubber)

    assert scrubber.force_flush() is True
    assert [s.name for s in memory.get_finished_spans()] == ["chat.run"]


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"OTEL_TRACES_EXPORTER": "none"},
        {"OTEL_TRACES_EXPORTER": " , "},
        {"OTEL_TRACES_EXPORTER": "console", "OTEL_SDK_DISABLED": "TRUE"},
    ],
)
def test_should_build_no_provider_when_tracing_is_off(env: dict[str, str]) -> None:
    assert build_tracer_provider(env) is None


def test_should_build_no_provider_and_warn_once_for_an_unknown_exporter() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)

    assert build_tracer_provider({"OTEL_TRACES_EXPORTER": "console,zipkin"}) is None

    (line,) = _log_lines(stream)
    assert line["event"] == "tracing_disabled"
    assert line["exporters"] == ["zipkin"]


def test_should_scrub_every_selected_exporter() -> None:
    first, second = InMemorySpanExporter(), InMemorySpanExporter()
    factories = {"console": lambda: first, "otlp": lambda: second}

    provider = build_tracer_provider(
        {"OTEL_TRACES_EXPORTER": "Console, otlp", "OTEL_SERVICE_NAME": "ai-test"},
        exporters=factories,
    )
    assert provider is not None
    use_provider(provider)
    try:
        with span("chat.run", {"message.text": "hi"}):
            pass
    finally:
        use_provider(None)
    shutdown_tracer_provider(provider)

    for memory in (first, second):
        (finished,) = memory.get_finished_spans()
        assert not finished.attributes
        assert finished.resource.attributes["service.name"] == "ai-test"
        assert set(finished.resource.attributes) == RESOURCE_KEYS


@pytest.mark.parametrize("name", sorted(EXPORTERS))
def test_should_build_the_real_exporters(name: str) -> None:
    provider = build_tracer_provider({"OTEL_TRACES_EXPORTER": name})

    assert provider is not None
    assert provider.resource.attributes["service.name"] == "ecommerce-ai"
    shutdown_tracer_provider(provider)  # no span was recorded: nothing is sent


def test_should_log_and_swallow_a_failing_shutdown() -> None:
    class BrokenProvider(TracerProvider):
        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            raise RuntimeError("secret-XYZ")

    stream = io.StringIO()
    configure_logging("INFO", stream)

    shutdown_tracer_provider(None)
    shutdown_tracer_provider(BrokenProvider(shutdown_on_exit=False))

    (line,) = _log_lines(stream)
    assert line["event"] == "tracing_shutdown_failed"
    assert line["error_type"] == "RuntimeError"


async def test_should_trace_nothing_when_the_app_uses_the_default_environment() -> None:
    app = create_app(probes=_PROBES)

    async with app.router.lifespan_context(app):
        with span("chat.run") as current:
            assert not current.is_recording()
    assert get_tracer() is get_tracer()


async def test_should_flush_but_keep_an_injected_provider_when_the_app_stops(
    spans: SpanRecorder,
) -> None:
    app = create_app(
        probes=_PROBES,
        tracer_provider=spans.provider,
    )

    async with app.router.lifespan_context(app):
        with span("chat.run"):
            pass
    with span("chat.resume"):
        pass

    assert spans.names() == ["chat.run", "chat.resume"]
