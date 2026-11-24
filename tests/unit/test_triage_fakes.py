"""The Kafka, model and store fakes behave like the ports they stand in for."""

import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from ecommerce_ai.agents.triage.model import (
    CaseForTriage,
    ResultSource,
    SuggestedAction,
    TriageResult,
)
from ecommerce_ai.agents.triage.ports import (
    ModelUnavailable,
    PromptInput,
    TokenBudget,
    TriageCaseQueue,
    TriageModel,
)
from ecommerce_ai.agents.triage_intake.model import TriageCause, TriageEventInput, TriageStatus
from ecommerce_ai.agents.triage_intake.ports import DeadLetterStore, TriageCaseStore
from ecommerce_ai.infrastructure.outbox import OutboxStatus
from ecommerce_ai.messaging.contracts import TOPIC_MODELS
from ecommerce_ai.messaging.kafka import ConsumerPort, TopicPartition
from ecommerce_ai.messaging.triage_published import to_outbox_message
from tests.support.kafka import TOPIC_PAYLOADS, FakeConsumer, FakeProducer, SentRecord, make_record
from tests.support.llm import MODEL_ID, ScriptedModel, reply
from tests.support.stores import (
    InMemoryDeadLetterStore,
    InMemoryOutboxStore,
    InMemoryTokenBudget,
    InMemoryTriageCaseQueue,
    InMemoryTriageCaseStore,
)

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


PROMPT = PromptInput(system="rules", user="case")


class Boom(Exception):
    """A failure injected into a fake."""


def _case(cause: TriageCause = TriageCause.UNKNOWN) -> CaseForTriage:
    return CaseForTriage(case_id=uuid4(), order_id=uuid4(), cause=cause, evidence=())


def _result() -> TriageResult:
    return TriageResult(
        cause=TriageCause.UNKNOWN,
        explanation="The reason for the failure is not known.",
        suggested_action=SuggestedAction.REVIEW,
        confidence=0.3,
        source=ResultSource.FALLBACK_TEMPLATE,
        prompt_version="triage.v1",
        model_id=None,
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        revisions=0,
        violations=(),
    )


# ScriptedModel


async def test_should_return_replies_and_raise_errors_in_order_when_scripted() -> None:
    model = ScriptedModel([reply(input_tokens=1), ModelUnavailable("timeout"), reply()])
    port: TriageModel = model

    first = await port.draft(PROMPT)
    with pytest.raises(ModelUnavailable):
        await model.draft(PROMPT)
    await model.draft(PROMPT)

    assert first.input_tokens == 1
    assert first.model_id == MODEL_ID
    assert model.calls == 3
    assert model.prompts == [PROMPT, PROMPT, PROMPT]


async def test_should_fail_loudly_when_the_model_is_called_more_than_scripted() -> None:
    model = ScriptedModel()

    with pytest.raises(AssertionError, match="more often than scripted"):
        await model.draft(PROMPT)


# FakeProducer


async def test_should_record_sent_records_and_raise_queued_failures_when_sending() -> None:
    producer = FakeProducer()
    producer.failures.append(Boom())
    await producer.start()

    with pytest.raises(Boom):
        await producer.send("topic", b"key", b"value")
    await producer.send("topic", b"key", b"value")
    await producer.stop()

    assert producer.started
    assert producer.stopped
    assert producer.sent == [SentRecord("topic", b"key", b"value")]


async def test_should_raise_the_configured_errors_when_starting_or_stopping() -> None:
    producer = FakeProducer(start_error=Boom(), stop_error=Boom())

    with pytest.raises(Boom):
        await producer.start()
    with pytest.raises(Boom):
        await producer.stop()

    assert not producer.started
    assert producer.stopped


# InMemoryTriageCaseQueue


async def test_should_claim_the_oldest_settled_case_when_several_are_pending() -> None:
    queue = InMemoryTriageCaseQueue()
    older, newer, unsettled = _case(), _case(), _case()
    queue.add(newer, NOW - timedelta(seconds=5))
    queue.add(older, NOW - timedelta(seconds=10))
    queue.add(unsettled, NOW + timedelta(seconds=1))

    async with queue.next_settled(NOW) as claim:
        assert claim.case == older


async def test_should_claim_nothing_when_no_case_is_settled() -> None:
    queue = InMemoryTriageCaseQueue()
    queue.add(_case(), NOW + timedelta(seconds=1))

    async with queue.next_settled(NOW) as claim:
        assert claim.case is None
        with pytest.raises(RuntimeError):
            await claim.complete(_result())


async def test_should_skip_a_locked_case_when_another_claim_holds_it() -> None:
    queue = InMemoryTriageCaseQueue()
    first, second = _case(), _case()
    queue.add(first, NOW - timedelta(seconds=10))
    queue.add(second, NOW - timedelta(seconds=5))

    async with queue.next_settled(NOW) as outer, queue.next_settled(NOW) as inner:
        assert (outer.case, inner.case) == (first, second)


async def test_should_commit_result_outbox_and_status_together_when_completed() -> None:
    queue = InMemoryTriageCaseQueue(serializer=to_outbox_message)
    port: TriageCaseQueue = queue
    case = _case()
    queue.add(case, NOW - timedelta(seconds=10))

    async with port.next_settled(NOW) as claim:
        await claim.complete(_result())

    assert queue.results == {case.case_id: _result()}
    assert [message.key for message in queue.outbox] == [str(case.order_id)]
    assert queue.status(case.case_id) is TriageStatus.COMPLETED
    assert queue.commits == 1
    async with queue.next_settled(NOW) as claim:
        assert claim.case is None


async def test_should_keep_the_case_pending_when_the_block_raises() -> None:
    queue = InMemoryTriageCaseQueue(serializer=to_outbox_message)
    case = _case()
    queue.add(case, NOW - timedelta(seconds=10))

    with pytest.raises(Boom):
        async with queue.next_settled(NOW) as claim:
            await claim.complete(_result())
            raise Boom

    assert queue.results == {}
    assert queue.outbox == []
    assert queue.status(case.case_id) is TriageStatus.PENDING_TRIAGE
    assert queue.rollbacks == 1
    async with queue.next_settled(NOW) as claim:
        assert claim.case == case


async def test_should_raise_queued_failures_when_claiming_or_completing() -> None:
    queue = InMemoryTriageCaseQueue()
    queue.add(_case(), NOW - timedelta(seconds=10))
    queue.failures.append(Boom())
    queue.complete_failures.append(Boom())

    with pytest.raises(Boom):
        async with queue.next_settled(NOW):
            pass  # pragma: no cover - the failure is raised on entry
    with pytest.raises(Boom):
        async with queue.next_settled(NOW) as claim:
            await claim.complete(_result())

    assert queue.results == {}


# InMemoryTokenBudget


async def test_should_reserve_until_the_budget_is_spent_and_settle_the_actual_spend() -> None:
    budget = InMemoryTokenBudget(budget=100)
    port: TokenBudget = budget
    day = date(2026, 11, 24)

    assert await port.reserve(day, 60)
    assert not await budget.reserve(day, 60)
    await budget.settle(day, reserved=60, actual=10)
    assert await budget.reserve(day, 60)

    assert budget.usage == {day: 70}
    assert [call.granted for call in budget.reserves] == [True, False, True]
    assert [(call.reserved, call.actual) for call in budget.settles] == [(60, 10)]


async def test_should_keep_each_day_separate_and_raise_queued_failures() -> None:
    budget = InMemoryTokenBudget(budget=50)
    budget.failures.append(Boom())

    with pytest.raises(Boom):
        await budget.reserve(date(2026, 11, 24), 10)
    assert await budget.reserve(date(2026, 11, 24), 50)
    assert await budget.reserve(date(2026, 11, 25), 50)


# InMemoryOutboxStore


async def test_should_claim_the_earliest_due_pending_event_when_several_exist() -> None:
    store = InMemoryOutboxStore()
    early = to_outbox_message(_result(), _case())
    late = to_outbox_message(_result(), _case())
    future = to_outbox_message(_result(), _case())
    store.add(late, NOW - timedelta(seconds=1))
    store.add(early, NOW - timedelta(seconds=5))
    store.add(future, NOW + timedelta(seconds=5))

    async with store.claim_due(NOW) as first, store.claim_due(NOW) as second:
        assert first.event is not None
        assert second.event is not None
        assert (first.event.message, second.event.message) == (early, late)
    async with store.claim_due(NOW) as third:
        assert third.event is not None  # nothing was decided: both are still due


async def test_should_apply_the_outcome_when_the_claim_commits() -> None:
    store = InMemoryOutboxStore()
    resolved = to_outbox_message(_result(), _case())
    failed = to_outbox_message(_result(), _case())
    exhausted = to_outbox_message(_result(), _case())
    for offset, message in enumerate((resolved, failed, exhausted)):
        store.add(message, NOW - timedelta(seconds=10 - offset))
    retry_at = NOW + timedelta(seconds=30)

    async with store.claim_due(NOW) as claim:
        await claim.resolve(NOW)
    async with store.claim_due(NOW) as claim:
        await claim.fail("KafkaConnectionError", retry_at, NOW)
    async with store.claim_due(NOW) as claim:
        await claim.exhaust("KafkaConnectionError", NOW)

    assert store.event(resolved.event_id).status is OutboxStatus.RESOLVED
    assert store.event(failed.event_id).status is OutboxStatus.PENDING
    assert store.event(failed.event_id).next_attempt_at == retry_at
    assert store.event(failed.event_id).last_error == "KafkaConnectionError"
    assert store.event(exhausted.event_id).status is OutboxStatus.EXHAUSTED
    assert [event.attempts for event in store.events] == [1, 1, 1]
    assert store.commits == 3


async def test_should_change_nothing_when_the_claim_raises_or_nothing_is_due() -> None:
    store = InMemoryOutboxStore()
    message = to_outbox_message(_result(), _case())
    store.add(message, NOW)
    store.failures.append(Boom())

    with pytest.raises(Boom):
        async with store.claim_due(NOW):
            pass  # pragma: no cover - the failure is raised on entry
    with pytest.raises(Boom):
        async with store.claim_due(NOW) as claim:
            await claim.resolve(NOW)
            raise Boom
    async with store.claim_due(NOW - timedelta(seconds=1)) as claim:
        assert claim.event is None
        with pytest.raises(RuntimeError):
            await claim.resolve(NOW)

    assert store.event(message.event_id).status is OutboxStatus.PENDING
    assert store.rollbacks == 1
