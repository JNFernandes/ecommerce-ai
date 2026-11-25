"""Propose → decide end to end on fakes (US-06 US1, US2): events, storage, the one cancel."""

from collections import deque
from collections.abc import Iterator
from uuid import UUID

import pytest
import respx

from ecommerce_ai.agents.assistant.approvals import APPROVAL_OUTCOME_MESSAGES
from ecommerce_ai.agents.assistant.model import ApprovalOutcome, Decision, FinishReason
from ecommerce_ai.agents.assistant.ports import ApprovalStatus
from ecommerce_ai.agents.ports import ModelUnavailable
from ecommerce_ai.infrastructure.settings import Settings
from tests.support.approvals import (
    CANCEL_CALL_ID,
    ORDER_ID,
    OTHER_ORDER_ID,
    OWNER,
    TOKEN,
    ApprovalHarness,
    cancel_turn,
    data,
    names,
    order,
    sse,
)
from tests.support.chat import FakeToolError, envelope
from tests.support.llm import MODEL_ID, text_turn, tool_turn
from tests.support.tokens import jwks

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default


@pytest.fixture(autouse=True)
def identity() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        yield route


@pytest.fixture
def harness() -> ApprovalHarness:
    return ApprovalHarness()


# Propose ----------------------------------------------------------------------------------------


async def test_should_pause_with_an_approval_card_and_cancel_nothing_when_cancel_is_proposed(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, events = await harness.propose(client)

    assert names(events) == [
        "message.start",
        "token",
        "tool.call",
        "approval.required",
        "message.end",
    ]
    required = data(events, "approval.required")
    assert required == {
        "approvalId": approval_id,
        "action": "cancel_order",
        "orderId": ORDER_ID,
        "summary": {"status": "PENDING", "totalAmount": 59.9, "currency": None},
        "expiresAt": "2026-11-24T12:10:00Z",
    }
    assert data(events, "message.end")["finishReason"] == "awaiting_approval"
    assert harness.tools.approved_calls == []
    assert [call.name for call in harness.tools.calls] == ["get_order"]
    (approval,) = harness.approvals.approvals
    assert approval.id == UUID(approval_id)
    assert approval.user_id == OWNER
    assert approval.arguments == {"orderId": ORDER_ID}
    assert approval.status is ApprovalStatus.PENDING
    assert harness.action(approval_id).decision is None
    messages = harness.store.messages[UUID(conversation_id)]
    assert messages[-1].finish_reason is FinishReason.AWAITING_APPROVAL
    assert messages[-1].content == "I can cancel it for you."


async def test_should_build_the_summary_from_the_fresh_read_not_the_model_when_proposing(
    harness: ApprovalHarness,
) -> None:
    harness.tools.results["get_order"] = envelope(order(total=12.5))
    async with harness.client() as client:
        _, _, events = await harness.propose(client, text="Let me prepare that.")

    assert data(events, "approval.required")["summary"] == {
        "status": "PENDING",
        "totalAmount": 12.5,
        "currency": None,
    }


# Approve ----------------------------------------------------------------------------------------


async def test_should_cancel_once_with_the_callers_token_and_stored_arguments_when_approved(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.script(text_turn("Done: your order was cancelled."))
        response = await harness.decide(client, conversation_id, approval_id)

    assert response.status_code == 200, response.text
    events = sse(response)
    assert names(events) == [
        "message.start",
        "approval.resolved",
        "tool.result",
        "token",
        "message.end",
    ]
    assert [event.id for event in events] == ["1", "2", "3", "4", "5"]
    assert data(events, "approval.resolved") == {
        "approvalId": approval_id,
        "decision": "approve",
        "outcome": "cancelled",
    }
    result = data(events, "tool.result")
    assert result == {
        "callId": CANCEL_CALL_ID,
        "name": "cancel_order",
        "ok": True,
        "summary": "Order cancelled",
    }
    end = data(events, "message.end")
    assert end["finishReason"] == "completed"
    assert end["modelId"] == MODEL_ID
    assert [(c.name, c.arguments) for c in harness.tools.approved_calls] == [
        ("cancel_order", {"orderId": ORDER_ID})
    ]
    assert harness.tools.approved_tokens == [TOKEN]
    assert [call.name for call in harness.tools.calls] == ["get_order", "get_order"]
    assert harness.approvals.approvals[0].status is ApprovalStatus.APPROVED
    action = harness.action(approval_id)
    assert (action.decision, action.outcome) == (Decision.APPROVE, ApprovalOutcome.CANCELLED)
    messages = harness.store.messages[UUID(conversation_id)]
    assert messages[-1].content == "Done: your order was cancelled."
    assert messages[-1].finish_reason is FinishReason.COMPLETED


async def test_should_give_the_model_the_paused_call_result_when_continuing(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.script(text_turn("Cancelled."))
        await harness.decide(client, conversation_id, approval_id)

    request = harness.model.requests[-1]
    last = request.transcript[-1]
    assert last.kind == "tool_result"
    assert last.call_id == CANCEL_CALL_ID
    assert last.ok is True


async def test_should_cancel_only_the_stored_order_when_the_model_asks_for_another_afterwards(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.script(
            tool_turn(("toolu_2", "cancel_order", {"orderId": OTHER_ORDER_ID})),
            text_turn("Order A is cancelled."),
        )
        response = await harness.decide(client, conversation_id, approval_id)

    events = sse(response)
    assert data(events, "approval.resolved")["outcome"] == "cancelled"
    assert [c.arguments for c in harness.tools.approved_calls] == [{"orderId": ORDER_ID}]


async def test_should_answer_the_fixed_confirmation_when_the_model_is_down_after_the_cancel(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        unavailable = [ModelUnavailable("down")]
        harness.script(unavailable, unavailable, unavailable)
        response = await harness.decide(client, conversation_id, approval_id)

    events = sse(response)
    assert names(events) == [
        "message.start",
        "approval.resolved",
        "tool.result",
        "token",
        "message.end",
    ]
    message = APPROVAL_OUTCOME_MESSAGES[ApprovalOutcome.CANCELLED]
    assert data(events, "token") == {"text": message}
    assert data(events, "message.end")["finishReason"] == "completed"
    assert harness.store.messages[UUID(conversation_id)][-1].content == message
    assert len(harness.tools.approved_calls) == 1


async def test_should_end_tool_failed_when_the_cancel_gets_an_upstream_401(
    harness: ApprovalHarness,
) -> None:
    harness.tools.results["cancel_order"] = FakeToolError(
        "unauthenticated", "session expired", upstream_unauthenticated=True
    )
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        response = await harness.decide(client, conversation_id, approval_id)

    events = sse(response)
    assert names(events) == ["message.start", "approval.resolved", "tool.result", "error"]
    assert data(events, "approval.resolved")["outcome"] == "failed"
    assert data(events, "error")["code"] == "TOOL_FAILED"
    assert harness.action(approval_id).outcome is ApprovalOutcome.FAILED


async def test_should_continue_normally_with_the_next_message_after_a_decision(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.script(text_turn("Cancelled."), text_turn("You're welcome."))
        await harness.decide(client, conversation_id, approval_id)
        response = await harness.post(client, conversation_id, "thanks")

    assert response.status_code == 200
    assert data(sse(response), "message.end")["finishReason"] == "completed"


# Reject (US2) -----------------------------------------------------------------------------------


async def test_should_change_nothing_and_say_so_when_rejected(harness: ApprovalHarness) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.script(text_turn("Okay, nothing was changed."))
        response = await harness.decide(client, conversation_id, approval_id, "reject")

    events = sse(response)
    assert names(events) == [
        "message.start",
        "approval.resolved",
        "tool.result",
        "token",
        "message.end",
    ]
    assert data(events, "approval.resolved") == {
        "approvalId": approval_id,
        "decision": "reject",
        "outcome": "rejected",
    }
    assert data(events, "tool.result")["ok"] is False
    assert data(events, "tool.result")["summary"] == "Cancellation rejected, nothing changed"
    last = harness.model.requests[-1].transcript[-1]
    assert last.kind == "tool_result"
    assert "nothing changed" in str(last.payload["message"])
    assert harness.tools.approved_calls == []
    assert [call.name for call in harness.tools.calls] == ["get_order"]
    action = harness.action(approval_id)
    assert (action.decision, action.outcome) == (Decision.REJECT, ApprovalOutcome.REJECTED)
    assert harness.approvals.approvals[0].status is ApprovalStatus.REJECTED


async def test_should_answer_the_fixed_nothing_changed_text_when_the_model_is_down_on_reject(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        unavailable = [ModelUnavailable("down")]
        harness.script(unavailable, unavailable, unavailable)
        response = await harness.decide(client, conversation_id, approval_id, "reject")

    message = APPROVAL_OUTCOME_MESSAGES[ApprovalOutcome.REJECTED]
    assert data(sse(response), "token") == {"text": message}
    assert harness.tools.approved_calls == []


# Lazy closing (US2, research §6) ----------------------------------------------------------------


async def test_should_withdraw_an_orphaned_pause_when_its_approval_was_never_stored(
    harness: ApprovalHarness,
) -> None:
    harness.approvals.failures["create_proposal"] = deque([RuntimeError("db down")])
    harness.script(cancel_turn(), text_turn("How can I help?"))
    async with harness.client() as client:
        conversation_id = await harness.conversation(client)
        failed = await harness.post(client, conversation_id)
        response = await harness.post(client, conversation_id, "Hello again")

    assert names(sse(failed))[-1] == "error"
    assert data(sse(failed), "error")["code"] == "INTERNAL"
    assert harness.approvals.approvals == []
    assert data(sse(response), "message.end")["finishReason"] == "completed"
    results = [i for i in harness.model.requests[-1].transcript if i.kind == "tool_result"]
    assert results[-1].payload["error"] == "withdrawn"
    assert harness.tools.approved_calls == []


async def test_should_close_as_failed_a_decided_approval_whose_outcome_was_never_recorded(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        # A crash right after the decision was recorded: the graph never resumed.
        await harness.approvals.decide(
            OWNER, UUID(conversation_id), UUID(approval_id), Decision.APPROVE, harness.clock.now()
        )
        harness.script(text_turn("Please check your order."))
        response = await harness.post(client, conversation_id, "Did it work?")

    assert response.status_code == 200
    assert harness.action(approval_id).outcome is ApprovalOutcome.FAILED
    results = [i for i in harness.model.requests[-1].transcript if i.kind == "tool_result"]
    assert results[-1].payload["error"] == "failed"
    assert harness.tools.approved_calls == []
