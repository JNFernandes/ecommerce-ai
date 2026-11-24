"""The outbox relay publishes stored events unchanged, retries with backoff, gives up (US1, US4)."""

import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.outbox import OutboxMessage, OutboxStatus
from ecommerce_ai.messaging.outbox_relay import (
    IdleWait,
    OutboxRelay,
    OutboxStore,
    RelayState,
    encode_payload,
    wait_or_wake,
)
from tests.support.kafka import FakeProducer
from tests.support.polling import wait_until
from tests.support.stores import InMemoryOutboxStore

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
POLL_SECONDS = 0.5
BACKOFF_BASE = 2.0
BACKOFF_MAX = 10.0


def _message(order: str = "order-1") -> OutboxMessage:
    return OutboxMessage(
        event_id=uuid4(),
        topic="ai.order-triage-completed",
        key=order,
        payload={"orderId": order, "explanation": "Café déjà vu.", "confidence": 0.9},
        occurred_at=NOW,
    )


class MutableClock:
    """Wall clock that moves only when ``now`` is reassigned."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class UpperBound:
    """Jitter source that always picks the upper bound."""

    def uniform(self, a: float, b: float) -> float:
        return max(a, b)


class RecordingWait:
    """Records idle waits instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, _stop: asyncio.Event, _wake: asyncio.Event, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


def _relay(
    store: InMemoryOutboxStore,
    producer: FakeProducer,
    wait: IdleWait,
    *,
    max_attempts: int = 5,
    clock: MutableClock | None = None,
    state: RelayState | None = None,
) -> OutboxRelay:
    port: OutboxStore = store
    return OutboxRelay(
        port,
        lambda: producer,
        poll_interval_seconds=POLL_SECONDS,
        max_attempts=max_attempts,
        backoff_base_seconds=BACKOFF_BASE,
        backoff_max_seconds=BACKOFF_MAX,
        state=state or RelayState(),
        clock=clock or MutableClock(),
        wait=wait,
        rng=UpperBound(),
    )


async def _run_until(relay: OutboxRelay, done: object) -> None:
    stop, wake = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(relay.run(stop, wake))
    assert callable(done)
    await wait_until(lambda: bool(done()) or task.done())
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_should_publish_the_stored_payload_key_and_topic_then_resolve_the_event() -> None:
    store, producer, wait = InMemoryOutboxStore(), FakeProducer(), RecordingWait()
    message = _message()
    store.add(message, NOW)

    await _run_until(_relay(store, producer, wait), lambda: wait.delays)

    [record] = producer.sent
    assert record.topic == "ai.order-triage-completed"
    assert record.key == b"order-1"
    assert record.value == encode_payload(message.payload)
    assert json.loads(record.value) == message.payload
    stored = store.event(message.event_id)
    assert stored.status is OutboxStatus.RESOLVED
    assert stored.attempts == 1
    assert stored.updated_at == NOW
    assert producer.started and producer.stopped


async def test_should_publish_every_due_event_before_waiting() -> None:
    store, producer, wait = InMemoryOutboxStore(), FakeProducer(), RecordingWait()
    for order in ("a", "b", "c"):
        store.add(_message(order), NOW)

    await _run_until(_relay(store, producer, wait), lambda: wait.delays)

    assert [record.key for record in producer.sent] == [b"a", b"b", b"c"]
    assert wait.delays[0] == POLL_SECONDS


async def test_should_not_publish_an_event_when_it_is_not_due_yet() -> None:
    store, producer, wait = InMemoryOutboxStore(), FakeProducer(), RecordingWait()
    store.add(_message(), NOW + timedelta(seconds=1))

    await _run_until(_relay(store, producer, wait), lambda: wait.delays)

    assert producer.sent == []


async def test_should_send_the_same_bytes_whatever_the_key_order_of_the_payload() -> None:
    assert encode_payload({"b": 1, "a": "é"}) == encode_payload({"a": "é", "b": 1})
    assert encode_payload({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()


async def test_should_log_one_outbox_publish_line_without_the_payload() -> None:
    store, producer, wait = InMemoryOutboxStore(), FakeProducer(), RecordingWait()
    message = _message()
    store.add(message, NOW)
    relay = _relay(store, producer, wait)
    stream = io.StringIO()
    configure_logging("INFO", stream)

    await relay.publish_one(producer)

    [line] = [json.loads(text) for text in stream.getvalue().splitlines()]
    assert line["event"] == "outbox_publish"
    assert line["event_id"] == str(message.event_id)
    assert line["order_id"] == "order-1"
    assert line["attempt"] == 1
    assert line["outcome"] == "published"
    assert "Café" not in stream.getvalue()


async def test_should_schedule_a_retry_with_backoff_when_the_send_fails() -> None:
    store, producer, state = InMemoryOutboxStore(), FakeProducer(), RelayState()
    message = _message()
    store.add(message, NOW)
    producer.failures.append(ConnectionError("broker down"))

    assert await _relay(store, producer, RecordingWait(), state=state).publish_one(producer)

    stored = store.event(message.event_id)
    assert stored.status is OutboxStatus.PENDING
    assert stored.attempts == 1
    assert stored.next_attempt_at == NOW + timedelta(seconds=BACKOFF_BASE)
    assert stored.last_error == "ConnectionError"
    assert state.last_error == "ConnectionError"
    assert producer.sent == []


@pytest.mark.parametrize(
    ("attempts", "delay"), [(0, 2.0), (1, 4.0), (2, 8.0), (3, BACKOFF_MAX), (60, BACKOFF_MAX)]
)
def test_should_double_the_retry_delay_up_to_the_maximum(attempts: int, delay: float) -> None:
    relay = _relay(InMemoryOutboxStore(), FakeProducer(), RecordingWait(), max_attempts=10)

    assert relay.retry_delay(attempts) == delay


def test_should_keep_the_retry_delay_between_half_and_the_full_cap() -> None:
    relay = OutboxRelay(
        InMemoryOutboxStore(),
        FakeProducer,
        poll_interval_seconds=POLL_SECONDS,
        max_attempts=3,
        backoff_base_seconds=BACKOFF_BASE,
        backoff_max_seconds=BACKOFF_MAX,
        state=RelayState(),
    )

    assert all(1.0 <= relay.retry_delay(0) <= 2.0 for _ in range(50))


async def test_should_publish_the_same_event_later_when_the_bus_comes_back() -> None:
    store, producer, state = InMemoryOutboxStore(), FakeProducer(), RelayState()
    clock = MutableClock()
    message = _message()
    store.add(message, NOW)
    producer.failures.append(ConnectionError("broker down"))
    relay = _relay(store, producer, RecordingWait(), clock=clock, state=state)

    assert await relay.publish_one(producer)
    assert not await relay.publish_one(producer)
    clock.now = NOW + timedelta(seconds=BACKOFF_BASE)
    assert await relay.publish_one(producer)

    [record] = producer.sent
    assert json.loads(record.value) == message.payload
    assert record.key == b"order-1"
    stored = store.event(message.event_id)
    assert (stored.status, stored.attempts, stored.last_error) == (OutboxStatus.RESOLVED, 2, None)
    assert state.last_error is None


async def test_should_exhaust_the_event_and_stop_sending_when_max_attempts_are_reached() -> None:
    store, producer = InMemoryOutboxStore(), FakeProducer()
    clock = MutableClock()
    message = _message()
    store.add(message, NOW)
    producer.failures.extend(ConnectionError("down") for _ in range(5))
    relay = _relay(store, producer, RecordingWait(), max_attempts=2, clock=clock)

    assert await relay.publish_one(producer)
    clock.now = NOW + timedelta(hours=1)
    assert await relay.publish_one(producer)
    assert not await relay.publish_one(producer)

    stored = store.event(message.event_id)
    assert (stored.status, stored.attempts) == (OutboxStatus.EXHAUSTED, 2)
    assert len(producer.failures) == 3


async def test_should_log_one_outbox_publish_line_per_failed_attempt() -> None:
    store, producer = InMemoryOutboxStore(), FakeProducer()
    message = _message()
    store.add(message, NOW)
    producer.failures.append(ConnectionError("kafka://ai:s3cret@broker"))
    stream = io.StringIO()
    configure_logging("INFO", stream)

    await _relay(store, producer, RecordingWait(), max_attempts=1).publish_one(producer)

    [line] = [json.loads(raw) for raw in stream.getvalue().splitlines()]
    assert line["event"] == "outbox_publish"
    assert line["outcome"] == "exhausted"
    assert line["attempt"] == 1
    assert line["error"] == "ConnectionError"
    assert "s3cret" not in stream.getvalue()
    assert "Caf" not in stream.getvalue()


async def test_should_back_off_and_report_when_the_database_is_down() -> None:
    store, producer, state = InMemoryOutboxStore(), FakeProducer(), RelayState()
    store.add(_message(), NOW)
    store.failures.extend([ConnectionRefusedError("db down"), ConnectionRefusedError("db down")])
    wait = RecordingWait()
    seen: list[str | None] = []

    async def recording(stop: asyncio.Event, wake: asyncio.Event, delay: float) -> None:
        seen.append(state.last_error)
        await wait(stop, wake, delay)

    relay = _relay(store, producer, recording, state=state)

    await _run_until(relay, lambda: producer.sent)

    assert wait.delays[:2] == [BACKOFF_BASE, 2 * BACKOFF_BASE]
    assert seen[:2] == ["ConnectionRefusedError", "ConnectionRefusedError"]
    assert state.last_error is None


async def test_should_raise_when_the_database_fails_for_a_non_transient_reason() -> None:
    store, producer = InMemoryOutboxStore(), FakeProducer()
    store.failures.append(RuntimeError("bug"))
    relay = _relay(store, producer, RecordingWait())

    with pytest.raises(RuntimeError):
        await relay.run(asyncio.Event(), asyncio.Event())

    assert producer.stopped


async def test_should_retry_the_producer_start_and_report_running_once_connected() -> None:
    store, state = InMemoryOutboxStore(), RelayState()
    producer = FakeProducer(start_error=ConnectionRefusedError("down"))
    wait = RecordingWait()
    seen: list[str | None] = []

    async def recording(stop: asyncio.Event, wake: asyncio.Event, delay: float) -> None:
        seen.append(state.last_error)
        producer.start_error = None
        await wait(stop, wake, delay)

    relay = _relay(store, producer, recording, state=state)
    stop = asyncio.Event()
    task = asyncio.create_task(relay.run(stop, asyncio.Event()))

    await wait_until(lambda: state.running)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert seen[0] == "ConnectionRefusedError"
    assert wait.delays[0] == BACKOFF_BASE
    assert producer.started
    assert not state.running


async def test_should_stop_without_starting_when_stopped_during_the_start_backoff() -> None:
    state = RelayState()
    producer = FakeProducer(start_error=ConnectionRefusedError("down"))
    stop = asyncio.Event()

    async def stopping(_stop: asyncio.Event, _wake: asyncio.Event, _delay: float) -> None:
        stop.set()

    await _relay(InMemoryOutboxStore(), producer, stopping, state=state).run(stop, asyncio.Event())

    assert not producer.started
    assert not state.running


async def test_should_end_the_idle_wait_and_clear_wake_when_woken() -> None:
    stop, wake = asyncio.Event(), asyncio.Event()
    waiting = asyncio.create_task(wait_or_wake(stop, wake, 30))
    await asyncio.sleep(0)

    wake.set()
    await asyncio.wait_for(waiting, timeout=2)

    assert not wake.is_set()


async def test_should_end_the_idle_wait_when_stopped() -> None:
    stop, wake = asyncio.Event(), asyncio.Event()
    stop.set()

    await asyncio.wait_for(wait_or_wake(stop, wake, 30), timeout=2)


async def test_should_end_the_idle_wait_after_the_delay() -> None:
    await asyncio.wait_for(wait_or_wake(asyncio.Event(), asyncio.Event(), 0.001), timeout=2)
