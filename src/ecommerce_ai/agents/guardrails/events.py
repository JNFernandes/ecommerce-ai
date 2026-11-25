"""Guard events and the ports the guardrails reach the outside world through (research §9/§10).

A ``GuardEvent`` says which guard acted (``stage``, ``rule``), what it did (``action``) and how
serious the attempt was (``severity``). It never holds message text, tool data, a canary or a
token (SC-007), so it can be stored and logged as is. ``GuardrailEventStore`` keeps the
append-only audit, ``UserUsageStore`` the per-user day counters and ``RateWindow`` the in-process
minute window.
"""

from collections.abc import Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GuardStage(StrEnum):
    """Where a guard ran: on the user's input, on a tool call or result, or on the answer."""

    INPUT = "input"
    TOOL = "tool"
    OUTPUT = "output"


class GuardAction(StrEnum):
    """What a guard did."""

    BLOCKED = "blocked"
    REPLACED = "replaced"
    SANITIZED = "sanitized"
    COOLDOWN = "cooldown"


class GuardSeverity(StrEnum):
    """How serious the attempt was; ``medium`` and ``high`` input blocks count towards cooldown."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        """Order of the severity: ``low`` < ``medium`` < ``high``."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {GuardSeverity.LOW: 0, GuardSeverity.MEDIUM: 1, GuardSeverity.HIGH: 2}


class GuardEvent(BaseModel):
    """One action of one guard; content-free, so it is safe to store and log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: GuardStage = Field(description="Where the guard ran.")
    rule: str = Field(min_length=1, max_length=64, description="Name of the rule that matched.")
    action: GuardAction = Field(description="What the guard did.")
    severity: GuardSeverity = Field(description="How serious the attempt was.")


class GuardrailEventStore(Protocol):
    """Append-only audit of guard events (``guardrail_events``)."""

    async def append(
        self,
        user_id: str,
        conversation_id: UUID | None,
        request_id: str | None,
        events: Sequence[GuardEvent],
    ) -> None:
        """Store ``events`` of one run, stamped with the current UTC time."""
        ...

    async def count_blocks_since(
        self, user_id: str, since: datetime, min_severity: GuardSeverity
    ) -> int:
        """Input ``blocked`` events of ``user_id`` at or after ``since`` with ≥ ``min_severity``."""
        ...

    async def latest_cooldown_at(self, user_id: str) -> datetime | None:
        """Time of the user's newest ``cooldown`` event, ``None`` when there is none."""
        ...


class UserUsageStore(Protocol):
    """Per-user, per-UTC-day message and token counters (``chat_user_usage``)."""

    async def accept_message(self, user_id: str, day: date) -> int:
        """Count one accepted message on ``day``; return the day's count including it."""
        ...

    async def reserve(self, user_id: str, day: date, tokens: int, cap: int) -> bool:
        """Reserve ``tokens`` on ``day`` if the total stays ≤ ``cap``, else ``False``."""
        ...

    async def settle(self, user_id: str, day: date, reserved: int, actual: int) -> None:
        """Replace a reservation of ``reserved`` tokens with the ``actual`` spend (never < 0)."""
        ...


class RateWindow(Protocol):
    """A sliding window of hits per key, in process."""

    def hit(self, key: str) -> bool:
        """Count a hit and return ``True`` when it fits in the window."""
        ...

    def retry_after(self, key: str) -> int:
        """Whole seconds until ``key`` may hit again: ≥ 1 when the window is full, else 0."""
        ...
