"""Triage test helpers: a controllable clock and one representative case per cause."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import JsonValue

from ecommerce_ai.agents.triage.model import CaseForTriage, EvidenceItem
from ecommerce_ai.agents.triage_intake.model import TriageCause

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

_EVIDENCE: dict[TriageCause, tuple[str, str | None]] = {
    TriageCause.PAYMENT_FAILED: ("payments.payment-failed", "card declined"),
    TriageCause.INSUFFICIENT_STOCK: ("inventory.stock-rejected", "insufficient stock"),
    TriageCause.UNKNOWN_PRODUCT: ("inventory.stock-rejected", "unknown product"),
    TriageCause.CANCELLATION_REQUESTED: ("orders.order-cancelled", None),
    TriageCause.UNKNOWN: ("orders.order-cancelled", "something odd"),
}


class FakeClock:
    """``Clock`` that moves only when told: ``advance`` moves both clocks.

    ``tick`` is added to the monotonic clock after every ``monotonic()`` call, so a run that
    reads it twice lasts exactly ``tick`` seconds.
    """

    def __init__(self, now: datetime = NOW, tick: float = 0.0) -> None:
        """Start at ``now`` and monotonic ``0.0``."""
        self._now = now
        self._monotonic = 0.0
        self.tick = tick

    def now(self) -> datetime:
        """The current fake UTC time."""
        return self._now

    def monotonic(self) -> float:
        """The current fake monotonic time, then move it on by ``tick``."""
        value = self._monotonic
        self._monotonic += self.tick
        return value

    def advance(self, seconds: float) -> None:
        """Move both clocks forward."""
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


def make_case(
    cause: TriageCause = TriageCause.PAYMENT_FAILED,
    *,
    order_id: UUID | None = None,
    with_evidence: bool = True,
) -> CaseForTriage:
    """A case of ``cause`` with one stored event typical of it (or none)."""
    order = order_id or uuid4()
    topic, reason = _EVIDENCE[cause]
    payload: dict[str, JsonValue] = {"orderId": str(order)}
    if reason is not None:
        payload["reason"] = reason
    evidence = (
        (EvidenceItem(topic=topic, reason=reason, occurred_at=NOW, payload=payload),)
        if with_evidence
        else ()
    )
    return CaseForTriage(case_id=uuid4(), order_id=order, cause=cause, evidence=evidence)
