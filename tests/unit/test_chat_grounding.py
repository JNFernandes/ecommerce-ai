"""Correct answers are not over-blocked; a replaced one reports only its rule names (fix(US-06)).

Facts (ids, amounts, dates, quantities, SKUs) count as grounded when a tool result or the
customer's own message holds them; ``message.end`` and the ``chat_run`` log line carry the names
of the rules a replaced answer broke, never its text.
"""

import io
import json
from collections.abc import Iterator

import pytest
import respx
from pydantic import JsonValue

from ecommerce_ai.agents.assistant.gatekeeper import SAFE_MESSAGE
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.settings import Settings
from tests.support.approvals import ORDER_ID, ApprovalHarness, cancel_turn, data, order, sse
from tests.support.chat import envelope
from tests.support.llm import text_turn, tool_turn
from tests.support.sse import SseEvent
from tests.support.tokens import jwks

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default
PAGE: JsonValue = {
    "items": [
        {
            "orderId": ORDER_ID,
            "status": "PENDING",
            "totalAmount": 25.0,
            "placedAt": "2026-11-24T10:15:30Z",
            "items": [{"productId": "SKU-001", "quantity": 1, "unitPrice": 25.0, "subtotal": 25.0}],
        }
    ],
    "totalCount": 1,
    "hasNextPage": False,
}


@pytest.fixture(autouse=True)
def identity() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        yield route


def _text(events: list[SseEvent]) -> str:
    """The answer text the customer saw: every ``token`` in order."""
    parts = [event.data for event in events if event.event == "token"]
    return "".join(str(part["text"]) for part in parts if isinstance(part, dict))


@pytest.mark.parametrize(
    "placed",
    ["2026-11-24", "24/11/2026", "24-11-2026", "24 November 2026 at 10:15", "Nov 24, 2026"],
)
async def test_should_send_a_plain_listing_built_from_list_my_orders(placed: str) -> None:
    harness = ApprovalHarness()
    harness.tools.results["list_my_orders"] = envelope(PAGE)
    answer = (
        f"I found one pending order: Order {ORDER_ID}, total 25.0, placed {placed}, 1 x SKU-001"
    )
    harness.script(tool_turn(("toolu_list", "list_my_orders", {"status": "PENDING"})))
    harness.script(text_turn(answer))
    async with harness.client() as client:
        conversation_id = await harness.conversation(client)
        response = await harness.post(client, conversation_id, "Do I have pending orders?")

    events = sse(response)
    assert _text(events) == answer
    end = data(events, "message.end")
    assert (end["finishReason"], end["violations"]) == ("completed", [])


async def test_should_ground_facts_the_customer_wrote_in_their_own_message() -> None:
    harness = ApprovalHarness()
    harness.tools.results["get_order"] = envelope(order(total=25.0))
    answer = f"Order {ORDER_ID} has 3 x SKU-777 as you said, total 25.0."
    harness.script(tool_turn(("toolu_get", "get_order", {"orderId": ORDER_ID})))
    harness.script(text_turn(answer))
    async with harness.client() as client:
        conversation_id = await harness.conversation(client)
        response = await harness.post(client, conversation_id, f"{ORDER_ID} has 3 x SKU-777?")

    assert data(sse(response), "message.end")["violations"] == []


@pytest.mark.parametrize(
    "answer",
    [
        f"Order {ORDER_ID} cannot be cancelled because it is CONFIRMED.",
        f"I couldn't cancel order {ORDER_ID}. Its status is CONFIRMED; only PENDING orders"
        " can be cancelled.",
    ],
)
async def test_should_explain_an_order_that_cannot_be_cancelled(answer: str) -> None:
    harness = ApprovalHarness()
    harness.tools.results["get_order"] = envelope(order(status="CONFIRMED"))
    harness.script(cancel_turn(), text_turn(answer))
    async with harness.client() as client:
        conversation_id = await harness.conversation(client)
        response = await harness.post(client, conversation_id, f"Cancel order {ORDER_ID}")

    events = sse(response)
    assert data(events, "tool.result")["summary"] == "Order cannot be cancelled"
    assert _text(events) == answer
    assert data(events, "message.end")["finishReason"] == "completed"


async def test_should_report_only_the_rule_names_of_a_replaced_answer() -> None:
    harness = ApprovalHarness()
    harness.script(text_turn("We will refund 99.5 euros tomorrow."))
    stream = io.StringIO()
    async with harness.client() as client:
        configure_logging("INFO", stream)
        conversation_id = await harness.conversation(client)
        response = await harness.post(client, conversation_id, "Where is my refund?")

    events = sse(response)
    assert data(events, "token") == {"text": SAFE_MESSAGE}
    end = data(events, "message.end")
    assert (end["finishReason"], end["violations"]) == ("replaced", ["promise", "ungrounded_fact"])
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    (run,) = [line for line in lines if line["event"] == "chat_run"]
    assert run["violations"] == ["promise", "ungrounded_fact"]
    assert "99.5" not in stream.getvalue()
