"""PostgreSQL per-user day counters (``chat_user_usage``, FR-014/015, research §9).

One row per user and UTC day holds the messages accepted and the tokens reserved or spent. Both
counters live in the row, not in the stored messages, so deleting a conversation cannot reset a
user's day. ``reserve`` adds tokens only while the day stays within the cap, in one conditional
upsert (as ``TokenBudgetRepository`` does), so concurrent requests can never push a user past it;
``settle`` swaps the reservation for the actual spend and never goes below zero. Every query
filters by ``user_id``.
"""

from datetime import UTC, date, datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.infrastructure.db.tables import chat_user_usage


class UserUsageRepository:
    """``UserUsageStore`` over the async engine; each call is one short transaction."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Keep the counters on ``engine``."""
        self._engine = engine

    async def accept_message(self, user_id: str, day: date) -> int:
        """Count one accepted message on ``day``; return the day's count including it."""
        now = datetime.now(UTC)
        statement = (
            insert(chat_user_usage)
            .values(user_id=user_id, day=day, messages=1, tokens=0, updated_at=now)
            .on_conflict_do_update(
                index_elements=[chat_user_usage.c.user_id, chat_user_usage.c.day],
                set_={"messages": chat_user_usage.c.messages + 1, "updated_at": now},
            )
            .returning(chat_user_usage.c.messages)
        )
        async with self._engine.begin() as connection:
            return int((await connection.execute(statement)).scalar_one())

    async def reserve(self, user_id: str, day: date, tokens: int, cap: int) -> bool:
        """Reserve ``tokens`` on ``day`` if the total stays ≤ ``cap``, else ``False``."""
        if tokens > cap:
            return False
        now = datetime.now(UTC)
        statement = (
            insert(chat_user_usage)
            .values(user_id=user_id, day=day, messages=0, tokens=tokens, updated_at=now)
            .on_conflict_do_update(
                index_elements=[chat_user_usage.c.user_id, chat_user_usage.c.day],
                set_={"tokens": chat_user_usage.c.tokens + tokens, "updated_at": now},
                where=chat_user_usage.c.tokens + tokens <= cap,
            )
            .returning(chat_user_usage.c.tokens)
        )
        async with self._engine.begin() as connection:
            return (await connection.execute(statement)).first() is not None

    async def settle(self, user_id: str, day: date, reserved: int, actual: int) -> None:
        """Replace a reservation of ``reserved`` tokens with the ``actual`` spend (never < 0)."""
        statement = (
            chat_user_usage.update()
            .where(chat_user_usage.c.user_id == user_id, chat_user_usage.c.day == day)
            .values(
                tokens=func.greatest(chat_user_usage.c.tokens - reserved + actual, 0),
                updated_at=datetime.now(UTC),
            )
        )
        async with self._engine.begin() as connection:
            await connection.execute(statement)
