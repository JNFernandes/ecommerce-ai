"""Validation rules of the consumed event models."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from ecommerce_ai.messaging.contracts import (
    ORDER_CANCELLED_TOPIC,
    PAYMENT_FAILED_TOPIC,
    STOCK_REJECTED_TOPIC,
    TOPIC_MODELS,
    EventEnvelope,
    OrderCancelled,
    PaymentFailed,
    StockRejected,
    StockRejectReason,
)

ORDER_ID = UUID("0b6f3c1e-8f7a-4a52-9c1d-2f4e5a6b7c8d")
AGGREGATE_ID = UUID("5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d")

type Json = str | int | list[dict[str, str | int]] | None


def _envelope(**overrides: Json) -> dict[str, Json]:
    body: dict[str, Json] = {
        "eventId": str(uuid4()),
        "aggregateId": str(AGGREGATE_ID),
        "occurredAt": "2026-11-24T10:15:30Z",
        "version": 1,
    }
    body.update(overrides)
    return body


def test_should_read_an_order_cancelled_event_when_it_is_valid() -> None:
    event = OrderCancelled.model_validate(
        _envelope(aggregateId=str(ORDER_ID), customerId=str(uuid4()), reason="stock rejected")
    )

    assert event.order_key == ORDER_ID
    assert event.reason == "stock rejected"
    assert event.occurred_at == datetime(2026, 11, 24, 10, 15, 30, tzinfo=UTC)


def test_should_treat_a_missing_cancellation_reason_as_none() -> None:
    event = OrderCancelled.model_validate(_envelope(customerId=str(uuid4())))

    assert event.reason is None


def test_should_use_order_id_not_aggregate_id_when_the_event_is_a_payment_failure() -> None:
    event = PaymentFailed.model_validate(
        _envelope(orderId=str(ORDER_ID), reason="amount exceeds threshold")
    )

    assert event.order_key == ORDER_ID
    assert event.aggregate_id == AGGREGATE_ID


def test_should_use_order_id_not_aggregate_id_when_the_event_is_a_stock_rejection() -> None:
    event = StockRejected.model_validate(
        _envelope(orderId=str(ORDER_ID), reason="unknown product and insufficient stock")
    )

    assert event.order_key == ORDER_ID
    assert event.reason is StockRejectReason.UNKNOWN_PRODUCT_AND_INSUFFICIENT_STOCK


def test_should_ignore_unknown_fields_when_the_producer_adds_them() -> None:
    event = StockRejected.model_validate(
        _envelope(
            orderId=str(ORDER_ID),
            reason="insufficient stock",
            items=[{"productId": "p-1", "requested": 2, "available": 0}],
            rejectedAt="2026-11-24T10:15:30Z",
        )
    )

    assert "items" not in event.model_dump(by_alias=True)


def test_should_be_immutable_once_validated() -> None:
    event = PaymentFailed.model_validate(_envelope(orderId=str(ORDER_ID), reason="declined"))

    with pytest.raises(ValidationError):
        event.reason = "changed"  # type: ignore[misc]  # assigning a frozen field must fail


@pytest.mark.parametrize(
    ("model", "body"),
    [
        (PaymentFailed, _envelope(version=2, orderId=str(ORDER_ID), reason="x")),
        (PaymentFailed, _envelope(eventId="not-a-uuid", orderId=str(ORDER_ID), reason="x")),
        (PaymentFailed, _envelope(orderId="order-1", reason="x")),
        (OrderCancelled, _envelope(aggregateId="order-1", customerId=str(uuid4()))),
        (OrderCancelled, _envelope(occurredAt="2026-11-24T10:15:30", customerId=str(uuid4()))),
        (OrderCancelled, _envelope(customerId=str(uuid4()), reason=5)),
        (PaymentFailed, _envelope(orderId=str(ORDER_ID))),
        (StockRejected, _envelope(orderId=str(ORDER_ID), reason="out of stock")),
    ],
    ids=[
        "version-2",
        "non-uuid-event-id",
        "non-uuid-order-id",
        "non-uuid-aggregate-id",
        "naive-datetime",
        "non-string-reason",
        "missing-reason",
        "undocumented-stock-reason",
    ],
)
def test_should_reject_an_event_when_it_breaks_the_contract(
    model: type[EventEnvelope], body: dict[str, Json]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(body)


def test_should_map_each_consumed_topic_to_its_model() -> None:
    assert sorted(TOPIC_MODELS) == [
        STOCK_REJECTED_TOPIC,
        ORDER_CANCELLED_TOPIC,
        PAYMENT_FAILED_TOPIC,
    ]
    assert TOPIC_MODELS[ORDER_CANCELLED_TOPIC] is OrderCancelled
    assert TOPIC_MODELS[PAYMENT_FAILED_TOPIC] is PaymentFailed
    assert TOPIC_MODELS[STOCK_REJECTED_TOPIC] is StockRejected


def test_should_not_resolve_an_order_when_only_the_envelope_is_known() -> None:
    envelope = EventEnvelope.model_validate(_envelope())

    with pytest.raises(NotImplementedError):
        _ = envelope.order_key
