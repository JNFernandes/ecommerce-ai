"""Propose → decide end to end on fakes (US-06 US1, US2): events, storage, the one cancel."""

import asyncio
from collections import deque
from collections.abc import Iterator
from uuid import UUID

import pytest
import respx
from pydantic import JsonValue

from ecommerce_ai.agents.assistant.approvals import APPROVAL_OUTCOME_MESSAGES
from ecommerce_ai.agents.assistant.gatekeeper import SAFE_MESSAGE
from ecommerce_ai.agents.assistant.graph import ChatGraph, load_assistant_prompt
from ecommerce_ai.agents.assistant.model import (
    ApprovalOutcome,
    Decision,
    FinishReason,
    MessageEnd,
    MessageStart,
)
from ecommerce_ai.agents.assistant.ports import ApprovalStatus
from ecommerce_ai.agents.assistant.service import ChatService
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
from tests.support.polling import wait_until
from tests.support.sse import SseEvent
from tests.support.tokens import jwks
from tests.support.triage import NOW

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


# Nothing to approve (US4) -----------------------------------------------------------------------


async def _refused(harness: ApprovalHarness, order_id: str = ORDER_ID) -> list[SseEvent]:
    """Post a message the model answers with ``cancel_order`` on ``order_id``, then text."""
    harness.script(cancel_turn(order_id), text_turn("I could not do that."))
    async with harness.client() as client:
        conversation_id = await harness.conversation(client)
        response = await harness.post(client, conversation_id)
    assert response.status_code == 200, response.text
    return sse(response)


def _assert_no_approval(harness: ApprovalHarness, events: list[SseEvent]) -> None:
    assert "approval.required" not in names(events)
    assert data(events, "message.end")["finishReason"] == "completed"
    assert harness.approvals.approvals == []
    assert harness.approvals.actions == {}
    assert harness.tools.approved_calls == []


@pytest.mark.parametrize(
    ("result", "summary", "error"),
    [
        (envelope(order(status="CONFIRMED")), "Order cannot be cancelled", "not_cancellable"),
        (FakeToolError("not_found", "no such order"), "Order not found", "not_found"),
    ],
    ids=["confirmed", "missing-or-foreign"],
)
async def test_should_explain_and_create_no_approval_when_the_order_cannot_be_cancelled(
    harness: ApprovalHarness, result: dict[str, JsonValue] | FakeToolError, summary: str, error: str
) -> None:
    harness.tools.results["get_order"] = result
    events = await _refused(harness)

    assert data(events, "tool.result") == {
        "callId": CANCEL_CALL_ID,
        "name": "cancel_order",
        "ok": False,
        "summary": summary,
    }
    refusal = harness.model.requests[-1].transcript[-1]
    assert refusal.kind == "tool_result"
    assert refusal.payload["error"] == error
    _assert_no_approval(harness, events)


async def test_should_refuse_a_malformed_order_id_without_reading_the_order(
    harness: ApprovalHarness,
) -> None:
    events = await _refused(harness, "order-1")

    assert data(events, "tool.result")["summary"] == "Request refused"
    assert harness.tools.calls == []
    _assert_no_approval(harness, events)


async def test_should_end_tool_failed_without_approval_when_the_proposal_read_gets_a_401(
    harness: ApprovalHarness,
) -> None:
    harness.tools.results["get_order"] = FakeToolError(
        "unauthenticated", "session expired", upstream_unauthenticated=True
    )
    harness.script(cancel_turn())
    async with harness.client() as client:
        conversation_id = await harness.conversation(client)
        response = await harness.post(client, conversation_id)

    events = sse(response)
    assert names(events)[-1] == "error"
    assert data(events, "error")["code"] == "TOOL_FAILED"
    assert harness.approvals.approvals == []
    assert harness.tools.approved_calls == []


async def test_should_refuse_the_sixth_proposal_in_an_hour_even_after_a_restart(
    harness: ApprovalHarness,
) -> None:
    for _ in range(5):
        async with harness.client() as client:
            await harness.propose(client)
    events = await _refused(harness)  # a new app over the same stores

    assert data(events, "tool.result")["summary"] == "Too many cancellation requests"
    assert len(harness.approvals.approvals) == 5
    assert harness.tools.approved_calls == []

    harness.clock.advance(3601)  # the oldest proposals leave the rolling hour
    async with harness.client() as client:
        _, _, allowed = await harness.propose(client)
    assert "approval.required" in names(allowed)


async def test_should_end_not_cancellable_when_the_order_changed_before_the_approval(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.tools.results["get_order"] = envelope(order(status="CONFIRMED"))
        harness.script(text_turn("It could not be cancelled any more."))
        response = await harness.decide(client, conversation_id, approval_id)

    assert data(sse(response), "approval.resolved")["outcome"] == "not_cancellable"
    assert harness.tools.approved_calls == []
    assert harness.action(approval_id).outcome is ApprovalOutcome.NOT_CANCELLABLE


async def test_should_end_not_cancellable_when_orders_answers_409(
    harness: ApprovalHarness,
) -> None:
    harness.tools.results["cancel_order"] = FakeToolError("conflict", "not pending")
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.script(text_turn("It could not be cancelled any more."))
        response = await harness.decide(client, conversation_id, approval_id)

    events = sse(response)
    assert data(events, "approval.resolved")["outcome"] == "not_cancellable"
    assert data(events, "tool.result")["ok"] is False
    assert len(harness.tools.approved_calls) == 1
    assert harness.action(approval_id).outcome is ApprovalOutcome.NOT_CANCELLABLE


async def test_should_end_failed_and_never_claim_success_when_orders_times_out(
    harness: ApprovalHarness,
) -> None:
    harness.tools.results["cancel_order"] = FakeToolError("timeout", "no answer")
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.script(text_turn("Your order was cancelled."))
        response = await harness.decide(client, conversation_id, approval_id)

    events = sse(response)
    assert data(events, "approval.resolved")["outcome"] == "failed"
    assert data(events, "token") == {"text": SAFE_MESSAGE}
    assert data(events, "message.end")["finishReason"] == "replaced"
    assert harness.action(approval_id).outcome is ApprovalOutcome.FAILED


async def test_should_cancel_once_and_record_it_when_the_client_leaves_during_the_approval(
    harness: ApprovalHarness,
) -> None:
    graph = ChatGraph(
        model=harness.model,
        tools=harness.tools,
        prompt=load_assistant_prompt(),
        checkpointer=harness.checkpointer,
        clock=harness.clock,
    )
    service = ChatService(harness.store, graph, harness.clock, approvals=harness.approvals)
    conversation = await harness.store.create(OWNER, None, None, NOW)
    harness.script(cancel_turn())
    proposing = await service.start(OWNER, conversation.id, "Cancel my order")
    assert proposing is not None
    _ = [event async for event in proposing.events()]
    (approval,) = harness.approvals.approvals

    harness.tools.gate = asyncio.Event()  # holds the re-read of the approve continuation
    deciding = await service.decide(OWNER, conversation.id, approval.id, Decision.APPROVE)
    assert deciding is not None
    stream = deciding.events()
    assert isinstance(await anext(stream), MessageStart)
    step = asyncio.create_task(anext(stream))
    await wait_until(lambda: len(harness.tools.calls) == 2)
    step.cancel()  # the client went away while the approval was being carried out
    harness.tools.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await step

    assert len(harness.tools.approved_calls) == 1
    assert harness.approvals.actions[approval.id].outcome is ApprovalOutcome.CANCELLED
    assert not service.streams.is_active(conversation.id)
    harness.script(text_turn("You're welcome."))
    following = await service.start(OWNER, conversation.id, "Thanks")
    assert following is not None
    events = [event async for event in following.events()]
    assert isinstance(events[-1], MessageEnd)
    assert events[-1].finish_reason is FinishReason.COMPLETED
    assert len(harness.tools.approved_calls) == 1
