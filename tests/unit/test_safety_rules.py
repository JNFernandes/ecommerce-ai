"""The shared gatekeeper rules, used by the triage and the assistant gatekeepers."""

import pytest
from pydantic import JsonValue

from ecommerce_ai.agents.safety.rules import (
    Evidence,
    cancellation_claims,
    cancellation_claims_grounded,
    facts_grounded,
    has_contact_or_url,
    has_html,
    has_internal_name,
    has_promise,
    internal_name_matches,
    json_leaves,
    scrub_ids_and_dates,
)

ORDER_ID = "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"


def _evidence(*strings: str, topics: tuple[str, ...] = ()) -> Evidence:
    return Evidence.from_strings(strings, topics)


def test_should_yield_only_leaf_values_when_walking_json() -> None:
    value: JsonValue = {"id": "a", "items": [{"qty": 2, "price": 9.5}], "paid": True, "note": None}

    assert list(json_leaves(value)) == ["a", "2", "9.5"]


def test_should_index_uuids_dates_and_numbers_when_built_from_strings() -> None:
    evidence = _evidence(ORDER_ID.upper(), "2026-11-20T10:00:00Z", "total 49.90")

    assert evidence.uuids == {ORDER_ID}
    assert evidence.has_date(11, 20, 2026)
    assert not evidence.has_date(11, 21, None)
    # The parts of the ISO timestamp are numbers too (a date written as 20/11/2026 is grounded).
    assert evidence.numbers == {"49.9", "2026", "11", "20", "10", "0"}


def test_should_ground_ids_dates_and_numbers_of_extra_facts_but_not_their_status_words() -> None:
    evidence = Evidence.from_strings(["status PENDING"], facts=[f"Cancel {ORDER_ID}, 3 x SKU-001"])

    assert facts_grounded(f"Order {ORDER_ID}: 3 x SKU-001.", evidence)
    assert not facts_grounded("It was shipped.", Evidence.from_strings([], facts=["shipped?"]))


@pytest.mark.parametrize(
    "text",
    [
        f"Order {ORDER_ID} was placed on 2026-11-20.",
        "It cost 49.9 and was placed on November 20th, 2026.",
        "Your order is SHIPPED.",
        "Placed on 20 Nov.",
    ],
)
def test_should_accept_facts_when_they_appear_in_the_evidence(text: str) -> None:
    evidence = _evidence(ORDER_ID, "2026-11-20", "49.90", "SHIPPED")

    assert facts_grounded(text, evidence)


@pytest.mark.parametrize(
    "text",
    [
        "Order 0b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d is late.",
        "It was placed on 2026-11-21.",
        "It was placed on December 3.",
        "It cost 12.50.",
        "Your order was delivered.",
    ],
)
def test_should_reject_facts_when_they_are_not_in_the_evidence(text: str) -> None:
    assert not facts_grounded(text, _evidence(ORDER_ID, "2026-11-20", "49.90"))


def test_should_ground_a_status_word_when_its_topic_is_in_the_evidence() -> None:
    evidence = _evidence(topics=("orders.order-cancelled",))

    assert facts_grounded("The order was cancelled.", evidence)
    assert not facts_grounded("The order was cancelled.", _evidence())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We will refund you.", True),
        ("You'll get a voucher.", True),
        ("Paid by credit card.", False),
        ("Your order is on its way.", False),
    ],
)
def test_should_detect_promises_when_checking_text(text: str, expected: bool) -> None:
    assert has_promise(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("See https://example.org", True),
        ("Write to help@shop.example", True),
        ("Call +351 912 345 678", True),
        ("Visit shop.com", True),
        (f"Order {ORDER_ID} from 2026-11-20", False),
    ],
)
def test_should_detect_contacts_and_urls_when_checking_text(text: str, expected: bool) -> None:
    assert has_contact_or_url(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A KeyError happened", True),
        ("Topic orders.order-cancelled", True),
        ("field order_id", True),
        ("status SHIPPED", True),
        ("customerId", True),
        (f"Your order {ORDER_ID} is on its way.", False),
    ],
)
def test_should_detect_internal_names_when_checking_text(text: str, expected: bool) -> None:
    assert has_internal_name(text) is expected


def test_should_list_each_internal_name_when_asked_for_matches() -> None:
    assert internal_name_matches("It is SHIPPED and PAID, see order_id") == [
        "SHIPPED",
        "PAID",
        "order_id",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<b>bold</b>", True),
        ('<a href="x">link</a>', True),
        ("<br/>", True),
        ("<!-- hidden -->", True),
        ("2 < 3 and 4 > 1", False),
        ("- a list item\n- another", False),
    ],
)
def test_should_detect_html_when_checking_text(text: str, expected: bool) -> None:
    assert has_html(text) is expected


def test_should_blank_ids_and_iso_dates_when_scrubbing() -> None:
    scrubbed = scrub_ids_and_dates(f"{ORDER_ID} on 2026-11-20T10:00:00Z")

    assert ORDER_ID not in scrubbed
    assert "2026" not in scrubbed


OTHER_ID = "4c1e2f3a-5b6d-4e7f-8a9b-0c1d2e3f4a5b"


@pytest.mark.parametrize(
    ("text", "claims"),
    [
        ("Your order was cancelled.", [()]),
        ("It has been canceled.", [()]),
        (f"Order {ORDER_ID} is cancelled. Anything else?", [(ORDER_ID,)]),
        ("I will cancel it now.", [()]),
        ("Your cancellation is complete.", [()]),
        ("A sua encomenda foi cancelada.", [()]),
        ("Cancelei a encomenda.", [()]),
        ("Vou cancelar a encomenda.", [()]),
        ("O cancelamento foi concluído.", [()]),
    ],
)
def test_should_find_a_cancellation_claim_in_english_or_portuguese(
    text: str, claims: list[tuple[str, ...]]
) -> None:
    assert list(cancellation_claims(text)) == claims


@pytest.mark.parametrize(
    "text",
    [
        "I can cancel it for you once you approve.",
        "Do you want it cancelled?",
        "Your order was not cancelled.",
        "It wasn't cancelled; nothing changed.",
        "It has never been cancelled.",
        "A encomenda não foi cancelada.",
        "Please approve the cancellation request.",
        "Quer confirmar o cancelamento?",
    ],
)
def test_should_not_take_a_proposal_question_or_negation_for_a_claim(text: str) -> None:
    assert list(cancellation_claims(text)) == []


def test_should_ground_a_claim_only_with_a_cancelled_order() -> None:
    claim = f"Order {ORDER_ID} was cancelled."

    assert cancellation_claims_grounded(claim, {ORDER_ID}, any_cancelled=True)
    assert not cancellation_claims_grounded(claim, {OTHER_ID}, any_cancelled=True)
    assert not cancellation_claims_grounded(claim, set(), any_cancelled=True)
    assert cancellation_claims_grounded("It was cancelled.", set(), any_cancelled=True)
    assert not cancellation_claims_grounded("It was cancelled.", set(), any_cancelled=False)
    assert cancellation_claims_grounded("Nothing changed.", set(), any_cancelled=False)
