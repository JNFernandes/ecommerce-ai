"""Value types of the transactional outbox (``outbox_events``), shared by storage and messaging.

An outbox message is saved in the same transaction as the result it announces, then published
by the outbox relay; its ``event_id``, key and payload never change between attempts (FR-017).
"""

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue


class OutboxStatus(StrEnum):
    """Lifecycle of one outbox event."""

    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    EXHAUSTED = "EXHAUSTED"


class OutboxMessage(BaseModel):
    """One event to save in the outbox and publish later, unchanged on every attempt."""

    model_config = ConfigDict(frozen=True)

    event_id: UUID = Field(description="The event's `eventId`; also stored with the result.")
    topic: str = Field(min_length=1, description="Kafka topic to publish to.")
    key: str = Field(min_length=1, description="Kafka message key (UTF-8 encoded on send).")
    payload: dict[str, JsonValue] = Field(description="The full event as JSON-ready values.")
    occurred_at: AwareDatetime = Field(description="The event's `occurredAt`.")
