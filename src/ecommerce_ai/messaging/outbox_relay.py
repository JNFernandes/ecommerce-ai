"""Outbox relay: publish saved outbox events to Kafka (research 7, FR-016, FR-017).

The only publishing path. The relay claims one due PENDING event at a time and sends its stored
payload with its stored key and topic. Success marks it RESOLVED; a failed send counts an attempt
and schedules the next one at ``now + delay``, where the delay doubles per attempt from
``OUTBOX_BACKOFF_BASE_SECONDS`` up to ``OUTBOX_BACKOFF_MAX_SECONDS``, with jitter; after
``OUTBOX_MAX_ATTEMPTS`` the event is EXHAUSTED and never sent again. The ``eventId``, key and
payload never change. When nothing is due it waits ``outbox_poll_interval_seconds``, or less when
the triage worker sets ``wake`` after a commit.

The relay survives its dependencies being down (FR-003): a producer that cannot start and a
transient database error are retried with the same growing backoff and reported in
``RelayState.last_error`` (class name only); any other database error ends the task. Each attempt
yields one ``outbox_publish`` log line with the event id, the order id (the key), the outcome and
the error class, never the payload.
"""

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog
from pydantic import JsonValue

from ecommerce_ai.infrastructure.db.engine import is_transient_db_error
from ecommerce_ai.infrastructure.errors import error_name
from ecommerce_ai.infrastructure.outbox import OutboxMessage
from ecommerce_ai.messaging.kafka import ProducerFactory, ProducerPort

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class RelayState:
    """Live state of the relay, reported by ``GET /ready``."""

    running: bool = False
    last_error: str | None = None


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

    async def fail(self, error: str, next_attempt_at: datetime, now: datetime) -> None:
        """Count a failed attempt and schedule the next one; ``error`` is a class name."""
        ...

    async def exhaust(self, error: str, now: datetime) -> None:
        """Count the last failed attempt and give the event up (EXHAUSTED)."""
        ...


class OutboxStore(Protocol):
    """Source of due outbox events, one transaction per event."""

    def claim_due(self, now: datetime) -> AbstractAsyncContextManager[OutboxClaim]:
        """Lock the earliest due PENDING event; concurrent callers skip it."""
        ...


class _Uniform(Protocol):
    """The part of ``random.Random`` the jitter needs."""

    def uniform(self, a: float, b: float) -> float:
        """Return a random float in ``[a, b]``."""
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


_SYSTEM_RANDOM = random.SystemRandom()


class OutboxRelay:
    """Publishes due outbox events until asked to stop."""

    def __init__(
        self,
        store: OutboxStore,
        producer_factory: ProducerFactory,
        *,
        poll_interval_seconds: float,
        max_attempts: int,
        backoff_base_seconds: float,
        backoff_max_seconds: float,
        state: RelayState,
        clock: Callable[[], datetime] = _utc_now,
        wait: IdleWait = wait_or_wake,
        rng: _Uniform = _SYSTEM_RANDOM,
    ) -> None:
        """Claim events from ``store`` and send them with producers from ``producer_factory``."""
        self._store = store
        self._producer_factory = producer_factory
        self._poll_interval = poll_interval_seconds
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._state = state
        self._clock = clock
        self._wait = wait
        self._rng = rng

    def retry_delay(self, failures: int) -> float:
        """Delay after ``failures + 1`` consecutive failures: ``min(base·2ⁿ, max)``, jittered
        into its upper half so retries never bunch at zero."""
        cap = min(self._backoff_base * 2 ** min(failures, 32), self._backoff_max)
        return self._rng.uniform(cap / 2, cap)

    async def _backoff(
        self, stop_event: asyncio.Event, error: BaseException, failures: int
    ) -> None:
        self._state.last_error = error_name(error)
        # A fresh wake event: only a stop request ends a failure backoff early.
        await self._wait(stop_event, asyncio.Event(), self.retry_delay(failures))

    async def _start_producer(self, stop_event: asyncio.Event) -> ProducerPort | None:
        """Start a producer, retrying with backoff; ``None`` when stopped before it started."""
        failures = 0
        while not stop_event.is_set():
            producer = self._producer_factory()
            try:
                await producer.start()
            except Exception as error:  # broker down or unreachable: retry, never give up
                logger.warning("outbox_relay_retry", step="start", error=error_name(error))
                await self._backoff(stop_event, error, failures)
                failures += 1
                continue
            self._state.last_error = None
            return producer
        return None

    async def run(self, stop_event: asyncio.Event, wake: asyncio.Event) -> None:
        """Publish due events until ``stop_event`` is set; idle waits end early on ``wake``."""
        producer = await self._start_producer(stop_event)
        if producer is None:
            return
        self._state.running = True
        failures = 0
        try:
            while not stop_event.is_set():
                try:
                    published = await self.publish_one(producer)
                except Exception as error:
                    if not is_transient_db_error(error):
                        raise
                    logger.warning("outbox_relay_retry", step="claim", error=error_name(error))
                    await self._backoff(stop_event, error, failures)
                    failures += 1
                    continue
                failures = 0
                if not published:
                    await self._wait(stop_event, wake, self._poll_interval)
        finally:
            self._state.running = False
            await producer.stop()

    async def publish_one(self, producer: ProducerPort) -> bool:
        """Try to send the earliest due event; ``False`` when nothing is due.

        A failed send is recorded on the event (retry or EXHAUSTED), not raised.
        """
        async with self._store.claim_due(self._clock()) as claim:
            event = claim.event
            if event is None:
                return False
            message = event.message
            attempt = event.attempts + 1
            try:
                await producer.send(
                    message.topic, message.key.encode(), encode_payload(message.payload)
                )
            except Exception as error:  # any send failure is one failed attempt of this event
                name = error_name(error)
                now = self._clock()
                if attempt >= self._max_attempts:
                    await claim.exhaust(name, now)
                    outcome = "exhausted"
                else:
                    delay = timedelta(seconds=self.retry_delay(event.attempts))
                    await claim.fail(name, now + delay, now)
                    outcome = "retry"
                self._state.last_error = name
                logger.warning(
                    "outbox_publish",
                    event_id=str(message.event_id),
                    order_id=message.key,
                    topic=message.topic,
                    attempt=attempt,
                    outcome=outcome,
                    error=name,
                )
                return True
            await claim.resolve(self._clock())
        self._state.last_error = None
        logger.info(
            "outbox_publish",
            event_id=str(message.event_id),
            order_id=message.key,
            topic=message.topic,
            attempt=attempt,
            outcome="published",
        )
        return True
