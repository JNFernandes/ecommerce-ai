"""Kafka consumer of the three failure topics: decode, delegate to the intake, then commit.

The consumer holds no business logic (constitution I): it decodes each record, hands valid events
to ``TriageIntakeService.record`` and invalid ones to the dead-letter store, and only then commits
the record's offset (FR-008, FR-009). Each record yields one log line with its coordinates and
outcome, never its payload, ``customerId`` or reason (FR-014).

Resilience (research 2-4, 13): a save that fails on a transient database error is retried with
bounded exponential backoff; when the retries run out, or Kafka fails, the consumer is closed
without committing and rebuilt after ``min(base * 2**n, max)`` seconds with jitter, so the
unsaved record is delivered again from the last commit. The stop event interrupts every wait.
"""

import asyncio
import math
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Final, Protocol

import structlog

from ecommerce_ai.agents.triage_intake.model import IntakeOutcome
from ecommerce_ai.agents.triage_intake.ports import DeadLetterStore
from ecommerce_ai.agents.triage_intake.service import TriageIntakeService
from ecommerce_ai.infrastructure.db.engine import is_transient_db_error
from ecommerce_ai.infrastructure.retry import call_with_retry
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.messaging.consumer_state import ConsumerState
from ecommerce_ai.messaging.decoding import DecodeFailure, decode
from ecommerce_ai.messaging.kafka import (
    ConsumedRecord,
    ConsumerFactory,
    ConsumerPort,
    TopicPartition,
)

DEFAULT_POLL_TIMEOUT_MS: Final = 500
"""Longest wait for new records before the stop request is checked again."""

logger = structlog.get_logger(__name__)


class _Uniform(Protocol):
    """The part of ``random.Random`` the backoff needs."""

    def uniform(self, a: float, b: float) -> float:
        """Return a random float in ``[a, b]``."""
        ...


type StopWait = Callable[[asyncio.Event, float], Awaitable[None]]
"""Waits ``delay`` seconds or until the stop event is set, whichever comes first."""


async def wait_or_stop(stop_event: asyncio.Event, delay: float) -> None:
    """Return after ``delay`` seconds, or as soon as ``stop_event`` is set."""
    with suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), delay)


_SYSTEM_RANDOM = random.SystemRandom()


class TriageConsumer:
    """Runs the consumer until asked to stop, reconnecting after every failure."""

    def __init__(
        self,
        consumer_factory: ConsumerFactory,
        intake: TriageIntakeService,
        dead_letters: DeadLetterStore,
        state: ConsumerState,
        settings: Settings,
        *,
        poll_timeout_ms: int = DEFAULT_POLL_TIMEOUT_MS,
        wait: StopWait = wait_or_stop,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: _Uniform = _SYSTEM_RANDOM,
    ) -> None:
        """Build consumers with ``consumer_factory`` and report progress in ``state``.

        ``wait`` paces reconnections, ``sleep`` the database retries and ``rng`` draws the jitter
        of both; tests replace them so they never wait.
        """
        self._consumer_factory = consumer_factory
        self._intake = intake
        self._dead_letters = dead_letters
        self._state = state
        self._settings = settings
        self._poll_timeout_ms = poll_timeout_ms
        self._wait = wait
        self._sleep = sleep
        self._rng = rng

    async def run(self, stop_event: asyncio.Event) -> None:
        """Consume until ``stop_event`` is set, reconnecting with bounded backoff on failures."""
        failures = 0
        while not stop_event.is_set():
            try:
                await self._run_connection(stop_event)
            except Exception as error:
                self._state.mark_failed(error)
                delay = self._reconnect_delay(failures)
                failures += 1
                # The class name only: driver messages can echo connection details.
                logger.warning(
                    "triage_consumer_error",
                    error=type(error).__name__,
                    retry_in_seconds=round(delay, 3),
                )
                await self._wait(stop_event, delay)
            else:
                failures = 0
        self._state.mark_stopped()

    def _reconnect_delay(self, failures: int) -> float:
        """``min(base * 2**n, max)`` with equal jitter: within ``[cap / 2, cap]``."""
        settings = self._settings
        cap = min(
            settings.kafka_reconnect_backoff_base_seconds * float(2**failures),
            settings.kafka_reconnect_backoff_max_seconds,
        )
        return cap / 2 + self._rng.uniform(0.0, cap / 2)

    async def _run_connection(self, stop_event: asyncio.Event) -> None:
        """Consume on one connection until stopped; the consumer is always closed afterwards."""
        consumer = self._consumer_factory()
        try:
            await consumer.start()
            self._state.mark_connected()
            await self._consume(consumer, stop_event)
        finally:
            await self._close(consumer)

    async def _close(self, consumer: ConsumerPort) -> None:
        try:
            await consumer.stop()
        except Exception as error:
            # Closing is best effort: a failure here must not hide the error that ended the run.
            logger.warning("triage_consumer_close_failed", error=type(error).__name__)

    async def _consume(self, consumer: ConsumerPort, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            batches = await consumer.getmany(self._poll_timeout_ms)
            for records in batches.values():
                for record in records:
                    if stop_event.is_set():
                        return
                    await self._handle(consumer, record)
            await self._refresh_lag(consumer)

    async def _handle(self, consumer: ConsumerPort, record: ConsumedRecord) -> None:
        decoded = decode(record)
        ids: dict[str, str] = {}
        if isinstance(decoded, DecodeFailure):
            failure = decoded

            async def save() -> IntakeOutcome:
                await self._dead_letters.add(
                    record.topic,
                    record.partition,
                    record.offset,
                    record.key,
                    record.value or b"",
                    failure.reason,
                )
                return IntakeOutcome.DEAD_LETTERED

        else:
            event = decoded
            ids = {"event_id": str(event.event_id), "order_id": str(event.order_id)}

            async def save() -> IntakeOutcome:
                return await self._intake.record(event)

        outcome = await self._with_db_retry(save)
        await consumer.commit({TopicPartition(record.topic, record.partition): record.offset + 1})
        logger.info(
            "triage_message",
            topic=record.topic,
            partition=record.partition,
            offset=record.offset,
            outcome=outcome.value,
            **ids,
        )

    async def _with_db_retry(self, save: Callable[[], Awaitable[IntakeOutcome]]) -> IntakeOutcome:
        """Run ``save``, retrying transient database errors up to ``DB_RETRY_MAX_ATTEMPTS``."""

        async def attempt(_timeout: float) -> IntakeOutcome:
            # The database driver enforces DATABASE_TIMEOUT_SECONDS on every statement.
            return await save()

        settings = self._settings
        return await call_with_retry(
            attempt,
            deadline=math.inf,
            max_retries=settings.db_retry_max_attempts - 1,
            backoff_base=settings.db_retry_backoff_base_seconds,
            backoff_max=settings.db_retry_backoff_max_seconds,
            attempt_timeout=math.inf,
            is_retryable=is_transient_db_error,
            sleep=self._sleep,
            rng=self._rng,
        )

    async def _refresh_lag(self, consumer: ConsumerPort) -> None:
        lag = 0
        for partition in consumer.assignment():
            highwater = consumer.highwater(partition)
            if highwater is None:
                return
            lag += max(highwater - await consumer.position(partition), 0)
        self._state.update_lag(lag)
