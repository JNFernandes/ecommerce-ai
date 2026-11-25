"""Ports the triage graph and worker need, implemented in ``infrastructure`` (constitution I).

- ``TriageModel``: one structured draft from the LLM (``infrastructure/llm``).
- ``TriageCaseQueue``: one unit of work per case: lock the oldest settled case, then save its
  result, outbox event and COMPLETED status together, or roll everything back (FR-001, FR-015).

``TokenBudget``, ``Clock``, ``SystemClock`` and the model errors live in ``agents/ports.py`` (shared
with the assistant) and are re-exported here.
"""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from ecommerce_ai.agents.ports import (
    Clock,
    ModelError,
    ModelOutputInvalid,
    ModelUnavailable,
    SystemClock,
    TokenBudget,
)
from ecommerce_ai.agents.triage.model import CaseForTriage, DraftOutput, TriageResult

__all__ = [
    "CaseClaim",
    "Clock",
    "ModelError",
    "ModelOutputInvalid",
    "ModelReply",
    "ModelUnavailable",
    "PromptInput",
    "SystemClock",
    "TokenBudget",
    "TriageCaseQueue",
    "TriageModel",
]


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


class TriageModel(Protocol):
    """The LLM behind the draft node."""

    async def draft(self, prompt_input: PromptInput) -> ModelReply:
        """Ask for one draft; raise ``ModelUnavailable`` or ``ModelOutputInvalid`` on failure."""
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
