"""The service's tracer and a ``span`` helper that records failures by class name only (D2, D7).

The provider is a module reference set by ``use_provider`` (the composition root or a test), not
the OpenTelemetry global, which can be set only once per process. Without a provider the API's
no-op tracer is used, so instrumented code costs next to nothing.
"""

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Final

from opentelemetry import trace
from opentelemetry.trace import Span, StatusCode, Tracer, TracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import AttributeValue

from ecommerce_ai.telemetry.attributes import ERROR_TYPE

TRACER_NAME: Final = "ecommerce_ai"

type Attributes = Mapping[str, AttributeValue | None]
"""Span attributes; a ``None`` value is left out."""

_NOOP_TRACER: Final = trace.NoOpTracer()
_PROPAGATOR: Final = TraceContextTextMapPropagator()
_provider: TracerProvider | None = None


def use_provider(provider: TracerProvider | None) -> None:
    """Make ``provider`` the source of every later span; ``None`` turns tracing off."""
    global _provider  # the single, deliberately swappable process tracer (D2)
    _provider = provider


def get_tracer() -> Tracer:
    """Return the service's tracer, or a no-op tracer when no provider is in use."""
    if _provider is None:
        return _NOOP_TRACER
    return _provider.get_tracer(TRACER_NAME)


def set_attributes(target: Span, attributes: Attributes) -> None:
    """Set every attribute whose value is not ``None`` on ``target``."""
    for key, value in attributes.items():
        if value is not None:
            target.set_attribute(key, value)


def mark_ok(target: Span) -> None:
    """Mark ``target`` succeeded; OpenTelemetry treats an ``OK`` status as final."""
    target.set_status(StatusCode.OK)


def record_error(target: Span, error: BaseException) -> None:
    """Mark ``target`` failed with the error's class name only, never its message (D4)."""
    target.set_attribute(ERROR_TYPE, type(error).__name__)
    target.set_status(StatusCode.ERROR)


@contextmanager
def span(
    name: str,
    attributes: Attributes | None = None,
    *,
    ignore: tuple[type[BaseException], ...] = (),
) -> Iterator[Span]:
    """Run the block inside a new current span named ``name``.

    An exception leaving the block sets ``error.type`` to its class name and an error status; no
    exception event is recorded, so its message never reaches the exporter. Exceptions in
    ``ignore`` (control flow, such as a LangGraph interrupt) and a task cancellation (the caller
    went away, which is no failure of the operation) pass through without marking the span.
    """
    with get_tracer().start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as current:
        if attributes:
            set_attributes(current, attributes)
        passing: tuple[type[BaseException], ...] = (asyncio.CancelledError, *ignore)
        try:
            yield current
        except passing:
            raise
        except BaseException as error:
            record_error(current, error)
            raise


def trace_headers() -> dict[str, str]:
    """W3C ``traceparent``/``tracestate`` of the current span; empty when none is recording."""
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier
