"""Fake Kafka consumer and producer, and builders of valid failure events (no broker)."""

import asyncio
import json
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import JsonValue

from ecommerce_ai.messaging.contracts import (
    ORDER_CANCELLED_TOPIC,
    PAYMENT_FAILED_TOPIC,
    STOCK_REJECTED_TOPIC,
)
from ecommerce_ai.messaging.kafka import ConsumedRecord, TopicPartition

OCCURRED_AT = datetime(2026, 11, 24, 10, 15, 30, tzinfo=UTC)

type Payload = dict[str, JsonValue]


class FakeConsumer:
    """In-memory ``ConsumerPort``: hands out queued records one at a time and records commits.

    ``start_error`` / ``stop_error`` make those calls raise. ``getmany`` waits up to its timeout
    for a record, like the real consumer, so a loop over it never spins.
    """

    def __init__(
        self,
        records: Iterable[ConsumedRecord] = (),
        *,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
    ) -> None:
        """Queue ``records`` and set the failures to raise."""
        self.start_error = start_error
        self.stop_error = stop_error
        self.started = False
        self.stopped = False
        self.commits: list[dict[TopicPartition, int]] = []
        self._queue: deque[ConsumedRecord] = deque()
        self._highwater: dict[TopicPartition, int] = {}
        self._position: dict[TopicPartition, int] = {}
        self._available = asyncio.Event()
        self.add(*records)

    @property
    def committed(self) -> dict[TopicPartition, int]:
        """Latest committed offset per partition."""
        latest: dict[TopicPartition, int] = {}
        for commit in self.commits:
            latest.update(commit)
        return latest

    @property
    def pending(self) -> int:
        """Records not handed out yet."""
        return len(self._queue)

    def add(self, *records: ConsumedRecord) -> None:
        """Queue more records, as if producers had just published them."""
        for record in records:
            tp = TopicPartition(record.topic, record.partition)
            self._queue.append(record)
            self._highwater[tp] = max(self._highwater.get(tp, 0), record.offset + 1)
            self._position.setdefault(tp, record.offset)
        if self._queue:
            self._available.set()

    async def start(self) -> None:
        """Raise ``start_error`` if set, else mark the consumer started."""
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    async def stop(self) -> None:
        """Mark the consumer stopped, then raise ``stop_error`` if set."""
        self.stopped = True
        if self.stop_error is not None:
            raise self.stop_error

    async def getmany(self, timeout_ms: int) -> dict[TopicPartition, list[ConsumedRecord]]:
        """Return the next queued record, or nothing after ``timeout_ms``."""
        if not self._queue:
            try:
                await asyncio.wait_for(self._available.wait(), timeout_ms / 1000)
            except TimeoutError:
                return {}
        record = self._queue.popleft()
        if not self._queue:
            self._available.clear()
        tp = TopicPartition(record.topic, record.partition)
        self._position[tp] = record.offset + 1
        return {tp: [record]}

    async def commit(self, offsets: Mapping[TopicPartition, int]) -> None:
        """Record the committed offsets."""
        self.commits.append(dict(offsets))

    def assignment(self) -> set[TopicPartition]:
        """Every partition a record was queued on."""
        return set(self._highwater)

    def highwater(self, partition: TopicPartition) -> int | None:
        """Offset after the last queued record of ``partition``."""
        return self._highwater.get(partition)

    async def position(self, partition: TopicPartition) -> int:
        """Offset after the last record handed out from ``partition``."""
        return self._position.get(partition, 0)


def make_record(
    topic: str,
    value: Payload | bytes | None,
    *,
    key: bytes | None = None,
    partition: int = 0,
    offset: int = 0,
) -> ConsumedRecord:
    """Build a record; a payload dict is JSON-encoded, bytes are kept as they are."""
    raw = json.dumps(value).encode() if isinstance(value, dict) else value
    return ConsumedRecord(topic=topic, partition=partition, offset=offset, key=key, value=raw)


def _envelope(event_id: UUID | None, aggregate_id: UUID) -> Payload:
    return {
        "eventId": str(event_id or uuid4()),
        "aggregateId": str(aggregate_id),
        "occurredAt": OCCURRED_AT.isoformat(),
        "version": 1,
    }


def order_cancelled_payload(
    order_id: UUID, *, reason: str | None = None, event_id: UUID | None = None
) -> Payload:
    """A valid ``orders.order-cancelled`` event; ``reason`` omitted when ``None``."""
    payload = {**_envelope(event_id, order_id), "customerId": str(uuid4())}
    if reason is not None:
        payload["reason"] = reason
    return payload


def payment_failed_payload(
    order_id: UUID, *, reason: str = "card declined", event_id: UUID | None = None
) -> Payload:
    """A valid ``payments.payment-failed`` event (``aggregateId`` is a payment id)."""
    return {
        **_envelope(event_id, uuid4()),
        "orderId": str(order_id),
        "reason": reason,
        "failedAt": OCCURRED_AT.isoformat(),
    }


def stock_rejected_payload(
    order_id: UUID, *, reason: str = "insufficient stock", event_id: UUID | None = None
) -> Payload:
    """A valid ``inventory.stock-rejected`` event (``aggregateId`` is a reservation id)."""
    return {
        **_envelope(event_id, uuid4()),
        "orderId": str(order_id),
        "reason": reason,
        "items": [{"productId": "p-1", "requested": 2, "available": 0}],
        "rejectedAt": OCCURRED_AT.isoformat(),
    }


TOPIC_PAYLOADS = {
    ORDER_CANCELLED_TOPIC: order_cancelled_payload,
    PAYMENT_FAILED_TOPIC: payment_failed_payload,
    STOCK_REJECTED_TOPIC: stock_rejected_payload,
}
"""Valid-payload builder per consumed topic."""


@dataclass(frozen=True)
class SentRecord:
    """A record handed to ``FakeProducer.send``."""

    topic: str
    key: bytes
    value: bytes
    headers: tuple[tuple[str, bytes], ...] = ()


class FakeProducer:
    """In-memory producer: records every sent record; queued failures make the next sends raise.

    ``start_error`` / ``stop_error`` make those calls raise.
    """

    def __init__(
        self,
        *,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
    ) -> None:
        """Start with nothing sent and no send failure queued."""
        self.start_error = start_error
        self.stop_error = stop_error
        self.started = False
        self.stopped = False
        self.sent: list[SentRecord] = []
        self.failures: deque[BaseException] = deque()

    async def start(self) -> None:
        """Raise ``start_error`` if set, else mark the producer started."""
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    async def stop(self) -> None:
        """Mark the producer stopped, then raise ``stop_error`` if set."""
        self.stopped = True
        if self.stop_error is not None:
            raise self.stop_error

    async def send(
        self, topic: str, key: bytes, value: bytes, headers: Sequence[tuple[str, bytes]] = ()
    ) -> None:
        """Raise the next queued failure, else record the record as acknowledged."""
        if self.failures:
            raise self.failures.popleft()
        self.sent.append(SentRecord(topic=topic, key=key, value=value, headers=tuple(headers)))
