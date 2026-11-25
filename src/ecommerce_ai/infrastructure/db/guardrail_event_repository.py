"""PostgreSQL guard audit (``guardrail_events``, FR-019, research §10).

One row per guard event, stamped with the current UTC time; rows are never updated or deleted
(a trigger enforces it) and hold no content, only the stage, rule, action and severity. Every
query filters by ``user_id``. The cooldown rule reads it back: the count of a user's recent input
blocks, and the time of their newest ``cooldown`` event.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.guardrails.events import (
    GuardAction,
    GuardEvent,
    GuardSeverity,
    GuardStage,
)
from ecommerce_ai.infrastructure.db.tables import guardrail_events


def _utc_now() -> datetime:
    return datetime.now(UTC)


class GuardrailEventRepository:
    """``GuardrailEventStore`` over the async engine; each call is one short transaction."""

    def __init__(self, engine: AsyncEngine, now: Callable[[], datetime] = _utc_now) -> None:
        """Store events on ``engine``, stamped by ``now`` (UTC, timezone-aware)."""
        self._engine = engine
        self._now = now

    async def append(
        self,
        user_id: str,
        conversation_id: UUID | None,
        request_id: str | None,
        events: Sequence[GuardEvent],
    ) -> None:
        """Store ``events`` of one run in one transaction; nothing happens for no events."""
        if not events:
            return
        created_at = self._now()
        rows = [
            {
                "id": uuid4(),
                "user_id": user_id,
                "conversation_id": conversation_id,
                "request_id": request_id,
                "stage": event.stage.value,
                "rule": event.rule,
                "action": event.action.value,
                "severity": event.severity.value,
                "created_at": created_at,
            }
            for event in events
        ]
        async with self._engine.begin() as connection:
            await connection.execute(guardrail_events.insert(), rows)

    async def count_blocks_since(
        self, user_id: str, since: datetime, min_severity: GuardSeverity
    ) -> int:
        """Input ``blocked`` events of ``user_id`` at or after ``since`` with ≥ ``min_severity``."""
        severities = [s.value for s in GuardSeverity if s.rank >= min_severity.rank]
        query = select(func.count()).where(
            guardrail_events.c.user_id == user_id,
            guardrail_events.c.stage == GuardStage.INPUT.value,
            guardrail_events.c.action == GuardAction.BLOCKED.value,
            guardrail_events.c.severity.in_(severities),
            guardrail_events.c.created_at >= since,
        )
        async with self._engine.connect() as connection:
            return int((await connection.execute(query)).scalar_one())

    async def latest_cooldown_at(self, user_id: str) -> datetime | None:
        """Time of the user's newest ``cooldown`` event, ``None`` when there is none."""
        query = select(func.max(guardrail_events.c.created_at)).where(
            guardrail_events.c.user_id == user_id,
            guardrail_events.c.action == GuardAction.COOLDOWN.value,
        )
        async with self._engine.connect() as connection:
            latest: datetime | None = (await connection.execute(query)).scalar_one()
        return latest
