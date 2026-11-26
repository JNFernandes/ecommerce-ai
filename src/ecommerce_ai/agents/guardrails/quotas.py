"""Per-user quotas and cooldown (US5, FR-014..017, research §9).

``UserQuotas`` answers whether a user may post (cooldown → minute → day) or decide an approval
(cooldown → minute), with the seconds to wait. The minute window is the in-process
``RateWindow``; the day count and the cooldown come from the persisted stores, so they survive
restarts and deleting a conversation. A user who collects ``threshold`` input blocks of at least
``medium`` severity within ``violation_window`` gets one ``cooldown`` event; the cooldown is
active while the newest such event is younger than ``cooldown``.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import Final

from ecommerce_ai.agents.guardrails.events import (
    GuardAction,
    GuardEvent,
    GuardrailEventStore,
    GuardSeverity,
    GuardStage,
    RateWindow,
    UserUsageStore,
)
from ecommerce_ai.agents.ports import Clock

COOLDOWN_RULE: Final = "cooldown"
COOLDOWN_EVENT: Final = GuardEvent(
    stage=GuardStage.INPUT,
    rule=COOLDOWN_RULE,
    action=GuardAction.COOLDOWN,
    severity=GuardSeverity.HIGH,
)
"""The audit row that starts a user's cooldown."""
COUNTED_SEVERITY: Final = GuardSeverity.MEDIUM


class QuotaScope(StrEnum):
    """Whose limit was reached: the user's own or the service-wide one."""

    USER = "user"
    GLOBAL = "global"


class QuotaReason(StrEnum):
    """Which per-user limit refused the request."""

    COOLDOWN = "cooldown"
    MINUTE = "minute"
    DAY = "day"


@dataclass(frozen=True, slots=True)
class QuotaVerdict:
    """A refusal: why, and the whole seconds (≥ 1) until the user may try again."""

    reason: QuotaReason
    retry_after: int
    scope: QuotaScope = QuotaScope.USER


class QuotaExceeded(Exception):
    """A per-user limit refused the request (429 ``RATE_LIMITED`` with ``scope``)."""

    def __init__(self, verdict: QuotaVerdict) -> None:
        """Carry ``verdict``."""
        super().__init__(verdict.reason.value)
        self.verdict = verdict


@dataclass(frozen=True, slots=True)
class QuotaLimits:
    """``USER_MESSAGES_PER_DAY`` and the ``GUARD_*`` cooldown settings."""

    messages_per_day: int = 200
    violation_threshold: int = 5
    violation_window: timedelta = timedelta(seconds=600)
    cooldown: timedelta = timedelta(seconds=600)


def _whole_seconds(delta: timedelta) -> int:
    return max(1, math.ceil(delta.total_seconds()))


def seconds_to_midnight(now: datetime) -> int:
    """Whole seconds (≥ 1) from ``now`` to the next UTC midnight."""
    midnight = datetime.combine(now.date() + timedelta(days=1), time(), tzinfo=now.tzinfo)
    return _whole_seconds(midnight - now)


class UserQuotas:
    """The per-user checks before a post or a decision, and the cooldown trigger."""

    def __init__(
        self,
        window: RateWindow,
        usage: UserUsageStore | None,
        events: GuardrailEventStore | None,
        clock: Clock,
        limits: QuotaLimits | None = None,
    ) -> None:
        """Count minutes in ``window``, days in ``usage``, cooldowns in ``events``.

        ``None`` stores turn the day limit or the cooldown off (no storage configured).
        """
        self._window = window
        self._usage = usage
        self._events = events
        self._clock = clock
        self._limits = limits or QuotaLimits()

    async def cooldown(self, user_id: str) -> QuotaVerdict | None:
        """The cooldown refusal while one is active, else ``None``."""
        if self._events is None:
            return None
        started = await self._events.latest_cooldown_at(user_id)
        if started is None:
            return None
        remaining = started + self._limits.cooldown - self._clock.now()
        if remaining <= timedelta():
            return None
        return QuotaVerdict(QuotaReason.COOLDOWN, _whole_seconds(remaining))

    def minute(self, user_id: str) -> QuotaVerdict | None:
        """Count one hit in the minute window; the refusal when it is full."""
        if self._window.hit(user_id):
            return None
        return QuotaVerdict(QuotaReason.MINUTE, max(1, self._window.retry_after(user_id)))

    async def day(self, user_id: str) -> QuotaVerdict | None:
        """Count one accepted message today; the refusal once the day limit is passed."""
        if self._usage is None:
            return None
        now = self._clock.now()
        count = await self._usage.accept_message(user_id, now.date())
        if count <= self._limits.messages_per_day:
            return None
        return QuotaVerdict(QuotaReason.DAY, seconds_to_midnight(now))

    async def before_request(self, user_id: str) -> None:
        """Cooldown, then the minute window; raise ``QuotaExceeded`` on the first refusal."""
        verdict = await self.cooldown(user_id) or self.minute(user_id)
        if verdict is not None:
            raise QuotaExceeded(verdict)

    async def before_message(self, user_id: str) -> None:
        """The day limit of a post; raise ``QuotaExceeded`` when it is passed."""
        verdict = await self.day(user_id)
        if verdict is not None:
            raise QuotaExceeded(verdict)

    async def after_run(self, user_id: str, events: Iterable[GuardEvent]) -> GuardEvent | None:
        """Start a cooldown when the run's blocks bring the user to the threshold (FR-016).

        Return the ``cooldown`` event to store, or ``None``. Only input ``blocked`` events of at
        least ``medium`` severity count; no new cooldown starts while one is active.
        """
        if self._events is None or not any(_counts(event) for event in events):
            return None
        if await self.cooldown(user_id) is not None:
            return None
        since = self._clock.now() - self._limits.violation_window
        blocks = await self._events.count_blocks_since(user_id, since, COUNTED_SEVERITY)
        return COOLDOWN_EVENT if blocks >= self._limits.violation_threshold else None


def _counts(event: GuardEvent) -> bool:
    return (
        event.stage is GuardStage.INPUT
        and event.action is GuardAction.BLOCKED
        and event.severity.rank >= COUNTED_SEVERITY.rank
    )
