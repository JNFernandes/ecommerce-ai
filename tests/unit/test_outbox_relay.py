"""The outbox relay publishes stored events unchanged and marks them RESOLVED (US1)."""

import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.outbox import OutboxMessage, OutboxStatus
from ecommerce_ai.messaging.outbox_relay import (
    OutboxRelay,
    OutboxStore,
    encode_payload,
    wait_or_wake,
)
from tests.support.kafka import FakeProducer
from tests.support.polling import wait_until
from tests.support.stores import InMemoryOutboxStore

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
POLL_SECONDS = 0.5


def _message(order: str = "order-1") -> OutboxMessage:
    return OutboxMessage(
        event_id=uuid4(),
        topic="ai.order-triage-completed",
        key=order,
        payload={"orderId": order, "explanation": "Café déjà vu.", "confidence": 0.9},
        occurred_at=NOW,
    )


class RecordingWait:
    """Records idle waits instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, _stop: asyncio.Event, _wake: asyncio.Event, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


def _relay(store: InMemoryOutboxStore, producer: FakeProducer, wait: RecordingWait) -> OutboxRelay:
    port: OutboxStore = store
    return OutboxRelay(
        port,
        lambda: producer,
        poll_interval_seconds=POLL_SECONDS,
        clock=lambda: NOW,
        wait=wait,
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


async def test_should_keep_the_event_pending_when_the_send_fails() -> None:
    store, producer = InMemoryOutboxStore(), FakeProducer()
    message = _message()
    store.add(message, NOW)
    producer.failures.append(ConnectionError("broker down"))
    relay = _relay(store, producer, RecordingWait())

    with pytest.raises(ConnectionError):
        await relay.publish_one(producer)

    assert store.event(message.event_id).status is OutboxStatus.PENDING
    assert store.rollbacks == 1


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
    assert line["attempt"] == 1
    assert line["outcome"] == "published"
    assert "Café" not in stream.getvalue()


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
