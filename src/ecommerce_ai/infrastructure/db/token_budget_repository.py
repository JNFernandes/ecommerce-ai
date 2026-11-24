"""PostgreSQL daily token budget (``llm_token_usage``, research 4, FR-012).

``reserve`` adds a reservation to the day's row only when it still fits in the budget, in one
conditional upsert, so concurrent workers and restarts can never push the day past the budget.
``settle`` swaps the reservation for the tokens actually spent. A crash between the two leaves
the reservation counted: conservative, the spend never exceeds the budget plus one case cap
(SC-005). Days are UTC dates chosen by the caller.
"""

from datetime import UTC, date, datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.infrastructure.db.tables import llm_token_usage


class TokenBudgetRepository:
    """``TokenBudget`` over the async engine; each call is one short transaction."""

    def __init__(self, engine: AsyncEngine, daily_budget: int) -> None:
        """Keep at most ``daily_budget`` tokens per UTC day on ``engine``."""
        self._engine = engine
        self._daily_budget = daily_budget

    async def reserve(self, day: date, cap: int) -> bool:
        """Reserve ``cap`` tokens on ``day``; ``False`` (nothing reserved) when it would not fit."""
        if cap > self._daily_budget:
            return False
        now = datetime.now(UTC)
        statement = (
            insert(llm_token_usage)
            .values(day=day, tokens=cap, updated_at=now)
            .on_conflict_do_update(
                index_elements=[llm_token_usage.c.day],
                set_={"tokens": llm_token_usage.c.tokens + cap, "updated_at": now},
                where=llm_token_usage.c.tokens + cap <= self._daily_budget,
            )
            .returning(llm_token_usage.c.tokens)
        )
        async with self._engine.begin() as connection:
            return (await connection.execute(statement)).first() is not None

    async def settle(self, day: date, reserved: int, actual: int) -> None:
        """Replace a reservation of ``reserved`` tokens on ``day`` with the ``actual`` spend."""
        statement = (
            llm_token_usage.update()
            .where(llm_token_usage.c.day == day)
            .values(
                tokens=func.greatest(llm_token_usage.c.tokens - reserved + actual, 0),
                updated_at=datetime.now(UTC),
            )
        )
        async with self._engine.begin() as connection:
            await connection.execute(statement)
