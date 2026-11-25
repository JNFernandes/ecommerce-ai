"""The pure rules of a proposed cancellation (``agents/assistant/approvals.py``, US-06)."""

from uuid import UUID

import pytest
from pydantic import JsonValue

from ecommerce_ai.agents.assistant.approvals import (
    APPROVAL_OUTCOME_MESSAGES,
    OrderRead,
    Refusal,
    outcome_after_reread,
    outcome_of_cancel,
    outcome_payload,
    outcome_summary,
    parse_target,
    read_order,
    refusal_payload,
    refusal_summary,
    summary_of,
)
from ecommerce_ai.agents.assistant.model import ApprovalOutcome, ApprovalSummary
from ecommerce_ai.agents.assistant.ports import ToolOutcome
from tests.support.approvals import ORDER_ID, order
from tests.support.chat import envelope


def _ok(payload: dict[str, JsonValue]) -> ToolOutcome:
    return ToolOutcome(
        ok=True,
        payload=payload,
        outcome="ok",
        refused=False,
        upstream_unauthenticated=False,
        duration_ms=1.0,
    )


def _failed(code: str, *, unauthenticated: bool = False, refused: bool = False) -> ToolOutcome:
    return ToolOutcome(
        ok=False,
        payload={"error": code, "message": "x"},
        outcome=code,
        refused=refused,
        upstream_unauthenticated=unauthenticated,
        duration_ms=1.0,
    )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"orderId": ORDER_ID}, UUID(ORDER_ID)),
        ({"orderId": "not-a-uuid"}, None),
        ({"orderId": 5}, None),
        ({}, None),
    ],
)
def test_should_parse_only_a_uuid_order_id_when_reading_the_target(
    arguments: dict[str, JsonValue], expected: UUID | None
) -> None:
    assert parse_target(arguments) == expected


def test_should_allow_a_proposal_only_when_the_read_finds_a_pending_order() -> None:
    read = read_order(_ok(envelope(order())))

    assert read == OrderRead(status="PENDING", total_amount=59.9)
    assert summary_of(read) == ApprovalSummary(status="PENDING", total_amount=59.9)


@pytest.mark.parametrize("status", ["CONFIRMED", "CANCELLED", "SHIPPED"])
def test_should_refuse_with_the_status_when_the_order_is_not_pending(status: str) -> None:
    read = read_order(_ok(envelope(order(status=status))))

    assert read.refusal is Refusal.NOT_PENDING
    payload = refusal_payload(Refusal.NOT_PENDING, read.status)
    assert payload["error"] == "not_cancellable"
    assert status in str(payload["message"])


@pytest.mark.parametrize(
    ("outcome", "refusal", "unauthenticated"),
    [
        (_failed("not_found"), Refusal.NOT_FOUND, False),
        (_failed("upstream_error"), Refusal.UNAVAILABLE, False),
        (_failed("unauthenticated", unauthenticated=True), Refusal.UNAVAILABLE, True),
        (_ok(envelope(None)), Refusal.NOT_FOUND, False),
        (_ok(envelope({"status": 3})), Refusal.NOT_FOUND, False),
    ],
)
def test_should_refuse_when_the_read_fails_or_finds_nothing(
    outcome: ToolOutcome, refusal: Refusal, unauthenticated: bool
) -> None:
    read = read_order(outcome)

    assert (read.status, read.refusal, read.unauthenticated) == (None, refusal, unauthenticated)


@pytest.mark.parametrize("total", [None, "59.90", True])
def test_should_report_no_total_when_the_read_has_no_numeric_total(total: JsonValue) -> None:
    read = read_order(_ok(envelope({"status": "PENDING", "totalAmount": total})))

    assert read.total_amount is None


def test_should_refuse_to_summarize_a_failed_read() -> None:
    with pytest.raises(ValueError, match="successful read"):
        summary_of(OrderRead(status=None, refusal=Refusal.NOT_FOUND))


@pytest.mark.parametrize("refusal", list(Refusal))
def test_should_have_a_fixed_message_and_summary_for_every_refusal(refusal: Refusal) -> None:
    payload = refusal_payload(refusal)

    assert payload["error"] == refusal.value
    assert str(payload["message"]).startswith("No approval was created")
    assert refusal_summary(refusal)


@pytest.mark.parametrize(
    ("read", "expected"),
    [
        (OrderRead(status="PENDING"), None),
        (
            OrderRead(status="CONFIRMED", refusal=Refusal.NOT_PENDING),
            ApprovalOutcome.NOT_CANCELLABLE,
        ),
        (OrderRead(status=None, refusal=Refusal.NOT_FOUND), ApprovalOutcome.NOT_CANCELLABLE),
        (OrderRead(status=None, refusal=Refusal.UNAVAILABLE), ApprovalOutcome.FAILED),
        (
            OrderRead(status=None, refusal=Refusal.UNAVAILABLE, unauthenticated=True),
            ApprovalOutcome.FAILED,
        ),
    ],
)
def test_should_map_the_reread_to_go_ahead_or_an_outcome_when_approved(
    read: OrderRead, expected: ApprovalOutcome | None
) -> None:
    assert outcome_after_reread(read) is expected


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (_ok(envelope({"orderId": ORDER_ID, "status": "CANCELLED"})), ApprovalOutcome.CANCELLED),
        (_failed("conflict"), ApprovalOutcome.NOT_CANCELLABLE),
        (_failed("not_found"), ApprovalOutcome.NOT_CANCELLABLE),
        (_failed("timeout"), ApprovalOutcome.FAILED),
        (_failed("upstream_error"), ApprovalOutcome.FAILED),
        (_failed("unauthenticated", unauthenticated=True), ApprovalOutcome.FAILED),
        (_failed("unknown_tool", refused=True), ApprovalOutcome.FAILED),
    ],
)
def test_should_map_the_cancel_call_to_its_outcome(
    outcome: ToolOutcome, expected: ApprovalOutcome
) -> None:
    assert outcome_of_cancel(outcome) is expected


@pytest.mark.parametrize(
    "outcome", [o for o in ApprovalOutcome if o is not ApprovalOutcome.CANCELLED]
)
def test_should_tell_the_model_nothing_was_cancelled_for_every_other_outcome(
    outcome: ApprovalOutcome,
) -> None:
    payload = outcome_payload(outcome)

    assert payload["error"] == outcome.value
    message = str(payload["message"])
    assert "not cancelled" in message or "could not be confirmed" in message
    assert outcome_summary(outcome)


def test_should_refuse_a_fixed_payload_for_a_cancelled_order() -> None:
    with pytest.raises(ValueError, match="envelope"):
        outcome_payload(ApprovalOutcome.CANCELLED)


def test_should_have_a_fixed_answer_for_every_outcome() -> None:
    assert set(APPROVAL_OUTCOME_MESSAGES) == set(ApprovalOutcome)
    assert outcome_summary(ApprovalOutcome.CANCELLED) == "Order cancelled"
