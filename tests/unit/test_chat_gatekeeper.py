"""The chat gatekeeper: one test per rule, grounded values and allowances (FR-015, research §9)."""

import pytest
from pydantic import JsonValue

from ecommerce_ai.agents.assistant.gatekeeper import (
    NO_ANSWER_MESSAGE,
    SAFE_MESSAGE,
    TOOL_LIMIT_MESSAGE,
    ChatEvidence,
    ChatRule,
    check_answer,
    check_segment,
)
from ecommerce_ai.agents.assistant.model import (
    AssistantText,
    ToolResultItem,
    TranscriptItem,
    UserTurn,
)
from tests.support.chat import envelope

ORDER_ID = "3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b"
OWN_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"

ORDER: dict[str, JsonValue] = {
    "id": ORDER_ID,
    "status": "SHIPPED",
    "total": 49.9,
    "createdAt": "2026-11-20T10:15:00Z",
    "items": [{"name": "MacBook Air", "quantity": 2}],
}


def _evidence(*items: TranscriptItem) -> ChatEvidence:
    return ChatEvidence.from_transcript(items)


def _result(data: JsonValue, ok: bool = True) -> ToolResultItem:
    failure: dict[str, JsonValue] = {"error": "upstream_error", "message": "down 503"}
    payload = envelope(data) if ok else failure
    return ToolResultItem(call_id="toolu_01", name="get_order", ok=ok, payload=payload)


EVIDENCE = _evidence(UserTurn(text=f"Where is {OWN_ID}?"), _result(ORDER))


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("We will refund you.", ChatRule.PROMISE),
        ("You'll get a voucher.", ChatRule.PROMISE),
        ("Your order is going to arrive soon.", ChatRule.PROMISE),
        ("See https://shop.example for details.", ChatRule.CONTACT_OR_URL),
        ("Write to help@shop.example now.", ChatRule.CONTACT_OR_URL),
        ("Call +351 912 345 678 today.", ChatRule.CONTACT_OR_URL),
        ("The ecommerce-orders service is slow.", ChatRule.INTERNAL_NAME),
        ("An event went to orders.order-cancelled.", ChatRule.INTERNAL_NAME),
        ("The graphql API answered.", ChatRule.INTERNAL_NAME),
        ("It runs on localhost here.", ChatRule.INTERNAL_NAME),
        ("I used list_my_orders for that.", ChatRule.INTERNAL_NAME),
        ("The status is PENDING.", ChatRule.INTERNAL_NAME),
        ("It failed with a TimeoutError.", ChatRule.INTERNAL_NAME),
        ("Order 9b2e4f6a-1c3d-4e5f-8a7b-6c5d4e3f2a1b is fine.", ChatRule.UNGROUNDED_FACT),
        ("The total is 59.90 euros.", ChatRule.UNGROUNDED_FACT),
        ("It was placed on 2026-11-21.", ChatRule.UNGROUNDED_FACT),
        ("It was placed on November 22.", ChatRule.UNGROUNDED_FACT),
        ("Your order was cancelled.", ChatRule.UNGROUNDED_FACT),
        ("Your order is <b>here</b>.", ChatRule.HTML),
    ],
)
def test_should_flag_the_rule_when_a_segment_breaks_it(text: str, rule: ChatRule) -> None:
    assert rule in check_segment(text, EVIDENCE)


@pytest.mark.parametrize(
    "text",
    [
        f"Your order {ORDER_ID} is SHIPPED.",
        "It was shipped on 2026-11-20 and cost 49.90 euros.",
        "It was placed on November 20, 2026.",
        "You ordered 2 MacBook Air laptops.",
        f"I could not find {OWN_ID}.",
        "Here is what I found:\n- one order\n- nothing else\n",
        "I can only help with your orders, products and notifications.",
        SAFE_MESSAGE,
        NO_ANSWER_MESSAGE,
        TOOL_LIMIT_MESSAGE,
    ],
)
def test_should_pass_a_segment_when_its_facts_come_from_the_evidence(text: str) -> None:
    assert check_segment(text, EVIDENCE) == ()
    assert check_answer(text, EVIDENCE) == ()


def test_should_allow_a_past_tense_status_word_when_the_tool_data_holds_it() -> None:
    delivered = _evidence(_result({"status": "DELIVERED"}))

    assert check_segment("Your order was delivered.", delivered) == ()
    assert ChatRule.PROMISE in check_segment("It will be delivered.", delivered)
    assert ChatRule.PROMISE in check_segment("Your order was delivered.", EVIDENCE)


def test_should_never_allow_an_internal_name_even_when_a_tool_returned_it() -> None:
    leaky = _evidence(_result({"note": "see ecommerce-orders on localhost"}))

    assert ChatRule.INTERNAL_NAME in check_segment("Ask ecommerce-orders.", leaky)
    assert ChatRule.INTERNAL_NAME in check_segment("Try localhost.", leaky)


def test_should_not_take_evidence_from_errors_envelope_metadata_or_the_assistant() -> None:
    evidence = _evidence(
        _result(ORDER, ok=False),
        AssistantText(text="The total is 59.90."),
        _result({"status": "PAID"}),
    )

    assert ChatRule.UNGROUNDED_FACT in check_segment("It returned 503.", evidence)
    assert ChatRule.UNGROUNDED_FACT in check_segment("The total is 59.90.", evidence)
    assert ChatRule.INTERNAL_NAME in check_segment("It is untrusted_data.", evidence)
    assert check_segment("It is PAID.", evidence) == ()


def test_should_flag_a_rule_that_spans_two_segments_only_on_the_whole_answer() -> None:
    first, second = "It is <b\n", "class=x>ready"

    assert check_segment(first, EVIDENCE) == check_segment(second, EVIDENCE) == ()
    assert ChatRule.HTML in check_answer(first + second, EVIDENCE)


def test_should_report_each_broken_rule_once() -> None:
    rules = check_segment("We will refund 10 euros at https://x.example <b>now</b>.", EVIDENCE)

    assert rules == (
        ChatRule.PROMISE,
        ChatRule.UNGROUNDED_FACT,
        ChatRule.CONTACT_OR_URL,
        ChatRule.HTML,
    )
