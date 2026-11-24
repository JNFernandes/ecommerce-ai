"""Pin the event models to the field tables of ``ecommerce-infra/docs/events.md``.

A change to those tables must reach ``events.md`` first; this test then fails until the models
follow (constitution V).
"""

import pytest

from ecommerce_ai.agents.triage.model import ResultSource, SuggestedAction
from ecommerce_ai.agents.triage_intake.model import TriageCause
from ecommerce_ai.messaging.contracts import (
    ORDER_CANCELLED_TOPIC,
    PAYMENT_FAILED_TOPIC,
    STOCK_REJECTED_TOPIC,
    TOPIC_MODELS,
    OrderTriageCompleted,
    StockRejectReason,
)

ENVELOPE = {"eventId": True, "aggregateId": True, "occurredAt": True, "version": True}

# JSON field -> required, as documented in events.md. Fields the consumer does not read
# (`failedAt`, `items`, `rejectedAt`) are deliberately not modelled and are ignored.
DOCUMENTED_FIELDS = {
    ORDER_CANCELLED_TOPIC: {**ENVELOPE, "customerId": True, "reason": False},
    PAYMENT_FAILED_TOPIC: {**ENVELOPE, "orderId": True, "reason": True},
    STOCK_REJECTED_TOPIC: {**ENVELOPE, "orderId": True, "reason": True},
}


@pytest.mark.parametrize("topic", sorted(DOCUMENTED_FIELDS))
def test_should_match_the_documented_fields_when_reading_each_topic(topic: str) -> None:
    model = TOPIC_MODELS[topic]

    actual = {field.alias: field.is_required() for field in model.model_fields.values()}

    assert actual == DOCUMENTED_FIELDS[topic]


def test_should_match_the_documented_stock_reject_reasons() -> None:
    assert {reason.value for reason in StockRejectReason} == {
        "insufficient stock",
        "unknown product",
        "unknown product and insufficient stock",
    }


# `ai.order-triage-completed`, produced by this service: every field is required (`modelId` is
# required and nullable).
TRIAGE_COMPLETED_FIELDS = {
    **ENVELOPE,
    "orderId": True,
    "cause": True,
    "explanation": True,
    "suggestedAction": True,
    "confidence": True,
    "promptVersion": True,
    "modelId": True,
    "source": True,
}


def test_should_match_the_documented_fields_when_producing_triage_completed() -> None:
    actual = {
        field.alias: field.is_required() for field in OrderTriageCompleted.model_fields.values()
    }

    assert actual == TRIAGE_COMPLETED_FIELDS


def test_should_match_the_documented_enums_when_producing_triage_completed() -> None:
    assert {cause.value for cause in TriageCause} == {
        "PAYMENT_FAILED",
        "INSUFFICIENT_STOCK",
        "UNKNOWN_PRODUCT",
        "CANCELLATION_REQUESTED",
        "UNKNOWN",
    }
    assert {action.value for action in SuggestedAction} == {
        "CONTACT_CUSTOMER",
        "RESTOCK",
        "FIX_CATALOG",
        "REVIEW",
        "NONE",
    }
    assert {source.value for source in ResultSource} == {"LLM", "FALLBACK_TEMPLATE"}
