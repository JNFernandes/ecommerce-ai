"""Chat assistant ports: model events, tool outcomes, approvals and the checkpointer alias."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from ecommerce_ai.agents.assistant.model import ApprovalSummary, Usage, UserTurn
from ecommerce_ai.agents.assistant.ports import (
    Approval,
    ApprovalStatus,
    ChatModelEvent,
    ChatModelRequest,
    Checkpointer,
    ModelTurnEnd,
    ModelUnavailable,
    TextDelta,
    ToolOutcome,
    ToolSpec,
    ToolUseRequest,
)
from ecommerce_ai.agents.ports import ModelError


def test_should_reuse_the_shared_model_unavailable_error() -> None:
    assert issubclass(ModelUnavailable, ModelError)


def test_should_build_a_request_from_the_prompt_transcript_and_tools() -> None:
    request = ChatModelRequest(
        system="You are the shop assistant.",
        transcript=(UserTurn(text="Hi"),),
        tools=(ToolSpec(name="get_order", description="One order.", input_schema={}),),
    )

    assert request.tools[0].name == "get_order"
    with pytest.raises(ValidationError):
        ChatModelRequest(system="", transcript=(), tools=())


def test_should_tag_each_model_event_with_its_kind() -> None:
    events: list[ChatModelEvent] = [
        TextDelta(text="Hel"),
        ToolUseRequest(id="toolu_01", name="get_order", input={"orderId": "x"}),
        ModelTurnEnd(stop_reason="end_turn", usage=Usage(input_tokens=3), model_id="m"),
    ]

    assert [event.kind for event in events] == ["text", "tool_use", "turn_end"]


def test_should_keep_a_tool_outcome_immutable() -> None:
    outcome = ToolOutcome(
        ok=True,
        payload={"data": []},
        outcome="ok",
        refused=False,
        upstream_unauthenticated=False,
        duration_ms=1.5,
    )

    with pytest.raises(ValidationError):
        outcome.ok = False  # type: ignore[misc]  # frozen model, the assignment must fail


PROPOSED_AT = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
ORDER_ID = UUID("3f2a9c1e-7b4d-4e5f-8a6b-1c2d3e4f5a6b")


def _approval(**changes: object) -> Approval:
    fields: dict[str, object] = {
        "id": uuid4(),
        "conversation_id": uuid4(),
        "user_id": "user-1",
        "action": "cancel_order",
        "order_id": ORDER_ID,
        "arguments": {"orderId": str(ORDER_ID)},
        "summary": ApprovalSummary(status="PENDING", total_amount=59.9),
        "proposed_at": PROPOSED_AT,
        "expires_at": PROPOSED_AT + timedelta(seconds=600),
    }
    return Approval.model_validate(fields | changes)


def test_should_start_an_approval_pending_and_undecided() -> None:
    approval = _approval()

    assert approval.status is ApprovalStatus.PENDING
    assert approval.decided_at is None


def test_should_treat_an_approval_as_expired_from_its_expiry_instant() -> None:
    approval = _approval()

    assert not approval.is_expired(PROPOSED_AT + timedelta(seconds=599))
    assert approval.is_expired(PROPOSED_AT + timedelta(seconds=600))


def test_should_reject_an_approval_when_it_expires_before_it_is_proposed() -> None:
    with pytest.raises(ValidationError, match="expires_at"):
        _approval(expires_at=PROPOSED_AT)


def test_should_build_the_required_event_from_the_stored_approval() -> None:
    approval = _approval()

    event = approval.required_event()

    assert event.approval_id == approval.id
    assert event.order_id == ORDER_ID
    assert event.summary == approval.summary
    assert event.expires_at == approval.expires_at


def test_should_keep_an_approval_immutable() -> None:
    approval = _approval()

    with pytest.raises(ValidationError):
        approval.status = ApprovalStatus.APPROVED  # type: ignore[misc]  # frozen model


def test_should_accept_the_in_memory_saver_as_a_checkpointer() -> None:
    saver: Checkpointer = InMemorySaver()

    assert saver is not None
