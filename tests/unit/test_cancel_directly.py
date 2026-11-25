"""A cancel request goes straight to ``cancel_order``; the app asks for approval (prompt v3).

With a fake LLM that follows the v3 rule, one customer message is enough to reach the approval
card: for a named order, and for the single clear candidate found by a lookup. The model is told
the rule in its system prompt, and nothing is cancelled before a decision.
"""

from collections.abc import Iterator

import pytest
import respx
from pydantic import JsonValue

from ecommerce_ai.infrastructure.settings import Settings
from tests.support.approvals import (
    ORDER_ID,
    ApprovalHarness,
    cancel_turn,
    data,
    names,
    order,
    sse,
)
from tests.support.chat import envelope
from tests.support.llm import text_turn, tool_turn
from tests.support.tokens import jwks

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default


@pytest.fixture(autouse=True)
def identity() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        yield route


async def test_should_reach_the_approval_card_in_one_message_when_the_order_is_named() -> None:
    harness = ApprovalHarness()
    harness.script(cancel_turn())
    async with harness.client() as client:
        conversation_id = await harness.conversation(client)
        response = await harness.post(client, conversation_id, f"Cancel order {ORDER_ID}")

    events = sse(response)
    assert names(events) == ["message.start", "tool.call", "approval.required", "message.end"]
    end = data(events, "message.end")
    assert (end["finishReason"], end["promptVersion"]) == ("awaiting_approval", "assistant.v4")
    (request,) = harness.model.requests
    assert "call cancel_order for it right away" in request.system
    assert "Never ask the customer to confirm the cancellation in the chat" in request.system
    assert harness.tools.approved_calls == []


async def test_should_propose_the_single_pending_order_found_by_a_lookup() -> None:
    harness = ApprovalHarness()
    page: JsonValue = {"items": [order()], "totalCount": 1, "hasNextPage": False}
    harness.tools.results["list_my_orders"] = envelope(page)
    harness.script(
        tool_turn(("toolu_list", "list_my_orders", {"status": "PENDING"})),
        cancel_turn(),
    )
    async with harness.client() as client:
        conversation_id = await harness.conversation(client)
        response = await harness.post(client, conversation_id, "Cancel my pending order")

    events = sse(response)
    assert "token" not in names(events)  # no confirmation question in the chat
    assert data(events, "approval.required")["orderId"] == ORDER_ID
    assert data(events, "message.end")["finishReason"] == "awaiting_approval"
    assert harness.model.calls == 2
    assert harness.tools.approved_calls == []


async def test_should_not_need_a_chat_yes_before_the_approval_card() -> None:
    harness = ApprovalHarness()
    harness.script(cancel_turn(), text_turn("Okay, nothing was changed."))
    async with harness.client() as client:
        conversation_id = await harness.conversation(client)
        proposed = await harness.post(client, conversation_id, f"Cancel {ORDER_ID}")
        approval_id = data(sse(proposed), "approval.required")["approvalId"]
        decided = await harness.decide(client, conversation_id, str(approval_id), "reject")

    assert data(sse(decided), "approval.resolved")["outcome"] == "rejected"
    assert len(harness.store.messages) == 1
