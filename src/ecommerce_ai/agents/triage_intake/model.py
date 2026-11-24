"""Value types of the triage intake: causes, case status, outcomes and the event input."""

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue


class TriageCause(StrEnum):
    """Why an order failed; same values as the ``cause`` enum of ``events.md``."""

    PAYMENT_FAILED = "PAYMENT_FAILED"
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    UNKNOWN_PRODUCT = "UNKNOWN_PRODUCT"
    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"
    UNKNOWN = "UNKNOWN"


class TriageStatus(StrEnum):
    """Lifecycle of a triage case: PENDING_TRIAGE until its result is saved, then COMPLETED."""

    PENDING_TRIAGE = "PENDING_TRIAGE"
    COMPLETED = "COMPLETED"


class IntakeOutcome(StrEnum):
    """What happened to one consumed message (logged as ``outcome``)."""

    PROCESSED = "processed"
    DUPLICATE = "duplicate"
    DEAD_LETTERED = "dead_lettered"


class TriageEventInput(BaseModel):
    """A validated failure event, as the intake service receives it from messaging."""

    model_config = ConfigDict(frozen=True)

    topic: str = Field(min_length=1, description="Kafka topic the event came from.")
    event_id: UUID = Field(description="The event's `eventId`; the idempotency key.")
    order_id: UUID = Field(description="The order the event is about.")
    reason: str | None = Field(description="The event's reason, if any.")
    occurred_at: AwareDatetime = Field(description="When the event happened.")
    payload: dict[str, JsonValue] = Field(description="The validated event, JSON-ready.")


class LockedCase(BaseModel):
    """A triage case locked for the current transaction."""

    model_config = ConfigDict(frozen=True)

    case_id: UUID = Field(description="Id of the case.")
    cause: TriageCause = Field(description="Cause currently stored on the case.")
