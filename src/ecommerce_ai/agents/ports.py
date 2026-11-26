"""Ports and errors shared by every agent (triage, assistant), implemented in ``infrastructure``.

- ``TokenBudget``: the daily token budget shared by every LLM step (``llm_token_usage``).
- ``Clock`` / ``SystemClock``: wall time for stored timestamps and a monotonic clock for latency.
- ``ModelError`` and its subclasses: a failed LLM call, carrying the tokens it spent.
- ``LlmCallOutcome`` / ``llm_outcome``: how one model call ended (spans and usage records, US-08).
- ``LlmCall`` / ``LlmCallRecorder`` / ``CostEstimator``: per-call usage records and their cost.
"""

import time
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelError(Exception):
    """A failed model call; carries the tokens it spent (0 when unknown).

    The message is a short fixed description: never prompt text, evidence or model output.
    """

    def __init__(self, message: str, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class ModelUnavailable(ModelError):
    """The model could not answer (timeout, connection or API error); worth retrying."""


class ModelTimeout(ModelUnavailable):
    """The model did not answer within the per-call timeout; retried like any unavailability."""


class ModelOutputInvalid(ModelError):
    """The model answered, but not with a valid output (refusal, truncation, schema error)."""


class LlmCallOutcome(StrEnum):
    """How one model call ended (``llm.outcome`` on its span, ``outcome`` of its usage record)."""

    OK = "ok"
    TIMEOUT = "timeout"
    ERROR = "error"
    BUDGET = "budget"
    """The token budget refused the call: no request was sent."""
    FALLBACK = "fallback"
    """A triage run ended on the fixed template (one per run, no call of its own)."""


def llm_outcome(error: ModelError) -> LlmCallOutcome:
    """The outcome of a call that failed with ``error``: ``timeout`` or ``error``."""
    return LlmCallOutcome.TIMEOUT if isinstance(error, ModelTimeout) else LlmCallOutcome.ERROR


class LlmCallKind(StrEnum):
    """Which agent made a model call."""

    CHAT = "chat"
    TRIAGE = "triage"


class LlmCall(BaseModel):
    """Usage record of one model call (``llm_calls``, US-08 FR-010): ids and numbers only.

    A chat call names its conversation, a triage call its case; never both.
    """

    model_config = ConfigDict(frozen=True)

    kind: LlmCallKind = Field(description="The agent that made the call.")
    conversation_id: UUID | None = Field(default=None, description="Chat conversation, if chat.")
    case_id: UUID | None = Field(default=None, description="Triage case, if triage.")
    request_id: str | None = Field(default=None, description="HTTP request id (chat only).")
    model_id: str | None = Field(
        default=None, description="Model that answered (none when nothing was sent)."
    )
    prompt_version: str = Field(min_length=1, max_length=32, description="Prompt version used.")
    input_tokens: int = Field(default=0, ge=0, description="Input tokens spent.")
    output_tokens: int = Field(default=0, ge=0, description="Output tokens spent.")
    estimated_cost_usd: Decimal | None = Field(
        default=None, ge=0, description="Estimated USD cost; none for an unpriced model."
    )
    latency_ms: int = Field(default=0, ge=0, description="Wall time of the call.")
    outcome: LlmCallOutcome = Field(description="How the call ended.")

    @model_validator(mode="after")
    def _check_subject(self) -> Self:
        chat = self.kind is LlmCallKind.CHAT
        subject, other = (
            (self.conversation_id, self.case_id) if chat else (self.case_id, self.conversation_id)
        )
        if subject is None or other is not None:
            raise ValueError("a chat call names a conversation, a triage call a case")
        return self


class LlmCallRecorder(Protocol):
    """Stores usage records (``LlmCallRepository``)."""

    async def record(self, call: LlmCall) -> None:
        """Store ``call``."""
        ...


class CostEstimator(Protocol):
    """Estimates the USD cost of a call from its tokens (``ModelPrices``)."""

    def estimate_cost(
        self, model_id: str | None, input_tokens: int, output_tokens: int
    ) -> Decimal | None:
        """USD cost, ``None`` without a model or for an unpriced model."""
        ...


class TokenBudget(Protocol):
    """Daily token budget kept in the database, so it holds across restarts and workers."""

    async def reserve(self, day: date, cap: int) -> bool:
        """Reserve ``cap`` tokens on ``day``; ``False`` (nothing reserved) when it would not fit."""
        ...

    async def settle(self, day: date, reserved: int, actual: int) -> None:
        """Replace a reservation of ``reserved`` tokens on ``day`` with the ``actual`` spend."""
        ...


class Clock(Protocol):
    """Time source of the agents and their workers."""

    def now(self) -> datetime:
        """Current UTC time, timezone-aware."""
        ...

    def monotonic(self) -> float:
        """Seconds of a monotonic clock, for durations only."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        """Current UTC time, timezone-aware."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Seconds of ``time.monotonic``."""
        return time.monotonic()
