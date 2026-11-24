"""PostgreSQL store of the transactional outbox (``outbox_events``, research 7).

``claim_due`` opens one transaction and locks the earliest due PENDING event with ``FOR UPDATE
SKIP LOCKED``, so concurrent relays never send the same row at the same time. The claim records
the outcome of the send in that transaction; the event id, key and payload never change. A
failed send bumps ``attempts`` and reschedules the event, or marks it EXHAUSTED.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ecommerce_ai.infrastructure.db.tables import outbox_events
from ecommerce_ai.infrastructure.outbox import OutboxMessage, OutboxStatus


class DueOutboxEvent(BaseModel):
    """A PENDING outbox event whose next attempt is due."""

    model_config = ConfigDict(frozen=True)

    message: OutboxMessage = Field(description="What to publish, exactly as stored.")
    attempts: int = Field(ge=0, description="Publish attempts made so far.")


class SqlOutboxClaim:
    """One claimed due event (or none) on an open transaction."""

    def __init__(
        self, connection: AsyncConnection, row_id: UUID | None, event: DueOutboxEvent | None
    ) -> None:
        """Record outcomes through ``connection`` for the row ``row_id``."""
        self._connection = connection
        self._row_id = row_id
        self._event = event

    @property
    def event(self) -> DueOutboxEvent | None:
        """The claimed event, or ``None`` when nothing is due."""
        return self._event

    def _require(self) -> tuple[UUID, DueOutboxEvent]:
        if self._row_id is None or self._event is None:
            raise RuntimeError("no outbox event was claimed")
        return self._row_id, self._event

    async def resolve(self, now: datetime) -> None:
        """Mark the event published (RESOLVED) after one more attempt."""
        row_id, event = self._require()
        await self._connection.execute(
            update(outbox_events)
            .where(outbox_events.c.id == row_id)
            .values(
                status=OutboxStatus.RESOLVED.value,
                attempts=event.attempts + 1,
                last_error=None,
                updated_at=now,
            )
        )

    async def fail(self, error: str, next_attempt_at: datetime, now: datetime) -> None:
        """Count a failed attempt (``error`` is a class name) and schedule the next one."""
        row_id, event = self._require()
        await self._connection.execute(
            update(outbox_events)
            .where(outbox_events.c.id == row_id)
            .values(
                attempts=event.attempts + 1,
                next_attempt_at=next_attempt_at,
                last_error=error[:200],
                updated_at=now,
            )
        )

    async def exhaust(self, error: str, now: datetime) -> None:
        """Count the last failed attempt and give the event up (EXHAUSTED)."""
        row_id, event = self._require()
        await self._connection.execute(
            update(outbox_events)
            .where(outbox_events.c.id == row_id)
            .values(
                status=OutboxStatus.EXHAUSTED.value,
                attempts=event.attempts + 1,
                last_error=error[:200],
                updated_at=now,
            )
        )


class OutboxRepository:
    """Outbox store over the async engine; connects on the first claim only."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Open transactions on ``engine``."""
        self._engine = engine

    @asynccontextmanager
    async def claim_due(self, now: datetime) -> AsyncIterator[SqlOutboxClaim]:
        """Lock the earliest due PENDING event (or none); commit on exit, roll back on raise."""
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    select(outbox_events)
                    .where(
                        outbox_events.c.status == OutboxStatus.PENDING.value,
                        outbox_events.c.next_attempt_at <= now,
                    )
                    .order_by(outbox_events.c.next_attempt_at, outbox_events.c.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).first()
            if row is None:
                yield SqlOutboxClaim(connection, None, None)
                return
            event = DueOutboxEvent(
                message=OutboxMessage(
                    event_id=row.event_id,
                    topic=row.topic,
                    key=row.message_key,
                    payload=row.payload,
                    occurred_at=row.created_at,
                ),
                attempts=row.attempts,
            )
            yield SqlOutboxClaim(connection, row.id, event)
