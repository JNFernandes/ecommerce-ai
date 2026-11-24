"""The PostgreSQL triage queue locks settled cases and saves result, outbox and status together."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Table, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.triage.model import (
    GateRule,
    ResultSource,
    SuggestedAction,
    TriageResult,
    Violation,
)
from ecommerce_ai.agents.triage.ports import TriageCaseQueue
from ecommerce_ai.agents.triage_intake.model import TriageCause, TriageEventInput
from ecommerce_ai.infrastructure.db.tables import outbox_events, triage_cases, triage_results
from ecommerce_ai.infrastructure.db.triage_queue_repository import TriageQueueRepository
from ecommerce_ai.infrastructure.db.triage_repository import TriageRepository
from ecommerce_ai.messaging.triage_published import to_outbox_message

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
SETTLED = NOW - timedelta(seconds=3)


async def _add_case(
    engine: AsyncEngine,
    updated_at: datetime,
    *,
    events: int = 1,
    cause: TriageCause = TriageCause.PAYMENT_FAILED,
) -> UUID:
    """Create a case through the intake repository; return its order id."""
    order_id = uuid4()
    async with TriageRepository(engine).transaction() as work:
        case = await work.get_or_create_locked_case(order_id, cause, updated_at)
        for index in range(events):
            await work.add_evidence(
                case.case_id,
                TriageEventInput(
                    topic="payments.payment-failed",
                    event_id=uuid4(),
                    order_id=order_id,
                    reason=f"card declined {index}",
                    occurred_at=updated_at - timedelta(minutes=events - index),
                    payload={"orderId": str(order_id), "attempt": index},
                ),
                updated_at,
            )
    return order_id


def _result(source: ResultSource = ResultSource.LLM) -> TriageResult:
    llm = source is ResultSource.LLM
    return TriageResult(
        cause=TriageCause.PAYMENT_FAILED,
        explanation="The card was declined, so the order failed.",
        suggested_action=SuggestedAction.CONTACT_CUSTOMER,
        confidence=0.85,
        source=source,
        prompt_version="triage.v1",
        model_id="claude-test-model" if llm else None,
        input_tokens=300,
        output_tokens=40,
        latency_ms=1200,
        revisions=1,
        violations=(Violation(rule=GateRule.PROMISE, message="No promises.", attempt=1),),
    )


@pytest.fixture
def queue(db_engine: AsyncEngine) -> TriageQueueRepository:
    return TriageQueueRepository(db_engine, to_outbox_message)


async def _rows(engine: AsyncEngine, table: Table) -> list[dict[str, object]]:
    async with engine.connect() as connection:
        rows = await connection.execute(select(table))
        return [dict(row._mapping) for row in rows]


async def _status(engine: AsyncEngine, order_id: UUID) -> str:
    async with engine.connect() as connection:
        status = await connection.scalar(
            select(triage_cases.c.status).where(triage_cases.c.order_id == order_id)
        )
    assert isinstance(status, str)
    return status


def test_should_implement_the_case_queue_port(queue: TriageQueueRepository) -> None:
    port: TriageCaseQueue = queue

    assert port is queue


async def test_should_claim_no_case_when_none_has_settled(
    queue: TriageQueueRepository, db_engine: AsyncEngine
) -> None:
    await _add_case(db_engine, SETTLED + timedelta(milliseconds=1))

    async with queue.next_settled(SETTLED) as claim:
        assert claim.case is None


async def test_should_claim_the_oldest_settled_case_with_its_evidence_oldest_first(
    queue: TriageQueueRepository, db_engine: AsyncEngine
) -> None:
    await _add_case(db_engine, SETTLED)
    oldest = await _add_case(db_engine, SETTLED - timedelta(seconds=10), events=3)

    async with queue.next_settled(SETTLED) as claim:
        case = claim.case

    assert case is not None
    assert case.order_id == oldest
    assert case.cause is TriageCause.PAYMENT_FAILED
    assert [item.reason for item in case.evidence] == [
        "card declined 0",
        "card declined 1",
        "card declined 2",
    ]
    assert case.evidence[0].payload == {"orderId": str(oldest), "attempt": 0}
    assert case.evidence[0].topic == "payments.payment-failed"


async def test_should_save_result_outbox_and_completed_status_when_the_claim_completes(
    queue: TriageQueueRepository, db_engine: AsyncEngine
) -> None:
    order_id = await _add_case(db_engine, SETTLED)

    async with queue.next_settled(SETTLED) as claim:
        await claim.complete(_result())

    assert await _status(db_engine, order_id) == "COMPLETED"
    [result] = await _rows(db_engine, triage_results)
    [event] = await _rows(db_engine, outbox_events)
    assert result["event_id"] == event["event_id"]
    assert result["source"] == "LLM"
    assert result["model_id"] == "claude-test-model"
    assert result["revisions"] == 1
    assert result["violations"] == [{"rule": "PROMISE", "message": "No promises.", "attempt": 1}]
    assert event["status"] == "PENDING"
    assert event["attempts"] == 0
    assert event["topic"] == "ai.order-triage-completed"
    assert event["message_key"] == str(order_id)
    assert event["next_attempt_at"] == result["created_at"] == event["created_at"]
    payload = event["payload"]
    assert isinstance(payload, dict)
    assert payload["eventId"] == str(event["event_id"])
    assert payload["orderId"] == str(order_id)
    assert payload["modelId"] == "claude-test-model"


async def test_should_not_claim_a_case_again_when_it_is_completed(
    queue: TriageQueueRepository, db_engine: AsyncEngine
) -> None:
    await _add_case(db_engine, SETTLED)
    async with queue.next_settled(SETTLED) as claim:
        await claim.complete(_result(ResultSource.FALLBACK_TEMPLATE))

    async with queue.next_settled(NOW) as claim:
        assert claim.case is None


async def test_should_reject_a_second_result_for_the_same_case(
    queue: TriageQueueRepository, db_engine: AsyncEngine
) -> None:
    order_id = await _add_case(db_engine, SETTLED)

    with pytest.raises(IntegrityError):
        async with queue.next_settled(SETTLED) as claim:
            await claim.complete(_result())
            await claim.complete(_result())

    assert await _status(db_engine, order_id) == "PENDING_TRIAGE"
    assert await _rows(db_engine, triage_results) == []


async def test_should_roll_back_everything_and_keep_the_case_pending_when_the_run_fails(
    queue: TriageQueueRepository, db_engine: AsyncEngine
) -> None:
    order_id = await _add_case(db_engine, SETTLED)

    with pytest.raises(RuntimeError, match="crash"):
        async with queue.next_settled(SETTLED) as claim:
            await claim.complete(_result())
            raise RuntimeError("crash after saving")

    assert await _status(db_engine, order_id) == "PENDING_TRIAGE"
    assert await _rows(db_engine, triage_results) == []
    assert await _rows(db_engine, outbox_events) == []


async def test_should_skip_a_locked_case_when_another_claim_holds_it(
    queue: TriageQueueRepository, db_engine: AsyncEngine
) -> None:
    first = await _add_case(db_engine, SETTLED - timedelta(seconds=1))
    second = await _add_case(db_engine, SETTLED)

    async with (
        queue.next_settled(SETTLED) as held,
        queue.next_settled(SETTLED) as other,
        queue.next_settled(SETTLED) as none_left,
    ):
        claimed = (held.case, other.case, none_left.case)

    assert [case.order_id if case else None for case in claimed] == [first, second, None]


async def test_should_refuse_to_complete_when_no_case_was_claimed(
    queue: TriageQueueRepository,
) -> None:
    with pytest.raises(RuntimeError, match="no case"):
        async with queue.next_settled(SETTLED) as claim:
            await claim.complete(_result())
