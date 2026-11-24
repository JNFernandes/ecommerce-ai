"""The PostgreSQL token budget holds per UTC day, across concurrent reservations (SC-005)."""

import asyncio
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.triage.ports import TokenBudget
from ecommerce_ai.infrastructure.db.tables import llm_token_usage
from ecommerce_ai.infrastructure.db.token_budget_repository import TokenBudgetRepository

DAY = date(2026, 11, 24)


async def _used(engine: AsyncEngine, day: date = DAY) -> int | None:
    query = select(llm_token_usage.c.tokens).where(llm_token_usage.c.day == day)
    async with engine.connect() as connection:
        return (await connection.execute(query)).scalar_one_or_none()


async def test_should_reserve_until_the_daily_budget_is_spent(db_engine: AsyncEngine) -> None:
    budget: TokenBudget = TokenBudgetRepository(db_engine, daily_budget=1000)

    granted = [await budget.reserve(DAY, 400) for _ in range(3)]

    assert granted == [True, True, False]
    assert await _used(db_engine) == 800


async def test_should_settle_the_actual_spend_when_the_call_is_done(
    db_engine: AsyncEngine,
) -> None:
    budget = TokenBudgetRepository(db_engine, daily_budget=1000)
    assert await budget.reserve(DAY, 800)

    await budget.settle(DAY, reserved=800, actual=150)

    assert await _used(db_engine) == 150
    assert await budget.reserve(DAY, 800)


async def test_should_refuse_a_reservation_larger_than_the_budget(db_engine: AsyncEngine) -> None:
    budget = TokenBudgetRepository(db_engine, daily_budget=100)

    assert not await budget.reserve(DAY, 101)
    assert await _used(db_engine) is None


async def test_should_never_exceed_the_budget_when_workers_reserve_concurrently(
    db_engine: AsyncEngine,
) -> None:
    budget = TokenBudgetRepository(db_engine, daily_budget=1000)

    granted = await asyncio.gather(*(budget.reserve(DAY, 100) for _ in range(25)))

    assert sum(granted) == 10
    assert await _used(db_engine) == 1000


async def test_should_start_a_new_budget_on_the_next_utc_day(db_engine: AsyncEngine) -> None:
    budget = TokenBudgetRepository(db_engine, daily_budget=500)
    assert await budget.reserve(DAY, 500)
    assert not await budget.reserve(DAY, 1)

    assert await budget.reserve(date(2026, 11, 25), 500)
