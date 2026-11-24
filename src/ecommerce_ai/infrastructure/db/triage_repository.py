"""PostgreSQL implementation of the ``TriageCaseStore`` port (research 9).

One transaction per event, race-safe across consumers: the case is upserted with
``ON CONFLICT (order_id) DO NOTHING`` and then locked with ``SELECT ... FOR UPDATE``, so events
for the same order are serialised; evidence is inserted with ``ON CONFLICT (event_id) DO
NOTHING``, so a redelivered event adds nothing.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ecommerce_ai.agents.triage_intake.model import (
    LockedCase,
    TriageCause,
    TriageEventInput,
    TriageStatus,
)
from ecommerce_ai.infrastructure.db.tables import triage_cases, triage_evidence


class SqlTriageUnitOfWork:
    """``TriageUnitOfWork`` on one open database transaction."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Run every operation on ``connection``, inside its current transaction."""
        self._connection = connection

    async def get_or_create_locked_case(
        self, order_id: UUID, cause: TriageCause, now: datetime
    ) -> LockedCase:
        """Return the order's case, created with ``cause`` if absent, locked until commit."""
        await self._connection.execute(
            insert(triage_cases)
            .values(
                id=uuid4(),
                order_id=order_id,
                cause=cause.value,
                status=TriageStatus.PENDING_TRIAGE.value,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=[triage_cases.c.order_id])
        )
        row = (
            await self._connection.execute(
                select(triage_cases.c.id, triage_cases.c.cause, triage_cases.c.status)
                .where(triage_cases.c.order_id == order_id)
                .with_for_update()
            )
        ).one()
        return LockedCase(
            case_id=row.id, cause=TriageCause(row.cause), status=TriageStatus(row.status)
        )

    async def add_evidence(
        self, case_id: UUID, event: TriageEventInput, received_at: datetime
    ) -> bool:
        """Attach the event to the case; ``False`` when its ``eventId`` is already stored."""
        inserted = await self._connection.execute(
            insert(triage_evidence)
            .values(
                id=uuid4(),
                case_id=case_id,
                source_topic=event.topic,
                event_id=event.event_id,
                reason=event.reason,
                occurred_at=event.occurred_at,
                received_at=received_at,
                payload=event.payload,
            )
            .on_conflict_do_nothing(index_elements=[triage_evidence.c.event_id])
            .returning(triage_evidence.c.id)
        )
        return inserted.first() is not None

    async def update_case(self, case_id: UUID, cause: TriageCause, updated_at: datetime) -> None:
        """Set the case's cause and ``updated_at``."""
        await self._connection.execute(
            update(triage_cases)
            .where(triage_cases.c.id == case_id)
            .values(cause=cause.value, updated_at=updated_at)
        )


class TriageRepository:
    """``TriageCaseStore`` over the async engine; connects on the first transaction only."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Open transactions on ``engine``."""
        self._engine = engine

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[SqlTriageUnitOfWork]:
        """Open a transaction; commit on normal exit, roll back on an exception."""
        async with self._engine.begin() as connection:
            yield SqlTriageUnitOfWork(connection)
