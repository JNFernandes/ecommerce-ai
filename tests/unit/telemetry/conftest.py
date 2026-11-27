"""Fixtures for the tracing tests: a span recorder behind the production scrubber."""

from collections.abc import Iterator

import pytest

from tests.support.tracing import SpanRecorder, recording_spans


@pytest.fixture
def spans() -> Iterator[SpanRecorder]:
    """Record the spans a test produces; tracing is off again after it."""
    with recording_spans() as recorder:
        yield recorder
