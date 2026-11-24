"""The triage consumer saves each event, then commits its offset and logs one line."""

import asyncio
import io
import json
from collections.abc import Callable, Mapping
from uuid import uuid4

import pytest

from ecommerce_ai.agents.triage_intake.model import TriageCause
from ecommerce_ai.agents.triage_intake.service import TriageIntakeService
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.messaging.consumer_state import ConsumerState
from ecommerce_ai.messaging.contracts import (
    ORDER_CANCELLED_TOPIC,
    PAYMENT_FAILED_TOPIC,
    STOCK_REJECTED_TOPIC,
)
from ecommerce_ai.messaging.kafka import ConsumedRecord, TopicPartition
from ecommerce_ai.messaging.triage_consumer import TriageConsumer
from tests.support.kafka import (
    FakeConsumer,
    make_record,
    order_cancelled_payload,
    payment_failed_payload,
    stock_rejected_payload,
)
from tests.support.polling import wait_until
from tests.support.stores import InMemoryDeadLetterStore, InMemoryTriageCaseStore


class CheckingConsumer(FakeConsumer):
    """Records how many cases were saved at the moment of each commit."""

    def __init__(self, store: InMemoryTriageCaseStore, records: list[ConsumedRecord]) -> None:
        super().__init__(records)
        self.saved_at_commit: list[int] = []
        self._store = store

    async def commit(self, offsets: Mapping[TopicPartition, int]) -> None:
        self.saved_at_commit.append(len(self._store.evidence))
        await super().commit(offsets)


@pytest.fixture
def store() -> InMemoryTriageCaseStore:
    return InMemoryTriageCaseStore()


@pytest.fixture
def state() -> ConsumerState:
    return ConsumerState()


def _triage_consumer(
    fake: FakeConsumer, store: InMemoryTriageCaseStore, state: ConsumerState
) -> TriageConsumer:
    return TriageConsumer(
        lambda: fake,
        TriageIntakeService(store),
        InMemoryDeadLetterStore(),
        state,
        poll_timeout_ms=5,
    )


async def _run_until(consumer: TriageConsumer, condition: Callable[[], bool]) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    await wait_until(lambda: condition() or task.done())
    stop.set()
    await asyncio.wait_for(task, timeout=2)


def _three_orders() -> tuple[list[ConsumedRecord], dict[str, TriageCause]]:
    cancelled, paid, stocked = uuid4(), uuid4(), uuid4()
    records = [
        make_record(ORDER_CANCELLED_TOPIC, order_cancelled_payload(cancelled), offset=0),
        make_record(PAYMENT_FAILED_TOPIC, payment_failed_payload(paid), partition=1, offset=7),
        make_record(STOCK_REJECTED_TOPIC, stock_rejected_payload(stocked), offset=3),
    ]
    causes = {
        str(cancelled): TriageCause.CANCELLATION_REQUESTED,
        str(paid): TriageCause.PAYMENT_FAILED,
        str(stocked): TriageCause.INSUFFICIENT_STOCK,
    }
    return records, causes


async def test_should_record_one_case_per_order_when_each_topic_delivers_an_event(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    records, causes = _three_orders()
    fake = FakeConsumer(records)

    await _run_until(_triage_consumer(fake, store, state), lambda: len(fake.commits) == 3)

    assert {str(case.order_id): case.cause for case in store.cases} == causes
    assert len(store.evidence) == 3
    assert fake.committed == {
        TopicPartition(ORDER_CANCELLED_TOPIC, 0): 1,
        TopicPartition(PAYMENT_FAILED_TOPIC, 1): 8,
        TopicPartition(STOCK_REJECTED_TOPIC, 0): 4,
    }
    assert fake.stopped
    assert (state.connected, state.caught_up, state.last_error) == (False, False, None)


async def test_should_commit_each_offset_only_after_its_event_is_saved(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    records, _ = _three_orders()
    fake = CheckingConsumer(store, records)

    await _run_until(_triage_consumer(fake, store, state), lambda: len(fake.commits) == 3)

    assert fake.saved_at_commit == [1, 2, 3]


async def test_should_not_commit_and_should_stop_when_saving_the_event_fails(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    fake = FakeConsumer([make_record(PAYMENT_FAILED_TOPIC, payment_failed_payload(uuid4()))])
    store.failures.append(ConnectionResetError("database went away"))

    with pytest.raises(ConnectionResetError):
        await _triage_consumer(fake, store, state).run(asyncio.Event())

    assert fake.commits == []
    assert store.cases == []
    assert fake.stopped
    assert (state.connected, state.last_error) == (False, "ConnectionResetError")


async def test_should_report_connected_and_caught_up_while_running(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    fake = FakeConsumer([make_record(PAYMENT_FAILED_TOPIC, payment_failed_payload(uuid4()))])
    stop = asyncio.Event()
    task = asyncio.create_task(_triage_consumer(fake, store, state).run(stop))

    await wait_until(lambda: state.caught_up)

    assert state.connected
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_should_log_one_line_per_message_with_its_coordinates_and_outcome(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    order_id, event_id = uuid4(), uuid4()
    payload = payment_failed_payload(order_id, event_id=event_id, reason="card declined")
    fake = FakeConsumer([make_record(PAYMENT_FAILED_TOPIC, payload, partition=2, offset=41)])
    stream = io.StringIO()
    configure_logging("INFO", stream)

    await _run_until(_triage_consumer(fake, store, state), lambda: len(fake.commits) == 1)

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    (line,) = [line for line in lines if line["event"] == "triage_message"]
    assert line["topic"] == PAYMENT_FAILED_TOPIC
    assert (line["partition"], line["offset"]) == (2, 41)
    assert (line["event_id"], line["order_id"]) == (str(event_id), str(order_id))
    assert line["outcome"] == "processed"
    assert "card declined" not in stream.getvalue()
