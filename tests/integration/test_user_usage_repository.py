"""The PostgreSQL per-user counters hold per user and UTC day, across concurrent requests."""

import asyncio
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.guardrails.events import UserUsageStore
from ecommerce_ai.infrastructure.db.tables import chat_user_usage
from ecommerce_ai.infrastructure.db.user_usage_repository import UserUsageRepository

DAY = date(2026, 11, 25)


async def _usage(engine: AsyncEngine, user_id: str = "user-1", day: date = DAY) -> tuple[int, int]:
    query = select(chat_user_usage.c.messages, chat_user_usage.c.tokens).where(
        chat_user_usage.c.user_id == user_id, chat_user_usage.c.day == day
    )
    async with engine.connect() as connection:
        row = (await connection.execute(query)).one()
    return int(row.messages), int(row.tokens)


async def test_should_count_accepted_messages_per_user_and_day(db_engine: AsyncEngine) -> None:
    store: UserUsageStore = UserUsageRepository(db_engine)

    counts = [await store.accept_message("user-1", DAY) for _ in range(3)]
    other_user = await store.accept_message("user-2", DAY)
    next_day = await store.accept_message("user-1", date(2026, 11, 26))

    assert counts == [1, 2, 3]
    assert (other_user, next_day) == (1, 1)
    assert await _usage(db_engine) == (3, 0)


async def test_should_reserve_until_the_user_cap_is_spent(db_engine: AsyncEngine) -> None:
    store = UserUsageRepository(db_engine)

    granted = [await store.reserve("user-1", DAY, 400, cap=1000) for _ in range(3)]

    assert granted == [True, True, False]
    assert await _usage(db_engine) == (0, 800)


async def test_should_keep_messages_and_tokens_in_one_row(db_engine: AsyncEngine) -> None:
    store = UserUsageRepository(db_engine)

    assert await store.reserve("user-1", DAY, 100, cap=1000)
    assert await store.accept_message("user-1", DAY) == 1

    assert await _usage(db_engine) == (1, 100)


async def test_should_refuse_a_reservation_larger_than_the_cap(db_engine: AsyncEngine) -> None:
    store = UserUsageRepository(db_engine)

    assert not await store.reserve("user-1", DAY, 101, cap=100)
    async with db_engine.connect() as connection:
        assert (await connection.execute(select(chat_user_usage))).first() is None


async def test_should_isolate_users_when_one_spent_the_cap(db_engine: AsyncEngine) -> None:
    store = UserUsageRepository(db_engine)
    assert await store.reserve("user-1", DAY, 500, cap=500)

    assert not await store.reserve("user-1", DAY, 1, cap=500)
    assert await store.reserve("user-2", DAY, 500, cap=500)


async def test_should_settle_the_actual_spend_and_never_go_below_zero(
    db_engine: AsyncEngine,
) -> None:
    store = UserUsageRepository(db_engine)
    assert await store.reserve("user-1", DAY, 800, cap=1000)

    await store.settle("user-1", DAY, reserved=800, actual=150)
    assert await _usage(db_engine) == (0, 150)

    await store.settle("user-1", DAY, reserved=800, actual=0)
    assert await _usage(db_engine) == (0, 0)


async def test_should_settle_only_the_given_user_and_day(db_engine: AsyncEngine) -> None:
    store = UserUsageRepository(db_engine)
    assert await store.reserve("user-1", DAY, 300, cap=1000)
    assert await store.reserve("user-2", DAY, 300, cap=1000)

    await store.settle("user-1", DAY, reserved=300, actual=0)

    assert await _usage(db_engine, "user-1") == (0, 0)
    assert await _usage(db_engine, "user-2") == (0, 300)


async def test_should_never_exceed_the_cap_when_requests_reserve_concurrently(
    db_engine: AsyncEngine,
) -> None:
    store = UserUsageRepository(db_engine)

    granted = await asyncio.gather(
        *(store.reserve("user-1", DAY, 100, cap=1000) for _ in range(25))
    )

    assert sum(granted) == 10
    assert await _usage(db_engine) == (0, 1000)


async def test_should_count_every_message_when_accepted_concurrently(
    db_engine: AsyncEngine,
) -> None:
    store = UserUsageRepository(db_engine)

    counts = await asyncio.gather(*(store.accept_message("user-1", DAY) for _ in range(20)))

    assert sorted(counts) == list(range(1, 21))
