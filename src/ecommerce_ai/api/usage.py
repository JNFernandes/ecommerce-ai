"""``GET /usage/summary``: LLM usage and cost for operators (US-08 FR-012), ``ADMIN`` only.

I/O only: authentication (``require_caller`` with ``ADMIN``), query validation and the
camelCase response; the aggregation is the ``UsageSummaryStore``'s. ``from``/``to`` are UTC
dates, both included (default: the last 7 days up to today); the range spans at most
``MAX_RANGE_DAYS`` days. The gateway serves it as ``/api/ai/usage/summary``.
"""

from collections.abc import AsyncIterator, Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from ecommerce_ai.agents.usage import UsageGroupBy, UsageStats, UsageSummaryStore
from ecommerce_ai.api.route_policy import roles
from ecommerce_ai.infrastructure.caller import RequestCaller

PREFIX: Final = "/usage"
ADMIN_ROLE: Final = "ADMIN"
DEFAULT_RANGE_DAYS: Final = 7
MAX_RANGE_DAYS: Final = 90


class _Camel(BaseModel):
    model_config = ConfigDict(frozen=True, alias_generator=to_camel, populate_by_name=True)


class UsageStatsResponse(_Camel):
    """Aggregates of a group or of the whole range."""

    calls: int = Field(description="Model calls recorded, refused and fallback ones included.")
    input_tokens: int = Field(description="Input tokens spent.")
    output_tokens: int = Field(description="Output tokens spent.")
    estimated_cost_usd: Decimal = Field(
        description="Estimated USD cost (a decimal string); unpriced calls count as zero."
    )
    latency_p50_ms: int | None = Field(
        description="Median latency of the calls that reached the model; null without any."
    )
    latency_p95_ms: int | None = Field(
        description="95th percentile latency of the calls that reached the model."
    )
    error_rate: float | None = Field(
        description="Share of calls that timed out or failed; null without calls."
    )
    fallback_rate: float | None = Field(
        description="Share of calls that are triage fallbacks; null without calls."
    )
    guardrail_blocks: int | None = Field(
        description="Messages blocked by the guardrails; null when grouped by model or kind."
    )

    @classmethod
    def of(cls, stats: UsageStats) -> "UsageStatsResponse":
        """The response form of ``stats``."""
        return cls.model_validate(stats.model_dump(exclude={"key"}))


class UsageGroupResponse(UsageStatsResponse):
    """One group of the summary."""

    key: str | None = Field(
        description="The ISO day, the model id (null: no model answered) or the kind."
    )


class UsageSummaryResponse(_Camel):
    """The usage summary of a range of UTC days."""

    from_: date = Field(alias="from", description="First UTC day, included.")
    to: date = Field(description="Last UTC day, included.")
    group_by: UsageGroupBy = Field(description="How the groups are formed.")
    groups: tuple[UsageGroupResponse, ...] = Field(description="The groups, in key order.")
    totals: UsageStatsResponse = Field(description="Aggregates of the whole range.")


def _range(start: date | None, end: date | None, today: date) -> tuple[date, date]:
    """The requested range with its defaults; 422 when it is reversed or too long."""
    last = end or today
    first = start or last - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if first > last:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="`from` must not be after `to`."
        )
    if (last - first).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"The range spans more than {MAX_RANGE_DAYS} days.",
        )
    return first, last


def build_usage_router(
    store: UsageSummaryStore,
    caller_dependency: Callable[[Request], AsyncIterator[RequestCaller]],
    *,
    clock: Callable[[], datetime],
    is_unavailable: Callable[[BaseException], bool],
) -> APIRouter:
    """Return the ``/usage`` router over ``store``; ``clock`` (UTC) gives today's date."""
    router = APIRouter(prefix=PREFIX, tags=["usage"])
    Admin = Annotated[RequestCaller, Depends(caller_dependency)]  # a type alias

    @router.get(
        "/summary",
        summary="Summarize LLM usage and cost",
        description=(
            "Calls, tokens, estimated cost, latency percentiles, error and fallback rates and "
            "guardrail blocks over a range of UTC days, grouped by day, model or kind. "
            "Requires the `ADMIN` role."
        ),
        responses={
            401: {"description": "Missing or invalid token."},
            403: {"description": "The token lacks the `ADMIN` role."},
            422: {"description": "Invalid dates, `from` after `to` or a range over 90 days."},
            503: {"description": "Storage or signing keys unavailable."},
        },
        openapi_extra=roles(ADMIN_ROLE),
    )
    async def usage_summary(
        _caller: Admin,
        start: Annotated[
            date | None, Query(alias="from", description="First UTC day (ISO date), included.")
        ] = None,
        end: Annotated[
            date | None, Query(alias="to", description="Last UTC day (ISO date), included.")
        ] = None,
        group_by: Annotated[
            UsageGroupBy, Query(alias="groupBy", description="Group by `day`, `model` or `kind`.")
        ] = UsageGroupBy.DAY,
    ) -> UsageSummaryResponse:
        first, last = _range(start, end, clock().date())
        try:
            summary = await store.summarize(first, last, group_by)
        except Exception as exc:
            if is_unavailable(exc):
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable"
                ) from exc
            raise
        return UsageSummaryResponse(
            from_=first,
            to=last,
            group_by=group_by,
            groups=tuple(
                UsageGroupResponse.model_validate(group.model_dump()) for group in summary.groups
            ),
            totals=UsageStatsResponse.of(summary.totals),
        )

    return router
