"""Tracing test support: record finished spans in memory, through the production scrubber.

``recording_spans()`` builds an SDK provider whose only processor is a ``SimpleSpanProcessor``
(synchronous, so a span is visible as soon as it ends) over ``ScrubbingSpanExporter`` and an
``InMemorySpanExporter``, makes it current with ``use_provider`` and restores the no-op tracer on
exit. Pass ``recorder.provider`` to ``create_app(tracer_provider=...)`` so the app keeps it.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ecommerce_ai.infrastructure.tracing import ScrubbingSpanExporter
from ecommerce_ai.telemetry.tracing import use_provider


@dataclass(frozen=True)
class SpanRecorder:
    """A provider and the in-memory exporter behind its scrubber."""

    provider: TracerProvider
    exporter: InMemorySpanExporter

    @property
    def spans(self) -> tuple[ReadableSpan, ...]:
        """Every finished (scrubbed) span, in the order they ended."""
        return tuple(self.exporter.get_finished_spans())

    def names(self) -> list[str]:
        """The names of every finished span, in the order they ended."""
        return [span.name for span in self.spans]

    def spans_by_name(self, name: str) -> list[ReadableSpan]:
        """Every finished span called ``name``."""
        return [span for span in self.spans if span.name == name]

    def one(self, name: str) -> ReadableSpan:
        """The single finished span called ``name``; fail when there are none or several."""
        found = self.spans_by_name(name)
        assert len(found) == 1, f"expected one {name!r} span, got {len(found)}: {self.names()}"
        return found[0]

    def clear(self) -> None:
        """Forget every span recorded so far."""
        self.exporter.clear()


def assert_parent(child: ReadableSpan, parent: ReadableSpan) -> None:
    """Fail unless ``child`` is a direct child of ``parent`` in the same trace."""
    assert child.parent is not None, f"{child.name!r} has no parent"
    assert parent.context is not None
    assert child.parent.trace_id == parent.context.trace_id, f"{child.name!r} is in another trace"
    assert child.parent.span_id == parent.context.span_id, (
        f"{child.name!r} is not a child of {parent.name!r}"
    )


@contextmanager
def recording_spans() -> Iterator[SpanRecorder]:
    """Record every span ended inside the block; tracing is off again afterwards."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(exporter)))
    use_provider(provider)
    try:
        yield SpanRecorder(provider, exporter)
    finally:
        use_provider(None)
        provider.shutdown()
