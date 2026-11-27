"""Per-call usage recording shared by both agents (US-08 FR-010, FR-011).

``LlmUsage`` prices a call (``CostEstimator``) and stores its ``LlmCall`` (``LlmCallRecorder``).
Recording fails open: a failure is logged by class name only and never changes the run's result.
``LlmUsage()`` with neither port records nothing and prices nothing.

``UsageSummary`` / ``UsageSummaryStore``: the ADMIN usage summary (``GET /usage/summary``).
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field

from ecommerce_ai.agents.ports import CostEstimator, LlmCall, LlmCallRecorder

_logger = structlog.get_logger(__name__)


class LlmUsage:
    """Prices and records the model calls of a graph."""

    def __init__(
        self, recorder: LlmCallRecorder | None = None, prices: CostEstimator | None = None
    ) -> None:
        """Store records in ``recorder`` (``None``: off), priced by ``prices`` (``None``: none)."""
        self._recorder = recorder
        self._prices = prices

    def cost(self, model_id: str | None, input_tokens: int, output_tokens: int) -> Decimal | None:
        """Estimated USD cost of a call, ``None`` when it cannot be priced."""
        if self._prices is None:
            return None
        return self._prices.estimate_cost(model_id, input_tokens, output_tokens)

    async def record(self, call: LlmCall) -> None:
        """Store ``call``; a failure is logged (class name only) and does not propagate."""
        if self._recorder is None:
            return
        try:
            await self._recorder.record(call)
        except Exception as error:
            _logger.warning(
                "llm_call_record_failed",
                error_type=type(error).__name__,
                kind=call.kind.value,
                outcome=call.outcome.value,
            )


class UsageGroupBy(StrEnum):
    """How the usage summary groups its calls."""

    DAY = "day"
    MODEL = "model"
    KIND = "kind"


class UsageStats(BaseModel):
    """Aggregates over a set of usage records (US-08 FR-012).

    Rates are shares of ``calls`` (``timeout`` + ``error`` for the error rate), ``None`` without
    calls; latency percentiles cover the calls that reached the model (``ok``, ``timeout``,
    ``error``), ``None`` when there are none. ``guardrail_blocks`` counts ``blocked`` guard events
    and is ``None`` where the audit cannot be split that way (per model or kind).
    """

    model_config = ConfigDict(frozen=True)

    calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_usd: Decimal = Field(ge=0)
    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None
    error_rate: float | None = None
    fallback_rate: float | None = None
    guardrail_blocks: int | None = None


class UsageGroup(UsageStats):
    """The aggregates of one group: a day (ISO date), a model id (``None``: none) or a kind."""

    key: str | None


class UsageSummary(BaseModel):
    """Every group of the range, in key order, and the totals of the range."""

    model_config = ConfigDict(frozen=True)

    groups: tuple[UsageGroup, ...]
    totals: UsageStats


class UsageSummaryStore(Protocol):
    """Aggregates the usage records and guard audit (``UsageSummaryRepository``)."""

    async def summarize(self, start: date, end: date, group_by: UsageGroupBy) -> UsageSummary:
        """The summary of the UTC days ``start`` to ``end``, both included."""
        ...
