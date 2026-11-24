"""A failure event becomes one explained result and one published event (US1 acceptance 1-4)."""

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.prompts.loader import load_prompt
from ecommerce_ai.agents.triage.gatekeeper import check
from ecommerce_ai.agents.triage.graph import TriageLimits, build_triage_graph
from ecommerce_ai.agents.triage.ports import TriageModel
from ecommerce_ai.agents.triage.service import TriageService
from ecommerce_ai.agents.triage.worker import TriageWorker
from ecommerce_ai.agents.triage_intake.model import TriageEventInput
from ecommerce_ai.agents.triage_intake.service import TriageIntakeService
from ecommerce_ai.infrastructure.db.outbox_repository import OutboxRepository
from ecommerce_ai.infrastructure.db.tables import outbox_events, triage_cases, triage_results
from ecommerce_ai.infrastructure.db.token_budget_repository import TokenBudgetRepository
from ecommerce_ai.infrastructure.db.triage_queue_repository import TriageQueueRepository
from ecommerce_ai.infrastructure.db.triage_repository import TriageRepository
from ecommerce_ai.messaging.contracts import (
    ORDER_TRIAGE_COMPLETED_TOPIC,
    PAYMENT_FAILED_TOPIC,
    STOCK_REJECTED_TOPIC,
    OrderTriageCompleted,
)
from ecommerce_ai.messaging.decoding import decode
from ecommerce_ai.messaging.outbox_relay import OutboxRelay, RelayState
from ecommerce_ai.messaging.triage_published import to_outbox_message
from tests.support.kafka import (
    FakeProducer,
    Payload,
    make_record,
    payment_failed_payload,
    stock_rejected_payload,
)
from tests.support.llm import MODEL_ID, ScriptedModel, reply
from tests.support.triage import NOW, FakeClock

SETTLE_SECONDS = 3.0
LIMITS = TriageLimits(
    max_retries=0, case_token_cap=8000, max_output_tokens=1024, backoff_base_seconds=0.01
)
EXPLANATION = "The card was declined, so the order failed."


def _service(engine: AsyncEngine, model: TriageModel | None, clock: FakeClock) -> TriageService:
    graph = build_triage_graph(
        model=model,
        budget=TokenBudgetRepository(engine, daily_budget=1_000_000),
        gatekeeper=check,
        prompt=load_prompt("triage", "v1"),
        limits=LIMITS,
        clock=clock,
    )
    queue = TriageQueueRepository(engine, to_outbox_message)
    return TriageService(queue, graph, clock, settle_seconds=SETTLE_SECONDS)


async def _record(engine: AsyncEngine, topic: str, payload: Payload) -> None:
    decoded = decode(make_record(topic, payload))
    assert isinstance(decoded, TriageEventInput)
    await TriageIntakeService(TriageRepository(engine), clock=lambda: NOW).record(decoded)


async def _rows(engine: AsyncEngine, table: Table) -> list[dict[str, object]]:
    async with engine.connect() as connection:
        return [dict(row._mapping) for row in await connection.execute(select(table))]


async def _run(
    engine: AsyncEngine,
    service: TriageService,
    producer: FakeProducer,
    events: int = 1,
    relay_poll_seconds: float = 5,
) -> None:
    """Run worker and relay until ``events`` outbox rows are RESOLVED, then stop both."""
    stop, wake = asyncio.Event(), asyncio.Event()
    worker = TriageWorker(service, poll_interval_seconds=0.01, wake=wake)
    relay = OutboxRelay(
        OutboxRepository(engine),
        lambda: producer,
        poll_interval_seconds=relay_poll_seconds,
        max_attempts=5,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.01,
        state=RelayState(),
    )
    tasks = [
        asyncio.create_task(worker.run(stop)),
        asyncio.create_task(relay.run(stop, wake)),
    ]

    async def all_resolved() -> bool:
        rows = await _rows(engine, outbox_events)
        return sum(row["status"] == "RESOLVED" for row in rows) == events

    try:
        async with asyncio.timeout(10):
            while not await all_resolved():
                assert not any(task.done() for task in tasks), "a background task ended"
                await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)


async def test_should_explain_a_failed_order_and_publish_one_event_when_its_case_settles(
    db_engine: AsyncEngine,
) -> None:
    order_id = uuid4()
    await _record(db_engine, PAYMENT_FAILED_TOPIC, payment_failed_payload(order_id))
    clock = FakeClock(now=NOW, tick=0.1)
    model = ScriptedModel([reply(EXPLANATION, input_tokens=320, output_tokens=45)])
    service = _service(db_engine, model, clock)

    assert await service.triage_one() is False  # not settled yet
    clock.advance(SETTLE_SECONDS)
    producer = FakeProducer()
    await _run(db_engine, service, producer)

    [case] = await _rows(db_engine, triage_cases)
    [result] = await _rows(db_engine, triage_results)
    [outbox] = await _rows(db_engine, outbox_events)
    assert case["status"] == "COMPLETED"
    assert result["case_id"] == case["id"]
    assert (result["source"], result["model_id"]) == ("LLM", MODEL_ID)
    assert (result["input_tokens"], result["output_tokens"]) == (320, 45)
    assert result["explanation"] == EXPLANATION
    assert outbox["attempts"] == 1
    [record] = producer.sent
    assert record.topic == ORDER_TRIAGE_COMPLETED_TOPIC
    assert record.key == str(order_id).encode()
    event = OrderTriageCompleted.model_validate_json(record.value)
    assert event.event_id == result["event_id"] == outbox["event_id"]
    assert event.order_id == event.aggregate_id == order_id
    assert event.cause.value == "PAYMENT_FAILED"
    assert event.suggested_action.value == "CONTACT_CUSTOMER"
    assert event.explanation == EXPLANATION
    assert event.prompt_version == "triage.v1"
    assert event.model_id == MODEL_ID


async def test_should_publish_one_template_event_per_order_when_no_model_is_configured(
    db_engine: AsyncEngine,
) -> None:
    orders: dict[UUID, str] = {uuid4(): "INSUFFICIENT_STOCK", uuid4(): "UNKNOWN_PRODUCT"}
    reasons = {"INSUFFICIENT_STOCK": "insufficient stock", "UNKNOWN_PRODUCT": "unknown product"}
    for order_id, cause in orders.items():
        await _record(
            db_engine,
            STOCK_REJECTED_TOPIC,
            stock_rejected_payload(order_id, reason=reasons[cause]),
        )
        # A second event for the same order still yields one case and one result.
        await _record(
            db_engine,
            STOCK_REJECTED_TOPIC,
            stock_rejected_payload(order_id, reason=reasons[cause]),
        )
    clock = FakeClock(now=NOW + timedelta(seconds=SETTLE_SECONDS))
    producer = FakeProducer()

    await _run(db_engine, _service(db_engine, None, clock), producer, events=len(orders))

    events = [OrderTriageCompleted.model_validate_json(record.value) for record in producer.sent]
    assert {event.order_id: event.cause.value for event in events} == orders
    assert all(event.source.value == "FALLBACK_TEMPLATE" for event in events)
    assert all(event.model_id is None for event in events)
    assert {record.key for record in producer.sent} == {str(o).encode() for o in orders}
    assert len(await _rows(db_engine, triage_results)) == len(orders)


async def test_should_keep_the_event_pending_while_the_bus_is_down_then_publish_it_once(
    db_engine: AsyncEngine,
) -> None:
    order_id = uuid4()
    await _record(db_engine, PAYMENT_FAILED_TOPIC, payment_failed_payload(order_id))
    clock = FakeClock(now=NOW + timedelta(seconds=SETTLE_SECONDS))
    service = _service(db_engine, None, clock)
    producer = FakeProducer()
    producer.failures.extend([ConnectionError("down"), ConnectionError("down")])

    assert await service.triage_one() is True
    [case] = await _rows(db_engine, triage_cases)
    [pending] = await _rows(db_engine, outbox_events)
    assert case["status"] == "COMPLETED"
    assert pending["status"] == "PENDING"

    await _run(db_engine, service, producer, relay_poll_seconds=0.02)

    [outbox] = await _rows(db_engine, outbox_events)
    [result] = await _rows(db_engine, triage_results)
    assert outbox["attempts"] == 3
    [record] = producer.sent
    event = OrderTriageCompleted.model_validate_json(record.value)
    assert event.event_id == pending["event_id"] == outbox["event_id"] == result["event_id"]
