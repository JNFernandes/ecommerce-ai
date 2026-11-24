"""Models of the events in ``events.md``: three consumed failure events and one produced result.

Every event shares the envelope (``eventId``, ``aggregateId``, ``occurredAt``, ``version``).
Orders events identify the order by ``aggregateId``; Payments and Inventory events carry it in
``orderId`` (FR-003), which ``order_key`` hides from callers. ``OrderTriageCompleted`` is the
event this service publishes; its ``aggregateId`` is the order id.
"""

from enum import StrEnum
from typing import Final, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from ecommerce_ai.agents.triage.model import ResultSource, SuggestedAction
from ecommerce_ai.agents.triage_intake.model import TriageCause

ORDER_CANCELLED_TOPIC: Final = "orders.order-cancelled"
PAYMENT_FAILED_TOPIC: Final = "payments.payment-failed"
STOCK_REJECTED_TOPIC: Final = "inventory.stock-rejected"
ORDER_TRIAGE_COMPLETED_TOPIC: Final = "ai.order-triage-completed"


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


class OrderTriageCompleted(EventEnvelope):
    """``ai.order-triage-completed``: why an order failed, for people only (advisory, FR-018).

    Produced, so unknown fields are rejected. ``aggregateId`` and ``orderId`` are the order id,
    which is also the Kafka key; ``modelId`` is null exactly when ``source`` is a fallback.
    """

    model_config = ConfigDict(extra="forbid")

    order_id: UUID = Field(description="The order; equal to `aggregateId`.")
    cause: TriageCause = Field(description="Why the order failed.")
    explanation: str = Field(
        min_length=1, max_length=1000, description="Plain text for an operator; ids only."
    )
    suggested_action: SuggestedAction = Field(
        description="Advice for a human; the AI never carries it out."
    )
    confidence: float = Field(ge=0, le=1, description="From 0 to 1.")
    prompt_version: str = Field(min_length=1, description="Prompt version, e.g. `triage.v1`.")
    model_id: str | None = Field(
        description="LLM model id when `source` is `LLM`; null for `FALLBACK_TEMPLATE`."
    )
    source: ResultSource = Field(description="`LLM` or `FALLBACK_TEMPLATE`.")

    @property
    def order_key(self) -> UUID:
        """The order id, which is the ``aggregateId`` and the message key."""
        return self.aggregate_id

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        """``orderId`` equals ``aggregateId``; ``modelId`` is null iff the source is a fallback."""
        if self.order_id != self.aggregate_id:
            raise ValueError("orderId must equal aggregateId")
        if (self.model_id is None) != (self.source is ResultSource.FALLBACK_TEMPLATE):
            raise ValueError("modelId must be null exactly when source is FALLBACK_TEMPLATE")
        return self
