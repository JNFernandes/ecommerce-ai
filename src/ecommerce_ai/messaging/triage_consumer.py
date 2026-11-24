"""Kafka consumer of the three failure topics: decode, delegate to the intake, then commit.

The consumer holds no business logic (constitution I): it decodes each record, hands valid events
to ``TriageIntakeService.record`` and invalid ones to the dead-letter store, and only then commits
the record's offset (FR-008, FR-009). Each record yields one log line with its coordinates and
outcome, never its payload, ``customerId`` or reason (FR-014).
"""

import asyncio
from typing import Final

import structlog

from ecommerce_ai.agents.triage_intake.model import IntakeOutcome
from ecommerce_ai.agents.triage_intake.ports import DeadLetterStore
from ecommerce_ai.agents.triage_intake.service import TriageIntakeService
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


class TriageConsumer:
    """Runs one consumer until asked to stop, committing each record after it is saved."""

    def __init__(
        self,
        consumer_factory: ConsumerFactory,
        intake: TriageIntakeService,
        dead_letters: DeadLetterStore,
        state: ConsumerState,
        *,
        poll_timeout_ms: int = DEFAULT_POLL_TIMEOUT_MS,
    ) -> None:
        """Build consumers with ``consumer_factory`` and report progress in ``state``."""
        self._consumer_factory = consumer_factory
        self._intake = intake
        self._dead_letters = dead_letters
        self._state = state
        self._poll_timeout_ms = poll_timeout_ms

    async def run(self, stop_event: asyncio.Event) -> None:
        """Consume until ``stop_event`` is set; an error stops the consumer and is re-raised."""
        consumer = self._consumer_factory()
        try:
            await consumer.start()
            self._state.mark_connected()
            await self._consume(consumer, stop_event)
        except Exception as error:
            self._state.mark_failed(error)
            raise
        finally:
            await consumer.stop()
        self._state.mark_stopped()

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
            await self._dead_letters.add(
                record.topic,
                record.partition,
                record.offset,
                record.key,
                record.value or b"",
                decoded.reason,
            )
            outcome = IntakeOutcome.DEAD_LETTERED
        else:
            outcome = await self._intake.record(decoded)
            ids = {"event_id": str(decoded.event_id), "order_id": str(decoded.order_id)}
        await consumer.commit({TopicPartition(record.topic, record.partition): record.offset + 1})
        logger.info(
            "triage_message",
            topic=record.topic,
            partition=record.partition,
            offset=record.offset,
            outcome=outcome.value,
            **ids,
        )

    async def _refresh_lag(self, consumer: ConsumerPort) -> None:
        lag = 0
        for partition in consumer.assignment():
            highwater = consumer.highwater(partition)
            if highwater is None:
                return
            lag += max(highwater - await consumer.position(partition), 0)
        self._state.update_lag(lag)
