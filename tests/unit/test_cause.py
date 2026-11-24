"""Cause derivation follows the data-model table (FR-006)."""

import pytest

from ecommerce_ai.agents.triage_intake.cause import TRIAGE_TOPICS, derive_cause
from ecommerce_ai.agents.triage_intake.model import TriageCause
from ecommerce_ai.messaging.contracts import (
    ORDER_CANCELLED_TOPIC,
    PAYMENT_FAILED_TOPIC,
    STOCK_REJECTED_TOPIC,
    TOPIC_MODELS,
)


@pytest.mark.parametrize(
    ("topic", "reason", "expected"),
    [
        (PAYMENT_FAILED_TOPIC, "card declined", TriageCause.PAYMENT_FAILED),
        (PAYMENT_FAILED_TOPIC, "insufficient funds", TriageCause.PAYMENT_FAILED),
        (STOCK_REJECTED_TOPIC, "insufficient stock", TriageCause.INSUFFICIENT_STOCK),
        (STOCK_REJECTED_TOPIC, "unknown product", TriageCause.UNKNOWN_PRODUCT),
        (
            STOCK_REJECTED_TOPIC,
            "unknown product and insufficient stock",
            TriageCause.UNKNOWN_PRODUCT,
        ),
        (STOCK_REJECTED_TOPIC, "out of warehouses", TriageCause.UNKNOWN),
        (ORDER_CANCELLED_TOPIC, None, TriageCause.CANCELLATION_REQUESTED),
        (ORDER_CANCELLED_TOPIC, "payment failed", TriageCause.PAYMENT_FAILED),
        (ORDER_CANCELLED_TOPIC, "payment failed: card declined", TriageCause.PAYMENT_FAILED),
        (
            ORDER_CANCELLED_TOPIC,
            "stock rejected: insufficient stock",
            TriageCause.INSUFFICIENT_STOCK,
        ),
        (ORDER_CANCELLED_TOPIC, "stock rejected: unknown product", TriageCause.UNKNOWN_PRODUCT),
        (
            ORDER_CANCELLED_TOPIC,
            "stock rejected: unknown product and insufficient stock",
            TriageCause.UNKNOWN_PRODUCT,
        ),
        (ORDER_CANCELLED_TOPIC, "stock rejected", TriageCause.UNKNOWN),
        (ORDER_CANCELLED_TOPIC, "stock rejected: out of warehouses", TriageCause.UNKNOWN),
        (ORDER_CANCELLED_TOPIC, "customer changed their mind", TriageCause.UNKNOWN),
        (ORDER_CANCELLED_TOPIC, "", TriageCause.UNKNOWN),
    ],
)
def test_should_derive_the_cause_from_topic_and_reason_when_an_event_arrives(
    topic: str, reason: str | None, expected: TriageCause
) -> None:
    assert derive_cause(topic, reason) is expected


def test_should_reject_a_topic_the_intake_does_not_know() -> None:
    with pytest.raises(ValueError, match="unsupported topic"):
        derive_cause("orders.order-placed", None)


def test_should_know_exactly_the_topics_messaging_consumes() -> None:
    assert set(TRIAGE_TOPICS) == set(TOPIC_MODELS)
