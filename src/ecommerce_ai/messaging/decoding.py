"""Decode one consumed record into the intake's input, or say why it cannot be (FR-002, FR-009).

Bytes are read as UTF-8 JSON and validated by the topic's contract model; the order id is taken
from ``order_key`` (FR-003). A failure reason names error locations and types only, never
payload values, so it is safe to store and log.
"""

from dataclasses import dataclass
from typing import Final

from pydantic import ValidationError

from ecommerce_ai.agents.triage_intake.model import TriageEventInput
from ecommerce_ai.messaging.contracts import TOPIC_MODELS
from ecommerce_ai.messaging.kafka import ConsumedRecord

MAX_REASON_LENGTH: Final = 500


@dataclass(frozen=True, slots=True)
class DecodeFailure:
    """A record that cannot become a triage event, with a reason free of payload values."""

    reason: str


def _validation_reason(error: ValidationError) -> str:
    details = "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['type']}"
        for item in error.errors(include_url=False, include_input=False)
    )
    return f"invalid event: {details}"[:MAX_REASON_LENGTH]


def decode(record: ConsumedRecord) -> TriageEventInput | DecodeFailure:
    """Return the validated event of ``record``, or the reason it is invalid."""
    model = TOPIC_MODELS.get(record.topic)
    if model is None:
        return DecodeFailure("unsupported topic")
    if record.value is None:
        return DecodeFailure("empty payload")
    try:
        text = record.value.decode("utf-8")
    except UnicodeDecodeError:
        return DecodeFailure("payload is not valid UTF-8")
    try:
        event = model.model_validate_json(text)
    except ValidationError as error:
        return DecodeFailure(_validation_reason(error))
    return TriageEventInput(
        topic=record.topic,
        event_id=event.event_id,
        order_id=event.order_key,
        reason=None if event.reason is None else str(event.reason),
        occurred_at=event.occurred_at,
        payload=event.model_dump(mode="json", by_alias=True),
    )
