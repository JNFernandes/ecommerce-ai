"""``LlmCallRepository`` on real PostgreSQL: rows as recorded, constraints of migration 0006."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.ports import LlmCall, LlmCallKind, LlmCallOutcome
from ecommerce_ai.infrastructure.db.llm_call_repository import LlmCallRepository
from ecommerce_ai.infrastructure.db.tables import llm_calls

pytestmark = pytest.mark.integration

STAMP = datetime(2026, 11, 26, 9, 30, tzinfo=UTC)


async def _rows(db_engine: AsyncEngine) -> list[dict[str, object]]:
    async with db_engine.connect() as connection:
        result = await connection.execute(select(llm_calls))
        return [dict(row) for row in result.mappings()]


async def test_should_store_a_chat_and_a_triage_call_when_recorded(db_engine: AsyncEngine) -> None:
    repository = LlmCallRepository(db_engine, now=lambda: STAMP)
    conversation_id, case_id = uuid4(), uuid4()

    await repository.record(
        LlmCall(
            kind=LlmCallKind.CHAT,
            conversation_id=conversation_id,
            request_id="req-1",
            model_id="claude-sonnet-5-5",
            prompt_version="v4",
            input_tokens=1200,
            output_tokens=80,
            estimated_cost_usd=Decimal("0.003200"),
            latency_ms=812,
            outcome=LlmCallOutcome.OK,
        )
    )
    await repository.record(
        LlmCall(
            kind=LlmCallKind.TRIAGE,
            case_id=case_id,
            prompt_version="triage.v1",
            outcome=LlmCallOutcome.FALLBACK,
        )
    )

    chat, triage = sorted(await _rows(db_engine), key=lambda row: str(row["kind"]))
    assert chat["conversation_id"] == conversation_id
    assert chat["case_id"] is None
    assert (chat["model_id"], chat["input_tokens"], chat["output_tokens"]) == (
        "claude-sonnet-5-5",
        1200,
        80,
    )
    assert chat["estimated_cost_usd"] == Decimal("0.003200")
    assert (chat["latency_ms"], chat["outcome"], chat["created_at"]) == (812, "ok", STAMP)
    assert triage["case_id"] == case_id
    assert (triage["model_id"], triage["estimated_cost_usd"], triage["outcome"]) == (
        None,
        None,
        "fallback",
    )


async def test_should_stamp_the_time_when_a_row_is_inserted_without_one(
    db_engine: AsyncEngine,
) -> None:
    async with db_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO llm_calls (id, kind, case_id, prompt_version, input_tokens,"
                " output_tokens, latency_ms, outcome)"
                " VALUES (:id, 'triage', :case, 'triage.v1', 0, 0, 0, 'budget')"
            ),
            {"id": uuid4(), "case": uuid4()},
        )

    [row] = await _rows(db_engine)
    assert isinstance(row["created_at"], datetime)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": uuid4(),
        "kind": "chat",
        "conversation_id": uuid4(),
        "case_id": None,
        "prompt_version": "v4",
        "input_tokens": 1,
        "output_tokens": 1,
        "estimated_cost_usd": Decimal("0.000001"),
        "latency_ms": 1,
        "outcome": "ok",
        "created_at": STAMP,
    }
    return row | overrides


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "batch"},
        {"outcome": "aborted"},
        {"case_id": uuid4()},  # both subjects
        {"conversation_id": None},  # no subject
        {"input_tokens": -1},
        {"output_tokens": -1},
        {"latency_ms": -1},
        {"estimated_cost_usd": Decimal("-0.000001")},
    ],
)
async def test_should_reject_a_row_when_a_value_breaks_a_constraint(
    db_engine: AsyncEngine, overrides: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError):
        async with db_engine.begin() as connection:
            await connection.execute(llm_calls.insert(), _row(**overrides))

    assert await _rows(db_engine) == []
