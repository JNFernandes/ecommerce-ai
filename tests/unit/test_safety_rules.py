"""The shared gatekeeper rules, used by the triage and the assistant gatekeepers."""

import pytest
from pydantic import JsonValue

from ecommerce_ai.agents.safety.rules import (
    Evidence,
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
    assert evidence.numbers == {"49.9"}


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
