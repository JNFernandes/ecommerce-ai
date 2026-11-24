"""Models of the three failure events the triage consumer reads (``events.md``).

Every event shares the envelope (``eventId``, ``aggregateId``, ``occurredAt``, ``version``).
Orders events identify the order by ``aggregateId``; Payments and Inventory events carry it in
``orderId`` (FR-003), which ``order_key`` hides from callers.
"""

from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

ORDER_CANCELLED_TOPIC: Final = "orders.order-cancelled"
PAYMENT_FAILED_TOPIC: Final = "payments.payment-failed"
STOCK_REJECTED_TOPIC: Final = "inventory.stock-rejected"


class StockRejectReason(StrEnum):
    """The only reasons Inventory publishes with ``inventory.stock-rejected``."""

    INSUFFICIENT_STOCK = "insufficient stock"
    UNKNOWN_PRODUCT = "unknown product"
    UNKNOWN_PRODUCT_AND_INSUFFICIENT_STOCK = "unknown product and insufficient stock"


class EventEnvelope(BaseModel):
    """Fields every platform event carries."""

    model_config = ConfigDict(
        frozen=True, extra="ignore", alias_generator=to_camel, populate_by_name=True
    )

    event_id: UUID = Field(description="Unique id of the event; the idempotency key.")
    aggregate_id: UUID = Field(description="Id of the aggregate that raised the event.")
    occurred_at: AwareDatetime = Field(description="When it happened (timezone-aware).")
    version: Literal[1] = Field(description="Schema version; only version 1 is understood.")

    @property
    def order_key(self) -> UUID:
        """Id of the order the event is about (FR-003); each concrete event defines it."""
        raise NotImplementedError


class OrderCancelled(EventEnvelope):
    """``orders.order-cancelled``: a pending order was cancelled; ``aggregateId`` is the order."""

    customer_id: UUID = Field(description="Customer who owns the order.")
    reason: str | None = Field(
        default=None, description="Outcome that caused the cancellation; absent when requested."
    )

    @property
    def order_key(self) -> UUID:
        """The order id, which is the ``aggregateId`` of Orders events."""
        return self.aggregate_id


class PaymentFailed(EventEnvelope):
    """``payments.payment-failed``: the order's payment was refused."""

    order_id: UUID = Field(description="The order whose payment failed.")
    reason: str = Field(description="Business reason given by Payments.")

    @property
    def order_key(self) -> UUID:
        """The ``orderId`` field (``aggregateId`` is the payment id)."""
        return self.order_id


class StockRejected(EventEnvelope):
    """``inventory.stock-rejected``: at least one item could not be reserved."""

    order_id: UUID = Field(description="The order whose stock was rejected.")
    reason: StockRejectReason = Field(description="Why the reservation was rejected.")

    @property
    def order_key(self) -> UUID:
        """The ``orderId`` field (``aggregateId`` is the reservation id)."""
        return self.order_id


type TriageEvent = OrderCancelled | PaymentFailed | StockRejected
"""Any event the triage consumer accepts."""

type TriageEventModel = type[OrderCancelled] | type[PaymentFailed] | type[StockRejected]
"""Class of an event the triage consumer accepts."""

TOPIC_MODELS: Final[dict[str, TriageEventModel]] = {
    ORDER_CANCELLED_TOPIC: OrderCancelled,
    PAYMENT_FAILED_TOPIC: PaymentFailed,
    STOCK_REJECTED_TOPIC: StockRejected,
}
"""Model that validates each consumed topic (FR-002)."""
