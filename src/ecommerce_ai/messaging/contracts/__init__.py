"""Inbound event contracts: Pydantic models of the Kafka events this service consumes.

The source of truth is ``ecommerce-infra/docs/events.md``; the models are frozen, ignore unknown
fields and pin the schema ``version`` (constitution V).
"""

from ecommerce_ai.messaging.contracts.events import (
    ORDER_CANCELLED_TOPIC,
    PAYMENT_FAILED_TOPIC,
    STOCK_REJECTED_TOPIC,
    TOPIC_MODELS,
    EventEnvelope,
    OrderCancelled,
    PaymentFailed,
    StockRejected,
    StockRejectReason,
    TriageEvent,
)

__all__ = [
    "ORDER_CANCELLED_TOPIC",
    "PAYMENT_FAILED_TOPIC",
    "STOCK_REJECTED_TOPIC",
    "TOPIC_MODELS",
    "EventEnvelope",
    "OrderCancelled",
    "PaymentFailed",
    "StockRejectReason",
    "StockRejected",
    "TriageEvent",
]
