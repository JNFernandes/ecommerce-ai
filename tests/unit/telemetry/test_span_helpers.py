"""Telemetry helpers: ``span`` records failures by class name, ``traced_node`` names nodes."""

import inspect
from collections.abc import Iterator

import pytest
from langgraph.errors import GraphInterrupt
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from ecommerce_ai.telemetry import attributes
from ecommerce_ai.telemetry.nodes import traced_node
from ecommerce_ai.telemetry.tracing import get_tracer, span, use_provider


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    use_provider(provider)
    yield memory
    use_provider(None)
    provider.shutdown()


def test_should_use_a_no_op_tracer_when_no_provider_is_in_use() -> None:
    use_provider(None)

    with span("chat.run", {attributes.REQUEST_ID: "r-1"}) as current:
        assert not current.is_recording()
    assert get_tracer() is get_tracer()


def test_should_set_attributes_and_skip_none_values(exporter: InMemorySpanExporter) -> None:
    with span("chat.run", {attributes.REQUEST_ID: "r-1", attributes.USER_HASH: None}):
        pass

    (finished,) = exporter.get_finished_spans()
    assert finished.name == "chat.run"
    assert dict(finished.attributes or {}) == {attributes.REQUEST_ID: "r-1"}


def test_should_record_only_the_error_class_when_the_block_raises(
    exporter: InMemorySpanExporter,
) -> None:
    with pytest.raises(ValueError, match="secret-XYZ"), span("tool.call"):
        raise ValueError("secret-XYZ")

    (finished,) = exporter.get_finished_spans()
    assert finished.attributes is not None
    assert finished.attributes[attributes.ERROR_TYPE] == "ValueError"
    assert finished.status.status_code is StatusCode.ERROR
    assert not finished.status.description
    assert not finished.events


def test_should_not_mark_an_ignored_exception_as_an_error(exporter: InMemorySpanExporter) -> None:
    with pytest.raises(KeyError), span("node.approval", ignore=(KeyError,)):
        raise KeyError("x")

    (finished,) = exporter.get_finished_spans()
    assert attributes.ERROR_TYPE not in (finished.attributes or {})
    assert finished.status.status_code is StatusCode.UNSET


def test_should_wrap_a_sync_node_in_a_named_span(exporter: InMemorySpanExporter) -> None:
    def classify(state: dict[str, int]) -> dict[str, int]:
        return {"seen": state["n"]}

    wrapped = traced_node("classify", classify)

    assert wrapped({"n": 3}) == {"seen": 3}
    (finished,) = exporter.get_finished_spans()
    assert finished.name == "node.classify"
    assert (finished.attributes or {})[attributes.NODE_NAME] == "classify"


async def test_should_keep_an_async_node_async_with_its_signature(
    exporter: InMemorySpanExporter,
) -> None:
    async def agent(state: dict[str, int], runtime: object) -> dict[str, int]:
        return {"seen": state["n"]}

    wrapped = traced_node("agent", agent)

    assert inspect.iscoroutinefunction(wrapped)
    assert list(inspect.signature(wrapped).parameters) == ["state", "runtime"]
    assert await wrapped({"n": 1}, object()) == {"seen": 1}
    assert [s.name for s in exporter.get_finished_spans()] == ["node.agent"]


async def test_should_not_mark_a_graph_interrupt_as_an_error(
    exporter: InMemorySpanExporter,
) -> None:
    async def approval(state: dict[str, int]) -> dict[str, int]:
        raise GraphInterrupt(())

    with pytest.raises(GraphInterrupt):
        await traced_node("approval", approval)({})

    (finished,) = exporter.get_finished_spans()
    assert attributes.ERROR_TYPE not in (finished.attributes or {})


def test_should_allow_every_key_constant() -> None:
    keys = {
        value
        for name, value in vars(attributes).items()
        if name.isupper() and isinstance(value, str)
    }

    assert keys == attributes.ALLOWED_KEYS
