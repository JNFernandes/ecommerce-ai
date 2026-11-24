"""Ports and errors shared by every agent (triage, assistant), implemented in ``infrastructure``.

- ``TokenBudget``: the daily token budget shared by every LLM step (``llm_token_usage``).
- ``Clock`` / ``SystemClock``: wall time for stored timestamps and a monotonic clock for latency.
- ``ModelError`` and its subclasses: a failed LLM call, carrying the tokens it spent.
"""

import time
from datetime import UTC, date, datetime
from typing import Protocol


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


class ModelOutputInvalid(ModelError):
    """The model answered, but not with a valid output (refusal, truncation, schema error)."""


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
