"""The deterministic gatekeeper: one pass and one fail case per rule (FR-008, research 8)."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import JsonValue

from ecommerce_ai.agents.triage.gatekeeper import RULE_MESSAGES, check
from ecommerce_ai.agents.triage.model import (
    CaseForTriage,
    DraftOutput,
    EvidenceItem,
    GateRule,
    SuggestedAction,
)
from ecommerce_ai.agents.triage_intake.model import TriageCause

ORDER_ID = UUID("7d0f4c1e-2b3a-4c5d-8e9f-0a1b2c3d4e5f")
PAYMENT_ID = UUID("11111111-2222-4333-8444-555555555555")
OCCURRED_AT = datetime(2026, 11, 20, 9, 30, tzinfo=UTC)
EVIDENCE_REASON = "card declined by issuer 4242"


def _item(topic: str, reason: str | None, payload: dict[str, JsonValue]) -> EvidenceItem:
    return EvidenceItem(topic=topic, reason=reason, occurred_at=OCCURRED_AT, payload=payload)


def _payment_case() -> CaseForTriage:
    return CaseForTriage(
        case_id=UUID("99999999-8888-4777-8666-555555555555"),
        order_id=ORDER_ID,
        cause=TriageCause.PAYMENT_FAILED,
        evidence=(
            _item(
                "payments.payment-failed",
                EVIDENCE_REASON,
                {
                    "eventId": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                    "aggregateId": str(PAYMENT_ID),
                    "orderId": str(ORDER_ID),
                    "reason": EVIDENCE_REASON,
                    "amount": 49.9,
                    "failedAt": OCCURRED_AT.isoformat(),
                },
            ),
            _item(
                "orders.order-cancelled",
                "payment failed: card declined",
                {"aggregateId": str(ORDER_ID), "reason": "payment failed: card declined"},
            ),
        ),
    )


def _draft(
    explanation: str, action: SuggestedAction = SuggestedAction.CONTACT_CUSTOMER
) -> DraftOutput:
    return DraftOutput(explanation=explanation, suggested_action=action, confidence=0.8)


def _rules(
    explanation: str, action: SuggestedAction = SuggestedAction.CONTACT_CUSTOMER
) -> set[GateRule]:
    return {violation.rule for violation in check(_draft(explanation, action), _payment_case())}


def test_should_pass_when_the_draft_states_only_grounded_facts_in_plain_text() -> None:
    explanation = (
        f"The payment of 49.90 for order {ORDER_ID} was declined on November 20, 2026, "
        "so the order was cancelled. The card was declined."
    )

    assert check(_draft(explanation), _payment_case()) == []


@pytest.mark.parametrize(
    "explanation",
    [
        "The order was cancelled and a refund was issued.",
        "We will contact the customer.",
        "You'll get a voucher for the next order.",
        "The customer gets compensation for the trouble.",
        "The order is going to be delivered tomorrow.",
    ],
)
def test_should_report_a_promise_when_the_draft_announces_a_future_action(
    explanation: str,
) -> None:
    assert GateRule.PROMISE in _rules(explanation)


def test_should_not_report_a_promise_when_the_draft_mentions_a_credit_card() -> None:
    assert GateRule.PROMISE not in _rules("The credit card payment was declined.")


@pytest.mark.parametrize(
    "explanation",
    [
        "The payment of 59.90 was declined.",
        "The payment failed because the card limit is 1,000.",
        "The payment 22222222-3333-4444-8555-666666666666 was declined.",
        "The payment was declined on 2026-11-21.",
        "The payment was declined on November 21, 2026.",
        "The payment was declined in October 2026.",
        "The order was shipped before the payment was declined.",
        "The order was paid and then cancelled.",
    ],
)
def test_should_report_an_ungrounded_fact_when_the_evidence_does_not_state_it(
    explanation: str,
) -> None:
    assert GateRule.UNGROUNDED_FACT in _rules(explanation)


@pytest.mark.parametrize(
    "explanation",
    [
        "The payment of 49.90 was declined.",
        f"The payment {PAYMENT_ID} was declined.",
        f"The payment {str(PAYMENT_ID).upper()} was declined.",
        "The payment was declined on 2026-11-20.",
        "The payment was declined on 20 November 2026.",
        "The payment was declined in November 2026.",
        "The order was cancelled.",
    ],
)
def test_should_not_report_an_ungrounded_fact_when_the_evidence_states_it(
    explanation: str,
) -> None:
    assert GateRule.UNGROUNDED_FACT not in _rules(explanation)


def test_should_report_a_cancelled_status_when_no_cancellation_event_is_stored() -> None:
    case = _payment_case()
    payment_only = case.model_copy(update={"evidence": case.evidence[:1]})
    draft = _draft("The order was cancelled.")

    assert {violation.rule for violation in check(draft, payment_only)} == {
        GateRule.UNGROUNDED_FACT
    }


@pytest.mark.parametrize(
    "explanation",
    [
        "See https://shop.example/orders for details.",
        "Details are on www.example.org today.",
        "Ask the support team at example.com for help.",
        "Write to help@example.org about the payment.",
        "Call 555 123 4567 about the payment.",
        "Call +351 912 345 678 about the payment.",
    ],
)
def test_should_report_a_contact_or_url_when_the_draft_contains_one(explanation: str) -> None:
    assert GateRule.CONTACT_OR_URL in _rules(explanation)


def test_should_not_report_a_contact_when_the_draft_has_a_date_and_an_id() -> None:
    explanation = f"The payment {PAYMENT_ID} was declined on 2026-11-20."

    assert GateRule.CONTACT_OR_URL not in _rules(explanation)


def test_should_report_length_when_the_draft_has_more_than_three_sentences() -> None:
    explanation = "The payment was declined. The card was declined. Nothing else. Truly."

    assert GateRule.LENGTH in _rules(explanation)


def test_should_report_length_when_the_draft_is_longer_than_the_limit() -> None:
    draft = DraftOutput.model_construct(
        explanation="The payment was declined " + "again " * 100 + ".",
        suggested_action=SuggestedAction.CONTACT_CUSTOMER,
        confidence=0.5,
    )

    assert GateRule.LENGTH in {violation.rule for violation in check(draft, _payment_case())}


def test_should_not_report_length_when_the_draft_has_three_sentences_with_decimals() -> None:
    explanation = "The payment of 49.90 was declined. The card was declined. Nothing else."

    assert GateRule.LENGTH not in _rules(explanation)


@pytest.mark.parametrize(
    "explanation",
    [
        "The event payments.payment-failed says the card was declined.",
        "The cause is PAYMENT_FAILED.",
        "The cause is REVIEW.",
        "The field orderId shows the payment was declined.",
        "The PaymentFailed event says the card was declined.",
        "The failed_at field shows the payment was declined.",
        "The payload {reason} shows the card was declined.",
        "The `reason` shows the card was declined.",
        "A Traceback shows the card was declined.",
    ],
)
def test_should_report_an_internal_name_when_the_draft_contains_one(explanation: str) -> None:
    assert GateRule.INTERNAL_NAME in _rules(explanation)


def test_should_not_report_an_internal_name_when_the_draft_uses_a_short_acronym() -> None:
    assert GateRule.INTERNAL_NAME not in _rules("The card was declined by the PSP.")


@pytest.mark.parametrize(
    "explanation",
    [
        "The payment was declined.\nThe card was declined.",
        "**The payment** was declined.",
        "# The payment was declined.",
        "- The payment was declined.",
        "The [payment](x) was declined.",
        "The payment | was declined.",
        "The <b>payment</b> was declined.",
    ],
)
def test_should_report_not_plain_when_the_draft_uses_markup(explanation: str) -> None:
    assert GateRule.NOT_PLAIN in _rules(explanation)


def test_should_not_report_not_plain_when_the_draft_uses_a_hyphenated_word() -> None:
    assert GateRule.NOT_PLAIN not in _rules("The card was declined by a third-party check.")


def test_should_report_an_action_not_allowed_when_the_cause_does_not_allow_it() -> None:
    assert GateRule.ACTION_NOT_ALLOWED in _rules("The card was declined.", SuggestedAction.RESTOCK)


@pytest.mark.parametrize("action", [SuggestedAction.CONTACT_CUSTOMER, SuggestedAction.REVIEW])
def test_should_accept_the_action_when_the_cause_allows_it(action: SuggestedAction) -> None:
    assert GateRule.ACTION_NOT_ALLOWED not in _rules("The card was declined.", action)


def test_should_report_each_broken_rule_once_with_its_fixed_message_and_the_attempt() -> None:
    draft = _draft(
        "Refund of 77 via https://x.example and 88 via SOME_FLAG. Refund again.",
        SuggestedAction.RESTOCK,
    )

    violations = check(draft, _payment_case(), attempt=2)

    rules = [violation.rule for violation in violations]
    assert sorted(rules) == sorted(set(rules))
    assert set(rules) >= {
        GateRule.PROMISE,
        GateRule.UNGROUNDED_FACT,
        GateRule.CONTACT_OR_URL,
        GateRule.INTERNAL_NAME,
        GateRule.ACTION_NOT_ALLOWED,
    }
    assert all(violation.attempt == 2 for violation in violations)
    assert all(violation.message == RULE_MESSAGES[violation.rule] for violation in violations)


def test_should_keep_evidence_values_out_of_every_message() -> None:
    assert set(RULE_MESSAGES) == set(GateRule)
    for message in RULE_MESSAGES.values():
        assert EVIDENCE_REASON not in message
        assert "4242" not in message
        assert str(ORDER_ID) not in message


def test_should_pass_a_neutral_draft_when_the_case_has_no_evidence() -> None:
    case = _payment_case().model_copy(update={"evidence": (), "cause": TriageCause.UNKNOWN})
    draft = _draft("The stored events do not explain the failure.", SuggestedAction.REVIEW)

    assert check(draft, case) == []
