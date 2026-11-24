"""Consumed failure events become triage cases in PostgreSQL (US1 acceptance 1-4)."""

import asyncio
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.triage_intake.model import IntakeOutcome, TriageEventInput
from ecommerce_ai.agents.triage_intake.service import TriageIntakeService
from ecommerce_ai.infrastructure.db.dead_letter_repository import DeadLetterRepository
from ecommerce_ai.infrastructure.db.tables import (
    inbound_dead_letters,
    triage_cases,
    triage_evidence,
)
from ecommerce_ai.infrastructure.db.triage_repository import TriageRepository
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.messaging.consumer_state import ConsumerState
from ecommerce_ai.messaging.contracts import (
    ORDER_CANCELLED_TOPIC,
    PAYMENT_FAILED_TOPIC,
    STOCK_REJECTED_TOPIC,
)
from ecommerce_ai.messaging.decoding import decode
from ecommerce_ai.messaging.kafka import ConsumedRecord, TopicPartition
from ecommerce_ai.messaging.triage_consumer import TriageConsumer
from tests.support.kafka import (
    OCCURRED_AT,
    FakeConsumer,
    make_record,
    order_cancelled_payload,
    payment_failed_payload,
    stock_rejected_payload,
)
from tests.support.polling import wait_until


async def _consume(engine: AsyncEngine, fake: FakeConsumer, done: Callable[[], bool]) -> None:
    consumer = TriageConsumer(
        lambda: fake,
        TriageIntakeService(TriageRepository(engine)),
        DeadLetterRepository(engine),
        ConsumerState(),
        Settings(),
        poll_timeout_ms=5,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    await wait_until(lambda: done() or task.done(), timeout=10)
    stop.set()
    await asyncio.wait_for(task, timeout=5)


async def _cases(engine: AsyncEngine) -> dict[UUID, tuple[str, str, int]]:
    """Cause, status and evidence count per order."""
    query = (
        select(
            triage_cases.c.order_id,
            triage_cases.c.cause,
            triage_cases.c.status,
            func.count(triage_evidence.c.id),
        )
        .join(triage_evidence, triage_evidence.c.case_id == triage_cases.c.id)
        .group_by(triage_cases.c.id)
    )
    async with engine.connect() as connection:
        rows = await connection.execute(query)
        return {row[0]: (row[1], row[2], row[3]) for row in rows}


async def test_should_create_one_pending_case_per_order_when_each_topic_delivers_a_failure(
    db_engine: AsyncEngine,
) -> None:
    paid, stocked, unknown, cancelled = uuid4(), uuid4(), uuid4(), uuid4()
    fake = FakeConsumer(
        [
            make_record(PAYMENT_FAILED_TOPIC, payment_failed_payload(paid), offset=0),
            make_record(STOCK_REJECTED_TOPIC, stock_rejected_payload(stocked), offset=0),
            make_record(
                STOCK_REJECTED_TOPIC,
                stock_rejected_payload(unknown, reason="unknown product and insufficient stock"),
                offset=1,
            ),
            make_record(ORDER_CANCELLED_TOPIC, order_cancelled_payload(cancelled), offset=5),
        ]
    )

    await _consume(db_engine, fake, lambda: len(fake.commits) == 4)

    assert await _cases(db_engine) == {
        paid: ("PAYMENT_FAILED", "PENDING_TRIAGE", 1),
        stocked: ("INSUFFICIENT_STOCK", "PENDING_TRIAGE", 1),
        unknown: ("UNKNOWN_PRODUCT", "PENDING_TRIAGE", 1),
        cancelled: ("CANCELLATION_REQUESTED", "PENDING_TRIAGE", 1),
    }
    assert fake.committed == {
        TopicPartition(PAYMENT_FAILED_TOPIC, 0): 1,
        TopicPartition(STOCK_REJECTED_TOPIC, 0): 2,
        TopicPartition(ORDER_CANCELLED_TOPIC, 0): 6,
    }


async def test_should_store_the_event_as_evidence_when_a_payment_failure_is_saved(
    db_engine: AsyncEngine,
) -> None:
    order_id, event_id = uuid4(), uuid4()
    payload = payment_failed_payload(order_id, event_id=event_id, reason="card declined")
    fake = FakeConsumer([make_record(PAYMENT_FAILED_TOPIC, payload)])

    await _consume(db_engine, fake, lambda: len(fake.commits) == 1)

    async with db_engine.connect() as connection:
        (evidence,) = (await connection.execute(select(triage_evidence))).mappings().all()
    assert evidence["event_id"] == event_id
    assert evidence["source_topic"] == PAYMENT_FAILED_TOPIC
    assert evidence["reason"] == "card declined"
    assert evidence["occurred_at"] == OCCURRED_AT
    assert evidence["received_at"].utcoffset().total_seconds() == 0
    assert evidence["payload"]["orderId"] == str(order_id)
    assert "failedAt" not in evidence["payload"]


async def test_should_store_evidence_but_leave_a_completed_case_unchanged(
    db_engine: AsyncEngine,
) -> None:
    order_id = uuid4()
    intake = _intake(db_engine)
    await intake.record(_cancelled(order_id))
    async with db_engine.begin() as connection:
        await connection.execute(
            update(triage_cases)
            .where(triage_cases.c.order_id == order_id)
            .values(status="COMPLETED")
        )
    the_case = select(triage_cases).where(triage_cases.c.order_id == order_id)
    async with db_engine.connect() as connection:
        before = (await connection.execute(the_case)).one()

    outcome = await intake.record(_payment_failed(order_id))

    assert outcome is IntakeOutcome.PROCESSED
    async with db_engine.connect() as connection:
        after = (await connection.execute(the_case)).one()
    assert after == before
    assert after.status == "COMPLETED"
    assert after.cause == "CANCELLATION_REQUESTED"
    assert (await _cases(db_engine))[order_id][2] == 2


def _intake(engine: AsyncEngine) -> TriageIntakeService:
    return TriageIntakeService(TriageRepository(engine))


def _input(record: ConsumedRecord) -> TriageEventInput:
    decoded = decode(record)
    assert isinstance(decoded, TriageEventInput)
    return decoded


def _cancelled(order_id: UUID, reason: str | None = None) -> TriageEventInput:
    return _input(
        make_record(ORDER_CANCELLED_TOPIC, order_cancelled_payload(order_id, reason=reason))
    )


def _payment_failed(order_id: UUID) -> TriageEventInput:
    return _input(make_record(PAYMENT_FAILED_TOPIC, payment_failed_payload(order_id)))


def _stock_rejected(order_id: UUID, reason: str = "insufficient stock") -> TriageEventInput:
    return _input(
        make_record(STOCK_REJECTED_TOPIC, stock_rejected_payload(order_id, reason=reason))
    )


FAILURES: dict[str, Callable[[UUID], TriageEventInput]] = {
    "PAYMENT_FAILED": _payment_failed,
    "INSUFFICIENT_STOCK": _stock_rejected,
    "UNKNOWN_PRODUCT": lambda order_id: _stock_rejected(order_id, "unknown product"),
}


@pytest.mark.parametrize("cancellation_first", [True, False], ids=["cancel-first", "fail-first"])
@pytest.mark.parametrize("cause", sorted(FAILURES))
async def test_should_keep_one_case_with_the_failure_cause_in_any_arrival_order(
    db_engine: AsyncEngine, cause: str, cancellation_first: bool
) -> None:
    order_id = uuid4()
    events = [_cancelled(order_id), FAILURES[cause](order_id)]
    if not cancellation_first:
        events.reverse()

    for event in events:
        await _intake(db_engine).record(event)

    assert await _cases(db_engine) == {order_id: (cause, "PENDING_TRIAGE", 2)}


@pytest.mark.parametrize(
    ("first", "second", "cause"),
    [
        (_payment_failed, _stock_rejected, "PAYMENT_FAILED"),
        (_stock_rejected, _payment_failed, "INSUFFICIENT_STOCK"),
        (
            lambda order_id: _cancelled(order_id, "payment failed: card declined"),
            _stock_rejected,
            "PAYMENT_FAILED",
        ),
    ],
    ids=["payment-then-stock", "stock-then-payment", "cancelled-payment-then-stock"],
)
async def test_should_keep_the_first_failure_cause_when_two_failures_arrive(
    db_engine: AsyncEngine,
    first: Callable[[UUID], TriageEventInput],
    second: Callable[[UUID], TriageEventInput],
    cause: str,
) -> None:
    order_id = uuid4()

    await _intake(db_engine).record(first(order_id))
    await _intake(db_engine).record(second(order_id))

    assert await _cases(db_engine) == {order_id: (cause, "PENDING_TRIAGE", 2)}


async def test_should_create_one_case_when_two_events_for_a_new_order_race(
    db_engine: AsyncEngine,
) -> None:
    order_id = uuid4()
    intake = _intake(db_engine)

    await asyncio.gather(
        intake.record(_cancelled(order_id)), intake.record(_payment_failed(order_id))
    )

    assert await _cases(db_engine) == {order_id: ("PAYMENT_FAILED", "PENDING_TRIAGE", 2)}


async def _snapshot(engine: AsyncEngine) -> tuple[list[dict[str, object]], ...]:
    async with engine.connect() as connection:
        cases = (await connection.execute(select(triage_cases))).mappings().all()
        evidence = (await connection.execute(select(triage_evidence))).mappings().all()
    return [dict(row) for row in cases], [dict(row) for row in evidence]


async def test_should_leave_the_data_as_after_one_delivery_when_an_event_is_redelivered(
    db_engine: AsyncEngine,
) -> None:
    event = _payment_failed(uuid4())
    intake = _intake(db_engine)
    await intake.record(event)
    once = await _snapshot(db_engine)

    outcome = await intake.record(event)

    assert outcome is IntakeOutcome.DUPLICATE
    assert await _snapshot(db_engine) == once


async def test_should_store_one_evidence_when_the_same_event_is_saved_concurrently(
    db_engine: AsyncEngine,
) -> None:
    order_id = uuid4()
    event = _payment_failed(order_id)
    intake = _intake(db_engine)

    outcomes = await asyncio.gather(*(intake.record(event) for _ in range(3)))

    assert sorted(outcomes) == [
        IntakeOutcome.DUPLICATE,
        IntakeOutcome.DUPLICATE,
        IntakeOutcome.PROCESSED,
    ]
    assert await _cases(db_engine) == {order_id: ("PAYMENT_FAILED", "PENDING_TRIAGE", 1)}


async def test_should_store_a_dead_letter_and_a_case_when_a_bad_message_precedes_a_good_one(
    db_engine: AsyncEngine,
) -> None:
    order_id = uuid4()
    fake = FakeConsumer(
        [
            make_record(STOCK_REJECTED_TOPIC, b"\xff\xfe", offset=0),
            make_record(STOCK_REJECTED_TOPIC, stock_rejected_payload(order_id), offset=1),
        ]
    )

    await _consume(db_engine, fake, lambda: len(fake.commits) == 2)

    async with db_engine.connect() as connection:
        (dead_letter,) = (await connection.execute(select(inbound_dead_letters))).mappings().all()
    assert dead_letter["raw_payload"] == b"\xff\xfe"
    assert dead_letter["offset"] == 0
    assert await _cases(db_engine) == {order_id: ("INSUFFICIENT_STOCK", "PENDING_TRIAGE", 1)}
    assert fake.committed == {TopicPartition(STOCK_REJECTED_TOPIC, 0): 2}
