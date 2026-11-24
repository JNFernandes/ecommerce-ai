"""A database connection lost during a save loses no event (SC-003)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.triage_intake.service import TriageIntakeService
from ecommerce_ai.infrastructure.db.dead_letter_repository import DeadLetterRepository
from ecommerce_ai.infrastructure.db.tables import triage_evidence
from ecommerce_ai.infrastructure.db.triage_repository import SqlTriageUnitOfWork, TriageRepository
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.messaging.consumer_state import ConsumerState
from ecommerce_ai.messaging.contracts import PAYMENT_FAILED_TOPIC
from ecommerce_ai.messaging.kafka import TopicPartition
from ecommerce_ai.messaging.triage_consumer import TriageConsumer
from tests.support.kafka import FakeConsumer, make_record, payment_failed_payload
from tests.support.polling import wait_until


def _fast_settings(db_attempts: int) -> Settings:
    """Settings whose database retries and reconnections wait 10 ms."""
    return Settings(
        db_retry_max_attempts=db_attempts,
        db_retry_backoff_base_seconds=0.01,
        db_retry_backoff_max_seconds=0.01,
        kafka_reconnect_backoff_base_seconds=0.01,
        kafka_reconnect_backoff_max_seconds=0.01,
    )


class ConnectionKillingRepository(TriageRepository):
    """Terminates its own database session inside the next ``kills`` transactions."""

    def __init__(self, engine: AsyncEngine, kills: int) -> None:
        super().__init__(engine)
        self.engine = engine
        self.kills = kills

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[SqlTriageUnitOfWork]:
        async with self.engine.begin() as connection:
            if self.kills:
                self.kills -= 1
                await connection.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))
            yield SqlTriageUnitOfWork(connection)


async def _evidence_count(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        count = await connection.scalar(select(func.count()).select_from(triage_evidence))
    return int(count or 0)


async def _run(
    engine: AsyncEngine,
    repository: TriageRepository,
    consumers: list[FakeConsumer],
    settings: Settings,
) -> None:
    def factory() -> FakeConsumer:
        return consumers.pop(0) if len(consumers) > 1 else consumers[0]

    last = consumers[-1]
    consumer = TriageConsumer(
        factory,
        TriageIntakeService(repository),
        DeadLetterRepository(engine),
        ConsumerState(),
        settings,
        poll_timeout_ms=5,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    await wait_until(lambda: bool(last.commits) or task.done(), timeout=15)
    stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_should_retry_and_save_when_the_connection_dies_during_a_save(
    db_engine: AsyncEngine,
) -> None:
    record = make_record(PAYMENT_FAILED_TOPIC, payment_failed_payload(uuid4()), offset=3)
    fake = FakeConsumer([record])
    repository = ConnectionKillingRepository(db_engine, kills=1)

    await _run(db_engine, repository, [fake], _fast_settings(3))

    assert repository.kills == 0
    assert fake.commits == [{TopicPartition(PAYMENT_FAILED_TOPIC, 0): 4}]
    assert await _evidence_count(db_engine) == 1


async def test_should_not_commit_and_should_record_the_redelivery_when_retries_run_out(
    db_engine: AsyncEngine,
) -> None:
    record = make_record(PAYMENT_FAILED_TOPIC, payment_failed_payload(uuid4()), offset=8)
    first, second = FakeConsumer([record]), FakeConsumer([record])
    repository = ConnectionKillingRepository(db_engine, kills=2)
    settings = _fast_settings(2)

    await _run(db_engine, repository, [first, second], settings)

    assert (first.commits, first.stopped) == ([], True)
    assert second.commits == [{TopicPartition(PAYMENT_FAILED_TOPIC, 0): 9}]
    assert await _evidence_count(db_engine) == 1
