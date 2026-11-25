"""The domain rules of a proposed cancellation (US-06, research §2, §5).

Pure functions over tool outcomes; the graph calls them, nothing here does I/O.

- Proposal checks (FR-003, FR-005): the target id must be a UUID, no other approval may be pending
  and a fresh ``get_order`` read must find the caller's order ``PENDING``; otherwise the model gets
  a fixed ``Refusal`` as the call's result.
- ``summary_of``: the approval card's summary, from that read, never from the model.
- Outcome mapping (FR-008): after approve, a re-read not ``PENDING`` / not found or Orders 409 is
  ``not_cancellable``; a timeout, 5xx or 401 is ``failed``; a confirmed cancel is ``cancelled``.
- ``APPROVAL_OUTCOME_MESSAGES``: the fixed answer of each outcome, sent when the model cannot
  answer after a decision, so a cancellation is never hidden (ADR 0003).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID

from pydantic import JsonValue

from ecommerce_ai.agents.assistant.model import ApprovalOutcome, ApprovalSummary, Decision
from ecommerce_ai.agents.assistant.ports import ToolOutcome

CANCEL_ACTION: Final = "cancel_order"
"""The one mutating tool; a call to it is intercepted and becomes a proposal."""
APPROVAL_ACTIONS: Final = frozenset({CANCEL_ACTION})
"""Tools whose calls only ever propose (constitution III)."""
TARGET_ARGUMENT: Final = "orderId"
READ_TOOL: Final = "get_order"
"""The read-only tool that re-reads the target with the caller's token."""
PENDING_STATUS: Final = "PENDING"
_NOT_FOUND_CODE: Final = "not_found"
_CONFLICT_CODE: Final = "conflict"


class ApprovalPending(Exception):
    """A chat message while a non-expired approval is pending (409 ``APPROVAL_PENDING``)."""


class ApprovalAlreadyDecided(Exception):
    """A decision on an approval no longer pending (409 ``APPROVAL_ALREADY_DECIDED``)."""


class ApprovalExpired(Exception):
    """A decision after ``expiresAt``; the approval is recorded ``expired`` (410)."""


class Refusal(StrEnum):
    """Why a ``cancel_order`` call produced no approval; its value is the result's error code."""

    INVALID_ID = "invalid_arguments"
    NOT_FOUND = "not_found"
    NOT_PENDING = "not_cancellable"
    ALREADY_PENDING = "approval_pending"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "upstream_error"


_REFUSAL_MESSAGES: Final[Mapping[Refusal, str]] = MappingProxyType(
    {
        Refusal.INVALID_ID: "No approval was created: orderId is not a valid order id.",
        Refusal.NOT_FOUND: "No approval was created: the order was not found.",
        Refusal.NOT_PENDING: (
            "No approval was created: the order cannot be cancelled because its status is"
            " {status}. Only PENDING orders can be cancelled."
        ),
        Refusal.ALREADY_PENDING: (
            "No approval was created: an approval is already pending in this conversation."
        ),
        Refusal.RATE_LIMITED: (
            "No approval was created: the limit of cancellation proposals per hour was reached."
        ),
        Refusal.UNAVAILABLE: ("No approval was created: the order could not be checked right now."),
    }
)

_REFUSAL_SUMMARIES: Final[Mapping[Refusal, str]] = MappingProxyType(
    {
        Refusal.INVALID_ID: "Request refused",
        Refusal.NOT_FOUND: "Order not found",
        Refusal.NOT_PENDING: "Order cannot be cancelled",
        Refusal.ALREADY_PENDING: "Approval already pending",
        Refusal.RATE_LIMITED: "Too many cancellation requests",
        Refusal.UNAVAILABLE: "Orders unavailable",
    }
)

APPROVAL_OUTCOME_MESSAGES: Final[Mapping[ApprovalOutcome, str]] = MappingProxyType(
    {
        ApprovalOutcome.CANCELLED: "Your order has been cancelled.",
        ApprovalOutcome.REJECTED: "Okay, the order was not cancelled. Nothing was changed.",
        ApprovalOutcome.NOT_CANCELLABLE: (
            "The order could not be cancelled because it is no longer pending. Nothing was changed."
        ),
        ApprovalOutcome.FAILED: (
            "The cancellation could not be confirmed. Please check the order's status in your"
            " orders."
        ),
        ApprovalOutcome.EXPIRED: "The cancellation request expired. Nothing was changed.",
        ApprovalOutcome.WITHDRAWN: "The cancellation request was withdrawn. Nothing was changed.",
    }
)
"""The fixed answer of each outcome (model down after a decision, closed approvals)."""

_OUTCOME_RESULTS: Final[Mapping[ApprovalOutcome, str]] = MappingProxyType(
    {
        ApprovalOutcome.REJECTED: (
            "The customer rejected the cancellation. The order was not cancelled; nothing changed."
        ),
        ApprovalOutcome.NOT_CANCELLABLE: (
            "The order was not cancelled: it is no longer PENDING. Nothing changed."
        ),
        ApprovalOutcome.FAILED: (
            "The cancellation could not be confirmed: Orders did not answer. Do not say the order"
            " was cancelled; suggest checking the order's status."
        ),
        ApprovalOutcome.EXPIRED: (
            "The cancellation request expired without a decision. The order was not cancelled;"
            " nothing changed."
        ),
        ApprovalOutcome.WITHDRAWN: (
            "The cancellation request was withdrawn. The order was not cancelled; nothing changed."
        ),
    }
)

_OUTCOME_SUMMARIES: Final[Mapping[ApprovalOutcome, str]] = MappingProxyType(
    {
        ApprovalOutcome.CANCELLED: "Order cancelled",
        ApprovalOutcome.REJECTED: "Cancellation rejected, nothing changed",
        ApprovalOutcome.NOT_CANCELLABLE: "Order cannot be cancelled",
        ApprovalOutcome.FAILED: "Cancellation could not be confirmed",
        ApprovalOutcome.EXPIRED: "Cancellation expired, nothing changed",
        ApprovalOutcome.WITHDRAWN: "Cancellation withdrawn, nothing changed",
    }
)


@dataclass(frozen=True, slots=True)
class OrderRead:
    """What a ``get_order`` read says about the target."""

    status: str | None
    """The order's status; ``None`` when the read failed or found nothing."""
    total_amount: float | None = None
    refusal: Refusal | None = None
    """Why the read rules out a proposal (``None``: the order is the caller's and ``PENDING``)."""
    unauthenticated: bool = False
    """The platform rejected the caller's token (the run ends ``TOOL_FAILED``)."""


def parse_target(arguments: Mapping[str, JsonValue]) -> UUID | None:
    """The ``orderId`` argument as a UUID, or ``None`` when it is missing or malformed."""
    raw = arguments.get(TARGET_ARGUMENT)
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def read_order(outcome: ToolOutcome) -> OrderRead:
    """Interpret a ``get_order`` outcome: only a found ``PENDING`` order may be proposed."""
    if not outcome.ok:
        refusal = Refusal.NOT_FOUND if outcome.outcome == _NOT_FOUND_CODE else Refusal.UNAVAILABLE
        return OrderRead(
            status=None, refusal=refusal, unauthenticated=outcome.upstream_unauthenticated
        )
    data = outcome.payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("status"), str):
        return OrderRead(status=None, refusal=Refusal.NOT_FOUND)
    status = str(data["status"])
    total = data.get("totalAmount")
    amount = (
        float(total) if isinstance(total, int | float) and not isinstance(total, bool) else None
    )
    blocked = None if status == PENDING_STATUS else Refusal.NOT_PENDING
    return OrderRead(status=status, total_amount=amount, refusal=blocked)


def summary_of(read: OrderRead) -> ApprovalSummary:
    """The approval card's summary, from the fresh read (never from the model's text)."""
    if read.status is None:
        raise ValueError("a summary needs a successful read")
    return ApprovalSummary(status=read.status, total_amount=read.total_amount)


_REFUSAL_REASONS: Final[Mapping[Refusal, str]] = MappingProxyType(
    {
        Refusal.INVALID_ID: "invalid_order_id",
        Refusal.NOT_FOUND: "order_not_found",
        Refusal.NOT_PENDING: "order_not_pending",
        Refusal.ALREADY_PENDING: "approval_pending",
        Refusal.RATE_LIMITED: "rate_limited",
        Refusal.UNAVAILABLE: "orders_unavailable",
    }
)
"""The safe, specific reason of each refusal, for the model to explain (never upstream text)."""


def refusal_payload(
    refusal: Refusal, status: str | None = None, order_id: UUID | None = None
) -> dict[str, JsonValue]:
    """The ``{error, reason, message}`` result the model gets for a refused ``cancel_order``.

    A refusal because the order is not ``PENDING`` also carries ``data`` ``{orderId, status}``
    from the fresh read, so the answer may state that status (the gatekeeper treats it as
    evidence; every ``cancel_order`` result is built here, never from upstream text).
    """
    message = _REFUSAL_MESSAGES[refusal].format(status=status or "unknown")
    payload: dict[str, JsonValue] = {
        "error": refusal.value,
        "reason": _REFUSAL_REASONS[refusal],
        "message": message,
    }
    if refusal is Refusal.NOT_PENDING and order_id is not None and status is not None:
        payload["data"] = {TARGET_ARGUMENT: str(order_id), "status": status}
    return payload


def refusal_summary(refusal: Refusal) -> str:
    """The fixed ``tool.result`` summary of a refused call."""
    return _REFUSAL_SUMMARIES[refusal]


def outcome_after_reread(read: OrderRead) -> ApprovalOutcome | None:
    """``None`` when the approved cancel may run; else the outcome ending the approval."""
    if read.refusal is None:
        return None
    if read.unauthenticated or read.refusal is Refusal.UNAVAILABLE:
        return ApprovalOutcome.FAILED
    return ApprovalOutcome.NOT_CANCELLABLE


def outcome_of_cancel(outcome: ToolOutcome) -> ApprovalOutcome:
    """The outcome of the approved cancel call (FR-008); 409 / 404 mean nothing changed."""
    if outcome.ok:
        return ApprovalOutcome.CANCELLED
    if outcome.outcome in {_CONFLICT_CODE, _NOT_FOUND_CODE} and not outcome.refused:
        return ApprovalOutcome.NOT_CANCELLABLE
    return ApprovalOutcome.FAILED


def outcome_payload(
    outcome: ApprovalOutcome, decision: Decision | None = None
) -> dict[str, JsonValue]:
    """The fixed result the model gets for an approval that did not cancel.

    ``approval`` states the customer's decision (``None``: closed without one) and its outcome
    explicitly, so the answer is about what happened, never a new request for approval.
    """
    if outcome is ApprovalOutcome.CANCELLED:
        raise ValueError("a cancelled order's result is the Orders envelope")
    return {
        "error": outcome.value,
        "message": _OUTCOME_RESULTS[outcome],
        "approval": approval_status(decision, outcome),
    }


def approval_status(decision: Decision | None, outcome: ApprovalOutcome) -> dict[str, JsonValue]:
    """The resolved approval as the model sees it: ``{resolved, decision, outcome}``."""
    return {
        "resolved": True,
        "decision": None if decision is None else decision.value,
        "outcome": outcome.value,
    }


def outcome_message(outcome: ApprovalOutcome, order_id: UUID) -> str:
    """The deterministic answer of a resolved approval (model down, or its answer replaced)."""
    if outcome is ApprovalOutcome.CANCELLED:
        return f"Your order {order_id} has been cancelled."
    return APPROVAL_OUTCOME_MESSAGES[outcome]


def outcome_summary(outcome: ApprovalOutcome) -> str:
    """The fixed ``tool.result`` summary of an approval's outcome."""
    return _OUTCOME_SUMMARIES[outcome]
