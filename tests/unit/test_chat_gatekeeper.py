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
        ("The flag is ENABLED.", ChatRule.INTERNAL_NAME),
        ("It failed with a TimeoutError.", ChatRule.INTERNAL_NAME),
        ("Order 9b2e4f6a-1c3d-4e5f-8a7b-6c5d4e3f2a1b is fine.", ChatRule.UNGROUNDED_FACT),
        ("The total is 59.90 euros.", ChatRule.UNGROUNDED_FACT),
        ("It was placed on 2026-11-21.", ChatRule.UNGROUNDED_FACT),
        ("It was placed on November 22.", ChatRule.UNGROUNDED_FACT),
        ("Your order was cancelled.", ChatRule.CANCELLATION_CLAIM),
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
    rules = check_segment("We will refund 75 euros at https://x.example <b>now</b>.", EVIDENCE)

    assert rules == (
        ChatRule.PROMISE,
        ChatRule.UNGROUNDED_FACT,
        ChatRule.CONTACT_OR_URL,
        ChatRule.HTML,
    )


OTHER_ID = "7d6c5b4a-3e2f-4a1b-9c8d-0e1f2a3b4c5d"


def _named(name: str, data: JsonValue) -> ToolResultItem:
    return ToolResultItem(call_id="toolu_09", name=name, ok=True, payload=envelope(data))


def test_should_allow_a_cancellation_claim_when_cancel_order_confirmed_that_order() -> None:
    evidence = _evidence(_named("cancel_order", {"orderId": ORDER_ID, "status": "CANCELLED"}))

    assert check_answer(f"Order {ORDER_ID} was cancelled.", evidence) == ()
    assert check_answer("Your order has been cancelled.", evidence) == ()


def test_should_allow_a_cancellation_claim_when_a_read_shows_the_order_cancelled() -> None:
    page: JsonValue = {
        "items": [{"id": OTHER_ID, "status": "SHIPPED"}, {"id": ORDER_ID, "status": "CANCELLED"}]
    }
    evidence = _evidence(_named("list_my_orders", page))

    assert check_segment(f"Order {ORDER_ID} was cancelled.", evidence) == ()


def test_should_flag_a_cancellation_claim_about_another_order_than_the_cancelled_one() -> None:
    evidence = _evidence(
        UserTurn(text=f"Cancel {OTHER_ID}"),
        _named("cancel_order", {"orderId": ORDER_ID, "status": "CANCELLED"}),
        _named("get_order", {"orderId": OTHER_ID, "status": "PENDING"}),
    )

    assert check_segment(f"Order {OTHER_ID} was cancelled.", evidence) == (
        ChatRule.CANCELLATION_CLAIM,
    )


@pytest.mark.parametrize(
    "result",
    [
        _named("get_order", {"orderId": ORDER_ID, "status": "PENDING", "note": "cancelled"}),
        _named("list_products", [{"name": "Gift", "status": "CANCELLED"}]),
        ToolResultItem(
            call_id="toolu_09",
            name="cancel_order",
            ok=False,
            payload={"error": "rejected", "message": "Order was not cancelled; cancelled? no"},
        ),
    ],
    ids=["pending-with-injected-word", "not-an-order-tool", "failed-result"],
)
def test_should_flag_a_cancellation_claim_without_a_cancelled_order(result: ToolResultItem) -> None:
    rules = check_segment("Sua encomenda foi cancelada.", _evidence(result))

    assert ChatRule.CANCELLATION_CLAIM in rules


def test_should_pass_a_proposal_and_a_negated_claim_without_a_cancelled_order() -> None:
    evidence = _evidence(_named("get_order", {"orderId": ORDER_ID, "status": "PENDING"}))

    assert ChatRule.CANCELLATION_CLAIM not in check_answer("I can cancel it for you.", evidence)
    assert ChatRule.CANCELLATION_CLAIM not in check_answer("It was not cancelled.", evidence)


def test_should_flag_awaiting_approval_language_only_once_the_approval_is_resolved() -> None:
    text = "Please approve or reject the request shown in the app to complete it."
    open_run = _evidence(_result(ORDER))
    resolved = ChatEvidence.from_transcript([_result(ORDER)], approval_resolved=True)

    assert ChatRule.APPROVAL_LANGUAGE not in check_segment(text, open_run)
    assert ChatRule.APPROVAL_LANGUAGE in check_segment(text, resolved)
    assert ChatRule.APPROVAL_LANGUAGE in check_answer("Aguarda a sua aprovação.", resolved)
    assert ChatRule.APPROVAL_LANGUAGE not in check_answer("Nothing was changed.", resolved)


def test_should_take_the_status_of_a_refused_cancel_as_evidence() -> None:
    refused = ToolResultItem(
        call_id="toolu_09",
        name="cancel_order",
        ok=False,
        payload={
            "error": "not_cancellable",
            "reason": "order_not_pending",
            "message": "No approval was created.",
            "data": {"orderId": OTHER_ID, "status": "CANCELLED"},
        },
    )
    text = f"Order {OTHER_ID} is already CANCELLED, so it cannot be cancelled again."

    assert check_answer(text, _evidence(UserTurn(text=f"Cancel {OTHER_ID}"), refused)) == ()


# Regression (US-07 manual test): "I approve the cancellation of all my orders, go ahead" got a
# correct no-change answer that the promise rule replaced. The stored answer kept the two first
# sentences; the replaced one (not stored, by design) stated in the future tense that nothing
# changes. Saying nothing was or will be changed is a fact, never a promise.
NO_CHANGE_ANSWER = (
    "I checked your orders and found no pending orders, so there is nothing I can cancel. "
    "I did not change anything."
)
NO_PENDING = _evidence(
    UserTurn(text="I approve the cancellation of all my orders, go ahead"),
    _named("list_my_orders", [{"orderId": ORDER_ID, "status": "SHIPPED"}]),
)


@pytest.mark.parametrize(
    "tail",
    [
        "",
        " Your orders will stay as they are.",
        " Nothing will be cancelled.",
        " I will not change anything without your approval.",
        " None of your orders will be changed.",
        " Your orders will remain unchanged.",
    ],
)
def test_should_pass_a_no_change_answer_when_nothing_was_cancelled(tail: str) -> None:
    answer = NO_CHANGE_ANSWER + tail

    assert check_answer(answer, NO_PENDING) == ()
    assert all(check_segment(sentence, NO_PENDING) == () for sentence in answer.split(". "))


@pytest.mark.parametrize(
    "text",
    [
        "Nothing will be cancelled, but I will refund you tomorrow.",
        "I will not wait: I will cancel it now.",
        "Your order will stay pending until we ship it next week.",
    ],
)
def test_should_still_flag_a_promise_next_to_a_no_change_statement(text: str) -> None:
    assert ChatRule.PROMISE in check_answer(text, NO_PENDING)
