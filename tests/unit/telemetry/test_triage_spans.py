"""Triage span trees (plan D7, D8): run, nodes, model calls, consume, and the published headers."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ecommerce_ai.agents.ports import ModelTimeout
from ecommerce_ai.agents.prompts.loader import load_prompt
from ecommerce_ai.agents.triage.gatekeeper import check
from ecommerce_ai.agents.triage.graph import TriageLimits, build_triage_graph
from ecommerce_ai.agents.triage.ports import TriageModel
from ecommerce_ai.agents.triage.service import TriageService
from ecommerce_ai.agents.triage_intake.model import TriageCause
from ecommerce_ai.agents.triage_intake.service import TriageIntakeService
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.messaging.consumer_state import ConsumerState
from ecommerce_ai.messaging.contracts import PAYMENT_FAILED_TOPIC
from ecommerce_ai.messaging.outbox_relay import (
    CORRELATION_ID_HEADER,
    OutboxRelay,
    RelayState,
    encode_payload,
)
from ecommerce_ai.messaging.triage_consumer import TriageConsumer
from ecommerce_ai.messaging.triage_published import to_outbox_message
from ecommerce_ai.telemetry import attributes
from tests.support.kafka import FakeConsumer, FakeProducer, make_record, payment_failed_payload
from tests.support.llm import ScriptedModel, reply
from tests.support.polling import wait_until
from tests.support.stores import (
    InMemoryDeadLetterStore,
    InMemoryOutboxStore,
    InMemoryTokenBudget,
    InMemoryTriageCaseQueue,
    InMemoryTriageCaseStore,
)
from tests.support.tracing import SpanRecorder, assert_parent
from tests.support.triage import NOW, FakeClock, make_case

SETTLE = timedelta(seconds=3)
LIMITS = TriageLimits(
    max_retries=0, case_token_cap=8000, max_output_tokens=1024, backoff_base_seconds=0.01
)
WALL = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def _service(
    queue: InMemoryTriageCaseQueue, model: TriageModel | None, budget: int = 1_000_000
) -> TriageService:
    clock = FakeClock()
    graph = build_triage_graph(
        model=model,
        budget=InMemoryTokenBudget(budget=budget),
        gatekeeper=check,
        prompt=load_prompt("triage", "v1"),
        limits=LIMITS,
        clock=clock,
    )
    return TriageService(queue, graph, clock, settle_seconds=SETTLE.total_seconds())


def _relay(store: InMemoryOutboxStore) -> OutboxRelay:
    return OutboxRelay(
        store,
        FakeProducer,
        poll_interval_seconds=0.5,
        max_attempts=5,
        backoff_base_seconds=1.0,
        backoff_max_seconds=2.0,
        state=RelayState(),
        clock=lambda: WALL,
    )


async def test_should_trace_a_triage_run_with_its_nodes_and_model_call(
    spans: SpanRecorder,
) -> None:
    queue = InMemoryTriageCaseQueue(serializer=to_outbox_message)
    case = make_case(TriageCause.PAYMENT_FAILED)
    queue.add(case, NOW - SETTLE)
    model = ScriptedModel([reply("The card was declined, so the order failed.")])

    assert await _service(queue, model).triage_one() is True

    run = spans.one("triage.run")
    assert run.parent is None
    assert dict(run.attributes or {}) == {
        attributes.CASE_ID: str(case.case_id),
        attributes.ORDER_ID: str(case.order_id),
        attributes.TRIAGE_SOURCE: "LLM",
        attributes.TRIAGE_REVISIONS: 0,
    }
    nodes = [s.name for s in spans.spans if s.name.startswith("node.")]
    assert nodes == ["node.classify", "node.draft", "node.gatekeeper", "node.finalize"]
    for node in [s for s in spans.spans if s.name.startswith("node.")]:
        assert_parent(node, run)
    call = spans.one("llm.call")
    assert_parent(call, spans.one("node.draft"))
    call_attributes = dict(call.attributes or {})
    assert call_attributes[attributes.ORDER_ID] == str(case.order_id)
    assert call_attributes[attributes.LLM_OUTCOME] == "ok"
    assert call_attributes[attributes.PROMPT_VERSION] == "triage.v1"
    assert call_attributes[attributes.MODEL] == "claude-test-model"


async def test_should_mark_a_timed_out_call_and_trace_the_fallback(spans: SpanRecorder) -> None:
    queue = InMemoryTriageCaseQueue()
    case = make_case(TriageCause.PAYMENT_FAILED)
    queue.add(case, NOW - SETTLE)

    await _service(queue, ScriptedModel([ModelTimeout("the model did not answer")])).triage_one()

    call = spans.one("llm.call")
    assert (call.attributes or {})[attributes.LLM_OUTCOME] == "timeout"
    assert (call.attributes or {})[attributes.ERROR_TYPE] == "ModelTimeout"
    assert call.status.description is None
    assert spans.one("node.fallback").status.is_ok
    assert (spans.one("triage.run").attributes or {})[attributes.TRIAGE_SOURCE] == (
        "FALLBACK_TEMPLATE"
    )


async def test_should_trace_a_call_the_budget_refused_without_a_request(
    spans: SpanRecorder,
) -> None:
    queue = InMemoryTriageCaseQueue()
    queue.add(make_case(TriageCause.PAYMENT_FAILED), NOW - SETTLE)
    model = ScriptedModel([reply("The card was declined, so the order failed.")])

    await _service(queue, model, budget=10).triage_one()

    call = spans.one("llm.call")
    assert (call.attributes or {})[attributes.LLM_OUTCOME] == "budget"
    assert attributes.ERROR_TYPE not in (call.attributes or {})
    assert model.calls == 0


async def test_should_publish_with_the_correlation_id_and_trace_context_and_the_same_payload(
    spans: SpanRecorder,
) -> None:
    queue = InMemoryTriageCaseQueue(serializer=to_outbox_message)
    case = make_case(TriageCause.PAYMENT_FAILED)
    queue.add(case, NOW - SETTLE)
    await _service(queue, None).triage_one()
    [message] = queue.outbox
    store, producer = InMemoryOutboxStore(), FakeProducer()
    store.add(message, WALL)

    assert await _relay(store).publish_one(producer) is True

    [record] = producer.sent
    assert record.value == encode_payload(message.payload)
    assert json.loads(record.value) == message.payload
    headers = dict(record.headers)
    assert headers[CORRELATION_ID_HEADER] == str(case.order_id).encode()
    publish = spans.one("outbox.publish")
    assert publish.context is not None
    trace_id = f"{publish.context.trace_id:032x}"
    span_id = f"{publish.context.span_id:016x}"
    assert headers["traceparent"].startswith(f"00-{trace_id}-{span_id}-".encode())
    assert dict(publish.attributes or {}) == {
        attributes.ORDER_ID: str(case.order_id),
        attributes.MESSAGING_DESTINATION: message.topic,
    }


async def test_should_send_only_the_correlation_id_when_tracing_is_off() -> None:
    queue = InMemoryTriageCaseQueue(serializer=to_outbox_message)
    case = make_case(TriageCause.PAYMENT_FAILED)
    queue.add(case, NOW - SETTLE)
    await _service(queue, None).triage_one()
    store, producer = InMemoryOutboxStore(), FakeProducer()
    store.add(queue.outbox[0], WALL)

    await _relay(store).publish_one(producer)

    assert producer.sent[0].headers == ((CORRELATION_ID_HEADER, str(case.order_id).encode()),)


async def test_should_mark_a_failed_publish_by_class_name(spans: SpanRecorder) -> None:
    store, producer = InMemoryOutboxStore(), FakeProducer()
    queue = InMemoryTriageCaseQueue(serializer=to_outbox_message)
    queue.add(make_case(TriageCause.PAYMENT_FAILED), NOW - SETTLE)
    await _service(queue, None).triage_one()
    store.add(queue.outbox[0], WALL)
    producer.failures.append(ConnectionError("broker k1:9092 refused jane@example.com"))

    await _relay(store).publish_one(producer)

    publish = spans.one("outbox.publish")
    assert (publish.attributes or {})[attributes.ERROR_TYPE] == "ConnectionError"
    assert "jane@example.com" not in publish.to_json()


async def test_should_trace_each_consumed_message_with_its_topic_and_order_id(
    spans: SpanRecorder,
) -> None:
    order_id = uuid4()
    fake = FakeConsumer(
        [
            make_record(PAYMENT_FAILED_TOPIC, b"\xff", offset=0),
            make_record(PAYMENT_FAILED_TOPIC, payment_failed_payload(order_id), offset=1),
        ]
    )
    consumer = TriageConsumer(
        lambda: fake,
        TriageIntakeService(InMemoryTriageCaseStore()),
        InMemoryDeadLetterStore(),
        ConsumerState(),
        Settings(),
        poll_timeout_ms=5,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    await wait_until(lambda: len(fake.commits) == 2 or task.done())
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    dead, processed = spans.spans_by_name("triage.consume")
    assert dict(dead.attributes or {}) == {attributes.MESSAGING_DESTINATION: PAYMENT_FAILED_TOPIC}
    assert dict(processed.attributes or {}) == {
        attributes.MESSAGING_DESTINATION: PAYMENT_FAILED_TOPIC,
        attributes.ORDER_ID: str(order_id),
    }
