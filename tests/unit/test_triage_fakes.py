"""The Kafka and store fakes behave like the ports they stand in for."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ecommerce_ai.agents.triage_intake.model import TriageCause, TriageEventInput
from ecommerce_ai.agents.triage_intake.ports import DeadLetterStore, TriageCaseStore
from ecommerce_ai.messaging.contracts import TOPIC_MODELS
from ecommerce_ai.messaging.kafka import ConsumerPort, TopicPartition
from tests.support.kafka import TOPIC_PAYLOADS, FakeConsumer, make_record
from tests.support.stores import InMemoryDeadLetterStore, InMemoryTriageCaseStore

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _event() -> TriageEventInput:
    return TriageEventInput(
        topic="payments.payment-failed",
        event_id=uuid4(),
        order_id=uuid4(),
        reason="card declined",
        occurred_at=NOW,
        payload={"reason": "card declined"},
    )


@pytest.mark.parametrize("topic", sorted(TOPIC_PAYLOADS))
def test_should_build_a_payload_the_topic_model_accepts(topic: str) -> None:
    order_id = uuid4()
    record = make_record(topic, TOPIC_PAYLOADS[topic](order_id), offset=4)

    assert record.value is not None
    event = TOPIC_MODELS[topic].model_validate_json(record.value)

    assert event.order_key == order_id
    assert json.loads(record.value)["version"] == 1


async def test_should_hand_out_records_one_at_a_time_and_track_offsets() -> None:
    first = make_record("payments.payment-failed", b"a", offset=5)
    second = make_record("payments.payment-failed", b"b", offset=6)
    consumer: ConsumerPort = FakeConsumer([first, second])
    tp = TopicPartition("payments.payment-failed", 0)

    assert await consumer.getmany(timeout_ms=10) == {tp: [first]}
    assert (consumer.highwater(tp), await consumer.position(tp)) == (7, 6)
    await consumer.commit({tp: 6})
    assert await consumer.getmany(timeout_ms=10) == {tp: [second]}
    assert await consumer.getmany(timeout_ms=1) == {}
    assert consumer.assignment() == {tp}


async def test_should_raise_the_configured_failures_and_report_commits() -> None:
    consumer = FakeConsumer(start_error=OSError("down"), stop_error=RuntimeError("late"))
    tp = TopicPartition("t", 0)

    with pytest.raises(OSError, match="down"):
        await consumer.start()
    with pytest.raises(RuntimeError, match="late"):
        await consumer.stop()
    await consumer.commit({tp: 1})
    await consumer.commit({tp: 2})

    assert (consumer.started, consumer.stopped) == (False, True)
    assert consumer.committed == {tp: 2}
    assert (consumer.pending, await consumer.position(tp), consumer.highwater(tp)) == (0, 0, None)


async def test_should_keep_changes_only_when_the_transaction_succeeds() -> None:
    store = InMemoryTriageCaseStore()
    port: TriageCaseStore = store
    event = _event()

    with pytest.raises(RuntimeError):
        async with port.transaction() as tx:
            await tx.get_or_create_locked_case(event.order_id, TriageCause.PAYMENT_FAILED, NOW)
            raise RuntimeError
    assert (store.cases, store.rollbacks) == ([], 1)

    async with port.transaction() as tx:
        case = await tx.get_or_create_locked_case(event.order_id, TriageCause.UNKNOWN, NOW)
        assert await tx.add_evidence(case.case_id, event, NOW) is True
        assert await tx.add_evidence(case.case_id, event, NOW) is False
        await tx.update_case(case.case_id, TriageCause.PAYMENT_FAILED, NOW)
        again = await tx.get_or_create_locked_case(event.order_id, TriageCause.UNKNOWN, NOW)

    assert again == case.model_copy(update={"cause": TriageCause.PAYMENT_FAILED})
    assert store.case_for(event.order_id).cause is TriageCause.PAYMENT_FAILED
    assert [item.event_id for item in store.evidence] == [event.event_id]
    assert store.commits == 1


async def test_should_raise_queued_failures_before_touching_the_stores() -> None:
    cases = InMemoryTriageCaseStore()
    dead_letters = InMemoryDeadLetterStore()
    port: DeadLetterStore = dead_letters
    cases.failures.append(TimeoutError())
    dead_letters.failures.append(OSError())

    with pytest.raises(TimeoutError):
        async with cases.transaction():
            pytest.fail("the failure is raised before the block runs")
    with pytest.raises(OSError):
        await port.add("t", 0, 1, None, b"x", "invalid JSON")
    await port.add("t", 0, 1, b"k", b"x", "invalid JSON")

    assert [(row.key, row.reason) for row in dead_letters.rows] == [(b"k", "invalid JSON")]
