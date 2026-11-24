"""Consumed failure events become triage cases in PostgreSQL (US1 acceptance 1-4)."""

import asyncio
from collections.abc import Callable
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.triage_intake.service import TriageIntakeService
from ecommerce_ai.infrastructure.db.dead_letter_repository import DeadLetterRepository
from ecommerce_ai.infrastructure.db.tables import triage_cases, triage_evidence
from ecommerce_ai.infrastructure.db.triage_repository import TriageRepository
from ecommerce_ai.messaging.consumer_state import ConsumerState
from ecommerce_ai.messaging.contracts import (
    ORDER_CANCELLED_TOPIC,
    PAYMENT_FAILED_TOPIC,
    STOCK_REJECTED_TOPIC,
)
from ecommerce_ai.messaging.kafka import TopicPartition
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
