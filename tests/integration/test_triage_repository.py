"""The PostgreSQL triage repository creates, locks and updates cases atomically."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.triage_intake.model import TriageCause, TriageEventInput
from ecommerce_ai.agents.triage_intake.ports import TriageCaseStore
from ecommerce_ai.infrastructure.db.tables import triage_cases, triage_evidence
from ecommerce_ai.infrastructure.db.triage_repository import TriageRepository

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _event(order_id: UUID, event_id: UUID | None = None) -> TriageEventInput:
    return TriageEventInput(
        topic="payments.payment-failed",
        event_id=event_id or uuid4(),
        order_id=order_id,
        reason="card declined",
        occurred_at=NOW - timedelta(minutes=1),
        payload={"orderId": str(order_id), "reason": "card declined", "items": [1, 2]},
    )


@pytest.fixture
def repository(db_engine: AsyncEngine) -> TriageRepository:
    return TriageRepository(db_engine)


async def _cases(engine: AsyncEngine) -> list[dict[str, object]]:
    async with engine.connect() as connection:
        rows = await connection.execute(select(triage_cases))
        return [dict(row._mapping) for row in rows]


async def _evidence(engine: AsyncEngine) -> list[dict[str, object]]:
    async with engine.connect() as connection:
        rows = await connection.execute(select(triage_evidence))
        return [dict(row._mapping) for row in rows]


def test_should_implement_the_store_port(repository: TriageRepository) -> None:
    store: TriageCaseStore = repository

    assert store is repository


async def test_should_create_a_case_and_its_evidence_when_the_order_is_new(
    repository: TriageRepository, db_engine: AsyncEngine
) -> None:
    order_id = uuid4()
    event = _event(order_id)

    async with repository.transaction() as work:
        case = await work.get_or_create_locked_case(order_id, TriageCause.PAYMENT_FAILED, NOW)
        added = await work.add_evidence(case.case_id, event, NOW)

    assert added is True
    assert case.cause is TriageCause.PAYMENT_FAILED
    (stored_case,) = await _cases(db_engine)
    assert stored_case["id"] == case.case_id
    assert stored_case["order_id"] == order_id
    assert stored_case["status"] == "PENDING_TRIAGE"
    assert stored_case["created_at"] == stored_case["updated_at"] == NOW
    (evidence,) = await _evidence(db_engine)
    assert evidence["case_id"] == case.case_id
    assert evidence["event_id"] == event.event_id
    assert evidence["source_topic"] == event.topic
    assert evidence["reason"] == "card declined"
    assert evidence["occurred_at"] == event.occurred_at
    assert evidence["received_at"] == NOW
    assert evidence["payload"] == event.payload


async def test_should_return_the_existing_case_with_its_cause_when_the_order_has_one(
    repository: TriageRepository, db_engine: AsyncEngine
) -> None:
    order_id = uuid4()
    async with repository.transaction() as work:
        first = await work.get_or_create_locked_case(order_id, TriageCause.UNKNOWN, NOW)

    async with repository.transaction() as work:
        second = await work.get_or_create_locked_case(
            order_id, TriageCause.PAYMENT_FAILED, NOW + timedelta(seconds=1)
        )

    assert second == first
    assert second.cause is TriageCause.UNKNOWN
    assert len(await _cases(db_engine)) == 1


async def test_should_report_a_duplicate_when_the_event_id_is_already_stored(
    repository: TriageRepository, db_engine: AsyncEngine
) -> None:
    order_id = uuid4()
    event = _event(order_id)
    async with repository.transaction() as work:
        case = await work.get_or_create_locked_case(order_id, TriageCause.PAYMENT_FAILED, NOW)
        assert await work.add_evidence(case.case_id, event, NOW) is True

    async with repository.transaction() as work:
        assert await work.add_evidence(case.case_id, event, NOW) is False

    assert len(await _evidence(db_engine)) == 1


async def test_should_update_cause_and_updated_at_when_asked(
    repository: TriageRepository, db_engine: AsyncEngine
) -> None:
    order_id = uuid4()
    later = NOW + timedelta(minutes=5)
    async with repository.transaction() as work:
        case = await work.get_or_create_locked_case(order_id, TriageCause.UNKNOWN, NOW)

    async with repository.transaction() as work:
        await work.update_case(case.case_id, TriageCause.INSUFFICIENT_STOCK, later)

    (stored_case,) = await _cases(db_engine)
    assert stored_case["cause"] == "INSUFFICIENT_STOCK"
    assert stored_case["created_at"] == NOW
    assert stored_case["updated_at"] == later


async def test_should_save_nothing_when_the_transaction_raises(
    repository: TriageRepository, db_engine: AsyncEngine
) -> None:
    order_id = uuid4()

    with pytest.raises(RuntimeError, match="boom"):
        async with repository.transaction() as work:
            case = await work.get_or_create_locked_case(order_id, TriageCause.UNKNOWN, NOW)
            await work.add_evidence(case.case_id, _event(order_id), NOW)
            raise RuntimeError("boom")

    assert await _cases(db_engine) == []
    assert await _evidence(db_engine) == []
