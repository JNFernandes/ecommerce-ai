"""Outbox relay: publish saved outbox events to Kafka (research 7, FR-016, FR-017).

The only publishing path. The relay claims one due PENDING event at a time, sends its stored
payload with its stored key and topic, and marks it RESOLVED in the same transaction; a failed
send rolls the claim back, so the event stays PENDING with the same ``eventId``. When nothing is
due it waits ``outbox_poll_interval_seconds``, or less when the triage worker sets ``wake`` after
a commit. Each attempt yields one ``outbox_publish`` log line with ids only, never the payload.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Protocol

import structlog
from pydantic import JsonValue

from ecommerce_ai.infrastructure.outbox import OutboxMessage
from ecommerce_ai.messaging.kafka import ProducerFactory, ProducerPort

logger = structlog.get_logger(__name__)


class DueEvent(Protocol):
    """A claimed outbox event."""

    @property
    def message(self) -> OutboxMessage:
        """What to publish, exactly as stored."""
        ...

    @property
    def attempts(self) -> int:
        """Publish attempts made so far."""
        ...


class OutboxClaim(Protocol):
    """One claimed due event (or none); outcomes take effect when the claim commits."""

    @property
    def event(self) -> DueEvent | None:
        """The claimed event, or ``None`` when nothing is due."""
        ...

    async def resolve(self, now: datetime) -> None:
        """Mark the event published."""
        ...


class OutboxStore(Protocol):
    """Source of due outbox events, one transaction per event."""

    def claim_due(self, now: datetime) -> AbstractAsyncContextManager[OutboxClaim]:
        """Lock the earliest due PENDING event; concurrent callers skip it."""
        ...


type IdleWait = Callable[[asyncio.Event, asyncio.Event, float], Awaitable[None]]
"""Waits ``delay`` seconds, or until the stop or the wake event is set."""


async def wait_or_wake(stop_event: asyncio.Event, wake: asyncio.Event, delay: float) -> None:
    """Return after ``delay`` seconds, or as soon as ``stop_event`` or ``wake`` is set.

    ``wake`` is cleared before returning, so the next wait starts fresh.
    """
    waiters = {asyncio.ensure_future(stop_event.wait()), asyncio.ensure_future(wake.wait())}
    try:
        await asyncio.wait(waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        wake.clear()


def encode_payload(payload: dict[str, JsonValue]) -> bytes:
    """The record value of a stored payload: compact JSON with sorted keys, so every attempt
    sends the same bytes."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class OutboxRelay:
    """Publishes due outbox events until asked to stop."""

    def __init__(
        self,
        store: OutboxStore,
        producer_factory: ProducerFactory,
        *,
        poll_interval_seconds: float,
        clock: Callable[[], datetime] = _utc_now,
        wait: IdleWait = wait_or_wake,
    ) -> None:
        """Claim events from ``store`` and send them with producers from ``producer_factory``."""
        self._store = store
        self._producer_factory = producer_factory
        self._poll_interval = poll_interval_seconds
        self._clock = clock
        self._wait = wait

    async def run(self, stop_event: asyncio.Event, wake: asyncio.Event) -> None:
        """Publish due events until ``stop_event`` is set; idle waits end early on ``wake``."""
        producer = self._producer_factory()
        await producer.start()
        try:
            while not stop_event.is_set():
                if not await self.publish_one(producer):
                    await self._wait(stop_event, wake, self._poll_interval)
        finally:
            await producer.stop()

    async def publish_one(self, producer: ProducerPort) -> bool:
        """Send the earliest due event; ``False`` when nothing is due."""
        async with self._store.claim_due(self._clock()) as claim:
            event = claim.event
            if event is None:
                return False
            message = event.message
            await producer.send(
                message.topic, message.key.encode(), encode_payload(message.payload)
            )
            await claim.resolve(self._clock())
        logger.info(
            "outbox_publish",
            event_id=str(message.event_id),
            topic=message.topic,
            attempt=event.attempts + 1,
            outcome="published",
        )
        return True
