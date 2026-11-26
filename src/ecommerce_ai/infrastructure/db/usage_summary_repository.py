"""The ADMIN usage summary over ``llm_calls`` and ``guardrail_events`` (US-08 FR-012).

Aggregation happens in SQL (``percentile_cont``, ``FILTER``) over the UTC days ``start`` to
``end``, both included. Grouped by day, every day of the range is returned (zero calls, ``None``
rates on a quiet day) with its ``blocked`` guard events; grouped by model or kind, only the keys
that have calls are, without guard counts. The totals always carry the range's guard blocks.
"""

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import ColumnElement, Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.guardrails.events import GuardAction
from ecommerce_ai.agents.ports import LlmCallOutcome
from ecommerce_ai.agents.usage import (
    UsageGroup,
    UsageGroupBy,
    UsageStats,
    UsageSummary,
)
from ecommerce_ai.infrastructure.db.tables import guardrail_events, llm_calls
from ecommerce_ai.infrastructure.pricing import COST_QUANTUM

_SENT: Final = (LlmCallOutcome.OK.value, LlmCallOutcome.TIMEOUT.value, LlmCallOutcome.ERROR.value)
_FAILED: Final = (LlmCallOutcome.TIMEOUT.value, LlmCallOutcome.ERROR.value)


def _utc_day(column: Any) -> ColumnElement[date]:  # noqa: ANN401  # any timestamptz column
    """The UTC calendar day of a ``timestamptz`` column."""
    return cast(func.timezone("UTC", column), Date)


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """``[start 00:00 UTC, end + 1 day 00:00 UTC)``."""
    return (
        datetime.combine(start, time(), UTC),
        datetime.combine(end + timedelta(days=1), time(), UTC),
    )


def _aggregates() -> list[ColumnElement[Any]]:
    sent = llm_calls.c.outcome.in_(_SENT)
    return [
        func.count().label("calls"),
        func.coalesce(func.sum(llm_calls.c.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(llm_calls.c.output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(llm_calls.c.estimated_cost_usd), 0).label("cost"),
        func.percentile_cont(0.5).within_group(llm_calls.c.latency_ms).filter(sent).label("p50"),
        func.percentile_cont(0.95).within_group(llm_calls.c.latency_ms).filter(sent).label("p95"),
        func.count().filter(llm_calls.c.outcome.in_(_FAILED)).label("failed"),
        func.count()
        .filter(llm_calls.c.outcome == LlmCallOutcome.FALLBACK.value)
        .label("fallbacks"),
    ]


def _rate(part: int, calls: int) -> float | None:
    return part / calls if calls else None


def _ms(value: float | None) -> int | None:
    return None if value is None else round(value)


def _stats(row: Mapping[str, Any] | None, blocks: int | None) -> dict[str, Any]:
    """The ``UsageStats`` fields of one aggregate row (``None``: no calls)."""
    if row is None:
        return {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": Decimal(0).quantize(COST_QUANTUM),
            "guardrail_blocks": blocks,
        }
    calls = int(row["calls"])
    return {
        "calls": calls,
        "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]),
        "estimated_cost_usd": Decimal(row["cost"]).quantize(COST_QUANTUM),
        "latency_p50_ms": _ms(row["p50"]),
        "latency_p95_ms": _ms(row["p95"]),
        "error_rate": _rate(int(row["failed"]), calls),
        "fallback_rate": _rate(int(row["fallbacks"]), calls),
        "guardrail_blocks": blocks,
    }


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


class UsageSummaryRepository:
    """``UsageSummaryStore`` over the async engine; one read-only connection per summary."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Read from ``engine``."""
        self._engine = engine

    async def summarize(self, start: date, end: date, group_by: UsageGroupBy) -> UsageSummary:
        """The summary of the UTC days ``start`` to ``end`` (included), grouped by ``group_by``."""
        low, high = _bounds(start, end)
        in_range = (llm_calls.c.created_at >= low, llm_calls.c.created_at < high)
        key = self._key(group_by)
        grouped = (
            select(key.label("key"), *_aggregates())
            .where(*in_range)
            .group_by(key)
            .order_by(key.asc().nulls_last())
        )
        totals = select(*_aggregates()).where(*in_range)
        block_day = _utc_day(guardrail_events.c.created_at)
        blocks = (
            select(block_day.label("day"), func.count().label("blocks"))
            .where(
                guardrail_events.c.action == GuardAction.BLOCKED.value,
                guardrail_events.c.created_at >= low,
                guardrail_events.c.created_at < high,
            )
            .group_by(block_day)
        )
        async with self._engine.connect() as connection:
            rows = [dict(row) for row in (await connection.execute(grouped)).mappings()]
            total_row = dict((await connection.execute(totals)).mappings().one())
            blocks_by_day = {
                row["day"]: int(row["blocks"])
                for row in (await connection.execute(blocks)).mappings()
            }
        total_blocks = sum(blocks_by_day.values())
        return UsageSummary(
            groups=tuple(self._groups(rows, group_by, start, end, blocks_by_day)),
            totals=UsageStats(**_stats(total_row if total_row["calls"] else None, total_blocks)),
        )

    @staticmethod
    def _key(group_by: UsageGroupBy) -> ColumnElement[Any]:
        if group_by is UsageGroupBy.DAY:
            return _utc_day(llm_calls.c.created_at)
        if group_by is UsageGroupBy.MODEL:
            return llm_calls.c.model_id
        return llm_calls.c.kind

    @staticmethod
    def _groups(
        rows: list[dict[str, Any]],
        group_by: UsageGroupBy,
        start: date,
        end: date,
        blocks_by_day: Mapping[date, int],
    ) -> list[UsageGroup]:
        if group_by is not UsageGroupBy.DAY:
            return [UsageGroup(key=row["key"], **_stats(row, None)) for row in rows]
        by_day = {row["key"]: row for row in rows}
        return [
            UsageGroup(key=day.isoformat(), **_stats(by_day.get(day), blocks_by_day.get(day, 0)))
            for day in _days(start, end)
        ]
