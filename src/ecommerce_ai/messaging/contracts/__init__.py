"""Event contracts: Pydantic models of the Kafka events this service consumes and produces.

The source of truth is ``ecommerce-infra/docs/events.md``; the models are frozen and pin the
schema ``version`` (constitution V). Consumed events ignore unknown fields; the produced
``OrderTriageCompleted`` forbids them.
"""

from ecommerce_ai.messaging.contracts.events import (
    ORDER_CANCELLED_TOPIC,
    ORDER_TRIAGE_COMPLETED_TOPIC,
    PAYMENT_FAILED_TOPIC,
    STOCK_REJECTED_TOPIC,
    TOPIC_MODELS,
    EventEnvelope,
    OrderCancelled,
    OrderTriageCompleted,
    PaymentFailed,
    StockRejected,
    StockRejectReason,
    TriageEvent,
)

__all__ = [
    "ORDER_CANCELLED_TOPIC",
    "ORDER_TRIAGE_COMPLETED_TOPIC",
    "PAYMENT_FAILED_TOPIC",
    "STOCK_REJECTED_TOPIC",
    "TOPIC_MODELS",
    "EventEnvelope",
    "OrderCancelled",
    "OrderTriageCompleted",
    "PaymentFailed",
    "StockRejectReason",
    "StockRejected",
    "TriageEvent",
]
