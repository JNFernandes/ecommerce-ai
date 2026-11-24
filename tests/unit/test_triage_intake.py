"""The intake service records one case per order with one evidence item per event."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from ecommerce_ai.agents.triage_intake.model import (
    IntakeOutcome,
    TriageCause,
    TriageEventInput,
    TriageStatus,
)
from ecommerce_ai.agents.triage_intake.service import TriageIntakeService
from ecommerce_ai.messaging.contracts import (
    ORDER_CANCELLED_TOPIC,
    PAYMENT_FAILED_TOPIC,
    STOCK_REJECTED_TOPIC,
)
from tests.support.stores import InMemoryTriageCaseStore

RECEIVED_AT = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
OCCURRED_AT = datetime(2026, 11, 24, 11, 59, tzinfo=UTC)


class FakeClock:
    """A clock that returns ``now`` and moves only when told to."""

    def __init__(self, now: datetime = RECEIVED_AT) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _event(topic: str, order_id: UUID, reason: str | None) -> TriageEventInput:
    return TriageEventInput(
        topic=topic,
        event_id=uuid4(),
        order_id=order_id,
        reason=reason,
        occurred_at=OCCURRED_AT,
        payload={"orderId": str(order_id), "reason": reason},
    )


@pytest.fixture
def store() -> InMemoryTriageCaseStore:
    return InMemoryTriageCaseStore()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def service(store: InMemoryTriageCaseStore, clock: FakeClock) -> TriageIntakeService:
    return TriageIntakeService(store, clock)


@pytest.mark.parametrize(
    ("topic", "reason", "cause"),
    [
        (PAYMENT_FAILED_TOPIC, "card declined", TriageCause.PAYMENT_FAILED),
        (STOCK_REJECTED_TOPIC, "unknown product", TriageCause.UNKNOWN_PRODUCT),
        (ORDER_CANCELLED_TOPIC, None, TriageCause.CANCELLATION_REQUESTED),
    ],
)
async def test_should_create_a_pending_case_with_one_evidence_when_the_first_event_arrives(
    service: TriageIntakeService,
    store: InMemoryTriageCaseStore,
    topic: str,
    reason: str | None,
    cause: TriageCause,
) -> None:
    order_id = uuid4()
    event = _event(topic, order_id, reason)

    outcome = await service.record(event)

    assert outcome is IntakeOutcome.PROCESSED
    (case,) = store.cases
    assert (case.order_id, case.cause, case.status) == (
        order_id,
        cause,
        TriageStatus.PENDING_TRIAGE,
    )
    assert case.created_at == case.updated_at == RECEIVED_AT
    (evidence,) = store.evidence
    assert evidence.case_id == case.case_id
    assert evidence.source_topic == topic
    assert evidence.event_id == event.event_id
    assert evidence.reason == reason
    assert evidence.occurred_at == OCCURRED_AT
    assert evidence.received_at == RECEIVED_AT
    assert evidence.payload == event.payload
    assert store.commits == 1


async def test_should_save_nothing_when_the_transaction_fails(
    service: TriageIntakeService, store: InMemoryTriageCaseStore
) -> None:
    store.failures.append(ConnectionResetError("database went away"))

    with pytest.raises(ConnectionResetError):
        await service.record(_event(PAYMENT_FAILED_TOPIC, uuid4(), "card declined"))

    assert store.cases == []
    assert store.evidence == []


async def test_should_stamp_received_at_in_utc_when_the_clock_is_in_another_zone(
    store: InMemoryTriageCaseStore,
) -> None:
    lisbon_summer = datetime(2026, 7, 1, 14, 0, tzinfo=timezone(timedelta(hours=1)))
    service = TriageIntakeService(store, FakeClock(lisbon_summer))

    await service.record(_event(PAYMENT_FAILED_TOPIC, uuid4(), "card declined"))

    (evidence,) = store.evidence
    assert evidence.received_at.utcoffset() == timedelta(0)
    assert evidence.received_at == lisbon_summer


def test_should_use_an_aware_utc_clock_by_default(store: InMemoryTriageCaseStore) -> None:
    now = TriageIntakeService(store).now()

    assert now.tzinfo is UTC


async def test_should_upgrade_the_cause_and_bump_updated_at_when_a_failure_follows_a_cancellation(
    service: TriageIntakeService, store: InMemoryTriageCaseStore, clock: FakeClock
) -> None:
    order_id = uuid4()
    await service.record(_event(ORDER_CANCELLED_TOPIC, order_id, None))
    clock.now = RECEIVED_AT + timedelta(seconds=30)

    outcome = await service.record(_event(PAYMENT_FAILED_TOPIC, order_id, "card declined"))

    assert outcome is IntakeOutcome.PROCESSED
    case = store.case_for(order_id)
    assert case.cause is TriageCause.PAYMENT_FAILED
    assert (case.created_at, case.updated_at) == (RECEIVED_AT, clock.now)
    assert len(store.evidence) == 2


async def test_should_keep_the_cause_but_bump_updated_at_when_a_lower_cause_arrives(
    service: TriageIntakeService, store: InMemoryTriageCaseStore, clock: FakeClock
) -> None:
    order_id = uuid4()
    await service.record(_event(STOCK_REJECTED_TOPIC, order_id, "insufficient stock"))
    clock.now = RECEIVED_AT + timedelta(seconds=30)

    await service.record(_event(ORDER_CANCELLED_TOPIC, order_id, None))
    await service.record(_event(PAYMENT_FAILED_TOPIC, order_id, "card declined"))

    case = store.case_for(order_id)
    assert case.cause is TriageCause.INSUFFICIENT_STOCK
    assert case.updated_at == clock.now
    assert len(store.cases) == 1
    assert {evidence.source_topic for evidence in store.evidence} == {
        STOCK_REJECTED_TOPIC,
        ORDER_CANCELLED_TOPIC,
        PAYMENT_FAILED_TOPIC,
    }


async def test_should_report_a_duplicate_and_change_nothing_when_an_event_is_redelivered(
    service: TriageIntakeService, store: InMemoryTriageCaseStore, clock: FakeClock
) -> None:
    order_id = uuid4()
    event = _event(ORDER_CANCELLED_TOPIC, order_id, None)
    await service.record(event)
    before = (store.cases, store.evidence)
    clock.now = RECEIVED_AT + timedelta(minutes=1)

    outcome = await service.record(event)

    assert outcome is IntakeOutcome.DUPLICATE
    assert (store.cases, store.evidence) == before
    assert (store.commits, store.rollbacks) == (1, 1)


async def test_should_not_create_a_case_when_a_known_event_id_names_another_order(
    service: TriageIntakeService, store: InMemoryTriageCaseStore
) -> None:
    event = _event(PAYMENT_FAILED_TOPIC, uuid4(), "card declined")
    await service.record(event)

    outcome = await service.record(event.model_copy(update={"order_id": uuid4()}))

    assert outcome is IntakeOutcome.DUPLICATE
    assert len(store.cases) == 1
