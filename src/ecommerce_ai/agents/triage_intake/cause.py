"""Cause decisions of the triage intake (FR-006): pure functions, no I/O.

The topic names and reason texts are those of ``ecommerce-infra/docs/events.md``. They are
repeated here rather than imported from ``messaging`` because agents never depend on messaging
(constitution I); a unit test keeps both lists equal.
"""

from typing import Final

from ecommerce_ai.agents.triage_intake.model import TriageCause

ORDER_CANCELLED: Final = "orders.order-cancelled"
PAYMENT_FAILED: Final = "payments.payment-failed"
STOCK_REJECTED: Final = "inventory.stock-rejected"
TRIAGE_TOPICS: Final = (ORDER_CANCELLED, PAYMENT_FAILED, STOCK_REJECTED)
"""Topics whose events the intake can turn into a cause."""

_STOCK_CAUSES: Final = {
    "insufficient stock": TriageCause.INSUFFICIENT_STOCK,
    "unknown product": TriageCause.UNKNOWN_PRODUCT,
    "unknown product and insufficient stock": TriageCause.UNKNOWN_PRODUCT,
}
_PAYMENT_FAILED_PREFIX: Final = "payment failed"
_STOCK_REJECTED_PREFIX: Final = "stock rejected: "


def _stock_cause(reason: str) -> TriageCause:
    return _STOCK_CAUSES.get(reason, TriageCause.UNKNOWN)


def _cancellation_cause(reason: str | None) -> TriageCause:
    if reason is None:
        return TriageCause.CANCELLATION_REQUESTED
    if reason.startswith(_PAYMENT_FAILED_PREFIX):
        return TriageCause.PAYMENT_FAILED
    if reason.startswith(_STOCK_REJECTED_PREFIX):
        return _stock_cause(reason.removeprefix(_STOCK_REJECTED_PREFIX))
    return TriageCause.UNKNOWN


_RANK: Final = {
    TriageCause.PAYMENT_FAILED: 2,
    TriageCause.INSUFFICIENT_STOCK: 2,
    TriageCause.UNKNOWN_PRODUCT: 2,
    TriageCause.CANCELLATION_REQUESTED: 1,
    TriageCause.UNKNOWN: 0,
}


def merge_cause(current: TriageCause, incoming: TriageCause) -> TriageCause:
    """Return the case's cause after ``incoming``: a higher rank replaces, a tie keeps ``current``.

    Failure causes outrank ``CANCELLATION_REQUESTED``, which outranks ``UNKNOWN``; between two
    failure causes the first received is kept (FR-006).
    """
    return incoming if _RANK[incoming] > _RANK[current] else current


def derive_cause(topic: str, reason: str | None) -> TriageCause:
    """Return the cause one event implies on its own; ``ValueError`` for an unknown topic."""
    if topic == PAYMENT_FAILED:
        return TriageCause.PAYMENT_FAILED
    if topic == STOCK_REJECTED:
        return _stock_cause(reason or "")
    if topic == ORDER_CANCELLED:
        return _cancellation_cause(reason)
    raise ValueError(f"unsupported topic: {topic}")
