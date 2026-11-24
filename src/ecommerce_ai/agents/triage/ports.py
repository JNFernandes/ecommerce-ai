"""Ports the triage graph and worker need, implemented in ``infrastructure`` (constitution I).

- ``TriageModel``: one structured draft from the LLM (``infrastructure/llm``).
- ``TokenBudget``: the daily token budget shared by every worker (``llm_token_usage``).
- ``TriageCaseQueue``: one unit of work per case: lock the oldest settled case, then save its
  result, outbox event and COMPLETED status together, or roll everything back (FR-001, FR-015).
- ``Clock``: wall time for stored timestamps and a monotonic clock for latency.
"""

import time
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from ecommerce_ai.agents.triage.model import CaseForTriage, DraftOutput, TriageResult


class PromptInput(BaseModel):
    """The rendered prompt of one draft attempt."""

    model_config = ConfigDict(frozen=True)

    system: str = Field(min_length=1, description="System part: role and rules.")
    user: str = Field(min_length=1, description="User part: case, evidence and feedback.")


class ModelReply(BaseModel):
    """A validated draft with the tokens it cost and the model that wrote it."""

    model_config = ConfigDict(frozen=True)

    draft: DraftOutput = Field(description="The structured answer, already validated.")
    input_tokens: int = Field(ge=0, description="Input tokens of the call.")
    output_tokens: int = Field(ge=0, description="Output tokens of the call.")
    model_id: str = Field(min_length=1, description="Model id reported for the call.")


class ModelError(Exception):
    """A failed draft attempt; carries the tokens it spent (0 when unknown).

    The message is a short fixed description: never prompt text, evidence or model output.
    """

    def __init__(self, message: str, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class ModelUnavailable(ModelError):
    """The model could not answer (timeout, connection or API error); worth retrying."""


class ModelOutputInvalid(ModelError):
    """The model answered, but not with a valid draft (refusal, truncation, schema error)."""


class TriageModel(Protocol):
    """The LLM behind the draft node."""

    async def draft(self, prompt_input: PromptInput) -> ModelReply:
        """Ask for one draft; raise ``ModelUnavailable`` or ``ModelOutputInvalid`` on failure."""
        ...


class TokenBudget(Protocol):
    """Daily token budget kept in the database, so it holds across restarts and workers."""

    async def reserve(self, day: date, cap: int) -> bool:
        """Reserve ``cap`` tokens on ``day``; ``False`` (nothing reserved) when it would not fit."""
        ...

    async def settle(self, day: date, reserved: int, actual: int) -> None:
        """Replace a reservation of ``reserved`` tokens on ``day`` with the ``actual`` spend."""
        ...


class CaseClaim(Protocol):
    """A unit of work holding at most one locked, settled case."""

    @property
    def case(self) -> CaseForTriage | None:
        """The locked case, or ``None`` when no case is settled."""
        ...

    async def complete(self, result: TriageResult) -> None:
        """Save the result and its outbox event and mark the case COMPLETED (same transaction)."""
        ...


class TriageCaseQueue(Protocol):
    """Source of settled cases, one transaction per case."""

    def next_settled(self, settle_before: datetime) -> AbstractAsyncContextManager[CaseClaim]:
        """Lock the oldest PENDING_TRIAGE case last updated at or before ``settle_before``.

        Concurrent callers skip a locked case. Everything done through the claim commits when the
        block exits normally and rolls back when it raises.
        """
        ...


class Clock(Protocol):
    """Time source of the triage graph and worker."""

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
