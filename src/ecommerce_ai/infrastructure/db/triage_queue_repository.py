"""PostgreSQL implementation of the ``TriageCaseQueue`` port (research 5, 6).

``next_settled`` opens one transaction and locks the oldest settled PENDING_TRIAGE case with
``FOR UPDATE SKIP LOCKED``, so concurrent workers never take the same case (FR-001). The lock is
held for the whole graph run. ``complete`` inserts the result (``case_id`` is UNIQUE: one result
per order) and its PENDING outbox event, and marks the case COMPLETED, all in that transaction;
an exception anywhere rolls everything back and leaves the case pending (FR-015).

The outbox payload comes from an injected serializer (``messaging/triage_published.py``), so this
module never knows the event contract.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ecommerce_ai.agents.triage.model import CaseForTriage, EvidenceItem, TriageResult
from ecommerce_ai.agents.triage_intake.model import TriageCause, TriageStatus
from ecommerce_ai.infrastructure.db.tables import (
    outbox_events,
    triage_cases,
    triage_evidence,
    triage_results,
)
from ecommerce_ai.infrastructure.outbox import OutboxMessage, OutboxStatus

type ResultSerializer = Callable[[TriageResult, CaseForTriage], OutboxMessage]
"""Builds the outbox message announcing a result; its ``occurred_at`` is the creation time."""


class SqlCaseClaim:
    """``CaseClaim`` on one open transaction holding the lock of at most one case."""

    def __init__(
        self,
        connection: AsyncConnection,
        case: CaseForTriage | None,
        serializer: ResultSerializer,
    ) -> None:
        """Save through ``connection``; ``case`` is ``None`` when nothing was settled."""
        self._connection = connection
        self._case = case
        self._serializer = serializer

    @property
    def case(self) -> CaseForTriage | None:
        """The locked case, or ``None`` when no case is settled."""
        return self._case

    async def complete(self, result: TriageResult) -> None:
        """Insert the result and its outbox event and mark the case COMPLETED."""
        case = self._case
        if case is None:
            raise RuntimeError("no case was claimed")
        message = self._serializer(result, case)
        now = message.occurred_at
        await self._connection.execute(
            triage_results.insert().values(
                id=uuid4(),
                case_id=case.case_id,
                event_id=message.event_id,
                cause=result.cause.value,
                explanation=result.explanation,
                suggested_action=result.suggested_action.value,
                confidence=result.confidence,
                source=result.source.value,
                prompt_version=result.prompt_version,
                model_id=result.model_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                latency_ms=result.latency_ms,
                revisions=result.revisions,
                violations=[violation.model_dump(mode="json") for violation in result.violations],
                created_at=now,
            )
        )
        await self._connection.execute(
            outbox_events.insert().values(
                id=uuid4(),
                event_id=message.event_id,
                topic=message.topic,
                message_key=message.key,
                payload=message.payload,
                status=OutboxStatus.PENDING.value,
                attempts=0,
                next_attempt_at=now,
                last_error=None,
                created_at=now,
                updated_at=now,
            )
        )
        await self._connection.execute(
            update(triage_cases)
            .where(triage_cases.c.id == case.case_id)
            .values(status=TriageStatus.COMPLETED.value, updated_at=now)
        )


async def _lock_settled_case(
    connection: AsyncConnection, settle_before: datetime
) -> CaseForTriage | None:
    row = (
        await connection.execute(
            select(triage_cases.c.id, triage_cases.c.order_id, triage_cases.c.cause)
            .where(
                triage_cases.c.status == TriageStatus.PENDING_TRIAGE.value,
                triage_cases.c.updated_at <= settle_before,
            )
            .order_by(triage_cases.c.updated_at, triage_cases.c.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).first()
    if row is None:
        return None
    evidence = await connection.execute(
        select(
            triage_evidence.c.source_topic,
            triage_evidence.c.reason,
            triage_evidence.c.occurred_at,
            triage_evidence.c.payload,
        )
        .where(triage_evidence.c.case_id == row.id)
        .order_by(triage_evidence.c.occurred_at, triage_evidence.c.received_at)
    )
    return CaseForTriage(
        case_id=row.id,
        order_id=row.order_id,
        cause=TriageCause(row.cause),
        evidence=tuple(
            EvidenceItem(
                topic=item.source_topic,
                reason=item.reason,
                occurred_at=item.occurred_at,
                payload=item.payload,
            )
            for item in evidence
        ),
    )


class TriageQueueRepository:
    """``TriageCaseQueue`` over the async engine; connects on the first claim only."""

    def __init__(self, engine: AsyncEngine, serializer: ResultSerializer) -> None:
        """Open transactions on ``engine``; ``serializer`` builds each result's outbox message."""
        self._engine = engine
        self._serializer = serializer

    @asynccontextmanager
    async def next_settled(self, settle_before: datetime) -> AsyncIterator[SqlCaseClaim]:
        """Lock the oldest settled case (or none); commit on normal exit, roll back on raise."""
        async with self._engine.begin() as connection:
            case = await _lock_settled_case(connection, settle_before)
            yield SqlCaseClaim(connection, case, self._serializer)
