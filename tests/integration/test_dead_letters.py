"""Inbound dead letters keep the raw message exactly as it was consumed."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.triage_intake.ports import DeadLetterStore
from ecommerce_ai.infrastructure.db.dead_letter_repository import DeadLetterRepository
from ecommerce_ai.infrastructure.db.tables import inbound_dead_letters

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


async def _rows(engine: AsyncEngine) -> list[dict[str, object]]:
    async with engine.connect() as connection:
        rows = await connection.execute(select(inbound_dead_letters))
        return [dict(row._mapping) for row in rows]


async def test_should_store_raw_bytes_key_and_reason_when_a_message_is_dead_lettered(
    db_engine: AsyncEngine,
) -> None:
    store: DeadLetterStore = DeadLetterRepository(db_engine, lambda: NOW)

    await store.add("payments.payment-failed", 2, 41, b"\x00key", b"\xff\xfe not utf-8", "bad")

    (row,) = await _rows(db_engine)
    assert row["topic"] == "payments.payment-failed"
    assert (row["partition"], row["offset"]) == (2, 41)
    assert row["message_key"] == b"\x00key"
    assert row["raw_payload"] == b"\xff\xfe not utf-8"
    assert row["failure_reason"] == "bad"
    assert row["created_at"] == NOW


async def test_should_keep_a_missing_key_and_cut_a_long_reason(db_engine: AsyncEngine) -> None:
    store = DeadLetterRepository(db_engine)

    await store.add("orders.order-cancelled", 0, 0, None, b"{}", "x" * 600)
    await store.add("orders.order-cancelled", 0, 0, None, b"{}", "again")

    rows = await _rows(db_engine)
    assert [row["message_key"] for row in rows] == [None, None]
    assert sorted(len(str(row["failure_reason"])) for row in rows) == [5, 500]
