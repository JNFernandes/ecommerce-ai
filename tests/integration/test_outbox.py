"""The PostgreSQL outbox store claims due PENDING events and records their outcome."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.infrastructure.db.outbox_repository import OutboxRepository
from ecommerce_ai.infrastructure.db.tables import outbox_events
from ecommerce_ai.infrastructure.outbox import OutboxStatus
from ecommerce_ai.messaging.outbox_relay import OutboxRelay, OutboxStore
from tests.support.kafka import FakeProducer

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
PAYLOAD = {"eventId": "x", "orderId": "order-1", "confidence": 0.9, "modelId": None}


async def _add(
    engine: AsyncEngine,
    *,
    next_attempt_at: datetime = NOW,
    status: OutboxStatus = OutboxStatus.PENDING,
    key: str = "order-1",
) -> UUID:
    event_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            outbox_events.insert().values(
                id=uuid4(),
                event_id=event_id,
                topic="ai.order-triage-completed",
                message_key=key,
                payload=PAYLOAD,
                status=status.value,
                attempts=0,
                next_attempt_at=next_attempt_at,
                last_error=None,
                created_at=NOW - timedelta(minutes=1),
                updated_at=NOW - timedelta(minutes=1),
            )
        )
    return event_id


async def _row(engine: AsyncEngine, event_id: UUID) -> dict[str, object]:
    query = select(outbox_events).where(outbox_events.c.event_id == event_id)
    async with engine.connect() as connection:
        row = (await connection.execute(query)).one()
    return dict(row._mapping)


@pytest.fixture
def store(db_engine: AsyncEngine) -> OutboxRepository:
    return OutboxRepository(db_engine)


def test_should_implement_the_outbox_store_port(store: OutboxRepository) -> None:
    port: OutboxStore = store

    assert port is store


async def test_should_claim_the_earliest_due_pending_event_with_its_stored_message(
    store: OutboxRepository, db_engine: AsyncEngine
) -> None:
    await _add(db_engine, next_attempt_at=NOW + timedelta(seconds=1))
    await _add(db_engine, status=OutboxStatus.RESOLVED)
    later = await _add(db_engine, next_attempt_at=NOW, key="order-2")
    earliest = await _add(db_engine, next_attempt_at=NOW - timedelta(seconds=5))

    async with store.claim_due(NOW) as first, store.claim_due(NOW) as second:
        claimed = (first.event, second.event)
        async with store.claim_due(NOW) as third:
            assert third.event is None

    assert [event.message.event_id if event else None for event in claimed] == [earliest, later]
    assert claimed[1] is not None
    message = claimed[1].message
    assert (message.topic, message.key, message.payload) == (
        "ai.order-triage-completed",
        "order-2",
        PAYLOAD,
    )
    assert message.occurred_at == NOW - timedelta(minutes=1)
    assert claimed[1].attempts == 0


async def test_should_mark_the_event_resolved_when_resolved(
    store: OutboxRepository, db_engine: AsyncEngine
) -> None:
    event_id = await _add(db_engine)

    async with store.claim_due(NOW) as claim:
        await claim.resolve(NOW)

    row = await _row(db_engine, event_id)
    assert row["status"] == "RESOLVED"
    assert row["attempts"] == 1
    assert row["updated_at"] == NOW
    async with store.claim_due(NOW) as claim:
        assert claim.event is None


async def test_should_refuse_to_resolve_when_nothing_was_claimed(store: OutboxRepository) -> None:
    with pytest.raises(RuntimeError, match="no outbox event"):
        async with store.claim_due(NOW) as claim:
            await claim.resolve(NOW)


async def test_should_publish_and_resolve_a_stored_event_through_the_relay(
    store: OutboxRepository, db_engine: AsyncEngine
) -> None:
    event_id = await _add(db_engine)
    producer = FakeProducer()
    relay = OutboxRelay(store, lambda: producer, poll_interval_seconds=1, clock=lambda: NOW)

    assert await relay.publish_one(producer) is True
    assert await relay.publish_one(producer) is False

    [record] = producer.sent
    assert record.key == b"order-1"
    assert record.value == (b'{"confidence":0.9,"eventId":"x","modelId":null,"orderId":"order-1"}')
    assert (await _row(db_engine, event_id))["status"] == "RESOLVED"
