"""``UsageSummaryRepository`` on real PostgreSQL: SQL aggregates per day, model and kind."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.guardrails.events import GuardAction, GuardEvent, GuardSeverity, GuardStage
from ecommerce_ai.agents.ports import LlmCall, LlmCallKind, LlmCallOutcome
from ecommerce_ai.agents.usage import UsageGroupBy
from ecommerce_ai.infrastructure.db.guardrail_event_repository import GuardrailEventRepository
from ecommerce_ai.infrastructure.db.llm_call_repository import LlmCallRepository
from ecommerce_ai.infrastructure.db.usage_summary_repository import UsageSummaryRepository

pytestmark = pytest.mark.integration

DAY_1 = date(2026, 11, 20)
DAY_2 = date(2026, 11, 21)
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _chat(outcome: LlmCallOutcome, model: str | None, latency: int, cost: str | None) -> LlmCall:
    return LlmCall(
        kind=LlmCallKind.CHAT,
        conversation_id=uuid4(),
        model_id=model,
        prompt_version="v4",
        input_tokens=100,
        output_tokens=10,
        estimated_cost_usd=None if cost is None else Decimal(cost),
        latency_ms=latency,
        outcome=outcome,
    )


def _triage(outcome: LlmCallOutcome, model: str | None = None, latency: int = 0) -> LlmCall:
    return LlmCall(
        kind=LlmCallKind.TRIAGE,
        case_id=uuid4(),
        model_id=model,
        prompt_version="triage.v1",
        latency_ms=latency,
        outcome=outcome,
    )


async def _record(db_engine: AsyncEngine, when: datetime, *calls: LlmCall) -> None:
    repository = LlmCallRepository(db_engine, now=lambda: when)
    for call in calls:
        await repository.record(call)


async def _block(db_engine: AsyncEngine, when: datetime, action: GuardAction) -> None:
    event = GuardEvent(
        stage=GuardStage.INPUT, rule="override", action=action, severity=GuardSeverity.HIGH
    )
    await GuardrailEventRepository(db_engine, now=lambda: when).append("u", None, None, [event])


@pytest.fixture
async def seeded(db_engine: AsyncEngine) -> UsageSummaryRepository:
    """Day 1: four chat calls (p50 300 ms, one timeout, one unpriced); day 2: triage, a fallback.

    Plus one call the day before and one the day after the range, and guard events.
    """
    await _record(
        db_engine,
        _at(DAY_1),
        _chat(LlmCallOutcome.OK, SONNET, 100, "0.001000"),
        _chat(LlmCallOutcome.OK, SONNET, 300, "0.002000"),
        _chat(LlmCallOutcome.TIMEOUT, None, 500, None),
        _chat(LlmCallOutcome.OK, "claude-unpriced", 700, None),
    )
    await _record(
        db_engine,
        _at(DAY_2, 23),
        _triage(LlmCallOutcome.OK, HAIKU, 200),
        _triage(LlmCallOutcome.BUDGET),
        _triage(LlmCallOutcome.FALLBACK),
    )
    await _record(db_engine, _at(DAY_1 - timedelta(days=1)), _triage(LlmCallOutcome.ERROR))
    await _record(db_engine, _at(DAY_2 + timedelta(days=1), 0), _triage(LlmCallOutcome.ERROR))
    await _block(db_engine, _at(DAY_1), GuardAction.BLOCKED)
    await _block(db_engine, _at(DAY_1), GuardAction.BLOCKED)
    await _block(db_engine, _at(DAY_2), GuardAction.REPLACED)  # not a block
    await _block(db_engine, _at(DAY_2 + timedelta(days=1)), GuardAction.BLOCKED)  # out of range
    return UsageSummaryRepository(db_engine)


async def test_should_aggregate_every_day_of_the_range_when_grouped_by_day(
    seeded: UsageSummaryRepository,
) -> None:
    summary = await seeded.summarize(DAY_1, DAY_2, UsageGroupBy.DAY)

    first, second = summary.groups
    assert (first.key, second.key) == (DAY_1.isoformat(), DAY_2.isoformat())
    assert (first.calls, first.input_tokens, first.output_tokens) == (4, 400, 40)
    assert first.estimated_cost_usd == Decimal("0.003000")
    assert first.latency_p50_ms == 400  # median of 100, 300, 500, 700
    assert first.latency_p95_ms == 670
    assert (first.error_rate, first.fallback_rate, first.guardrail_blocks) == (0.25, 0.0, 2)
    assert second.calls == 3
    assert (second.latency_p50_ms, second.latency_p95_ms) == (200, 200)  # only the sent call
    assert second.fallback_rate == pytest.approx(1 / 3)
    assert (second.error_rate, second.guardrail_blocks) == (0.0, 0)
    totals = summary.totals
    assert (totals.calls, totals.guardrail_blocks) == (7, 2)
    assert totals.estimated_cost_usd == Decimal("0.003000")
    assert totals.error_rate == pytest.approx(1 / 7)


async def test_should_group_by_model_with_no_model_last_and_no_guard_counts(
    seeded: UsageSummaryRepository,
) -> None:
    summary = await seeded.summarize(DAY_1, DAY_2, UsageGroupBy.MODEL)

    keys = [group.key for group in summary.groups]
    assert keys == ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-unpriced", None]
    by_key = {group.key: group for group in summary.groups}
    assert by_key[SONNET].calls == 2
    assert by_key[SONNET].estimated_cost_usd == Decimal("0.003000")
    assert by_key[None].calls == 3  # timeout, budget, fallback
    assert by_key[None].latency_p50_ms == 500  # the budget and fallback rows are left out
    assert all(group.guardrail_blocks is None for group in summary.groups)
    assert summary.totals.guardrail_blocks == 2


async def test_should_group_by_kind_when_asked(seeded: UsageSummaryRepository) -> None:
    summary = await seeded.summarize(DAY_1, DAY_2, UsageGroupBy.KIND)

    assert [(group.key, group.calls) for group in summary.groups] == [("chat", 4), ("triage", 3)]


async def test_should_return_zero_calls_and_null_rates_when_the_range_is_empty(
    db_engine: AsyncEngine,
) -> None:
    summary = await UsageSummaryRepository(db_engine).summarize(DAY_1, DAY_1, UsageGroupBy.DAY)

    [day] = summary.groups
    for stats in (day, summary.totals):
        assert (stats.calls, stats.input_tokens, stats.output_tokens) == (0, 0, 0)
        assert stats.estimated_cost_usd == Decimal("0.000000")
        assert (stats.latency_p50_ms, stats.latency_p95_ms) == (None, None)
        assert (stats.error_rate, stats.fallback_rate, stats.guardrail_blocks) == (None, None, 0)
    model = await UsageSummaryRepository(db_engine).summarize(DAY_1, DAY_1, UsageGroupBy.MODEL)
    assert model.groups == ()
