"""PostgreSQL implementation of the ``DeadLetterStore`` port (FR-009, research 10).

The raw bytes and key are stored exactly as consumed, so a non-UTF-8 payload survives for
inspection; the reason is cut to the column's 500 characters.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final
from uuid import uuid4

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.infrastructure.db.tables import inbound_dead_letters

MAX_REASON_LENGTH: Final = 500


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DeadLetterRepository:
    """``DeadLetterStore`` over the async engine; one transaction per stored message."""

    def __init__(self, engine: AsyncEngine, clock: Callable[[], datetime] = _utc_now) -> None:
        """Write on ``engine``, stamping rows with ``clock`` (timezone-aware)."""
        self._engine = engine
        self._clock = clock

    async def add(
        self,
        topic: str,
        partition: int,
        offset: int,
        key: bytes | None,
        raw: bytes,
        reason: str,
    ) -> None:
        """Save the raw message with a reason that holds no payload values."""
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(inbound_dead_letters).values(
                    id=uuid4(),
                    topic=topic,
                    partition=partition,
                    offset=offset,
                    message_key=key,
                    raw_payload=raw,
                    failure_reason=reason[:MAX_REASON_LENGTH],
                    created_at=self._clock().astimezone(UTC),
                )
            )
