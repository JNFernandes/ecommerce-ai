"""Entry point of the triage intake: record one validated failure event (FR-004, FR-005, FR-008).

Each call is one unit of work on the ``TriageCaseStore``: the order's case is created or locked,
the event is attached as evidence and the cause merged by precedence, all saved together or not
at all. Messaging commits the Kafka offset only after ``record`` returns.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from ecommerce_ai.agents.triage_intake.cause import derive_cause, merge_cause
from ecommerce_ai.agents.triage_intake.model import IntakeOutcome, TriageEventInput
from ecommerce_ai.agents.triage_intake.ports import TriageCaseStore


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TriageIntakeService:
    """Turns failure events into triage cases with per-event evidence."""

    def __init__(self, store: TriageCaseStore, clock: Callable[[], datetime] = _utc_now) -> None:
        """Use ``store`` for persistence and ``clock`` (timezone-aware) for timestamps."""
        self._store = store
        self._clock = clock

    def now(self) -> datetime:
        """Current time in UTC, from the injected clock."""
        return self._clock().astimezone(UTC)

    async def record(self, event: TriageEventInput) -> IntakeOutcome:
        """Save ``event`` on its order's case in one transaction and return the outcome."""
        cause = derive_cause(event.topic, event.reason)
        now = self.now()
        async with self._store.transaction() as work:
            case = await work.get_or_create_locked_case(event.order_id, cause, now)
            if await work.add_evidence(case.case_id, event, now):
                # Every new evidence bumps ``updated_at``; the cause changes only when outranked.
                await work.update_case(case.case_id, merge_cause(case.cause, cause), now)
        return IntakeOutcome.PROCESSED
