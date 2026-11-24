"""Turn a triage result into the ``ai.order-triage-completed`` outbox message (FR-016).

``main.py`` hands ``to_outbox_message`` to the case queue as its serializer, so agents never
import messaging and storage never knows the contract (research 6).
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from ecommerce_ai.agents.triage.model import CaseForTriage, TriageResult
from ecommerce_ai.infrastructure.outbox import OutboxMessage
from ecommerce_ai.messaging.contracts.events import (
    ORDER_TRIAGE_COMPLETED_TOPIC,
    OrderTriageCompleted,
)


def to_outbox_message(
    result: TriageResult,
    case: CaseForTriage,
    *,
    event_id: UUID | None = None,
    occurred_at: datetime | None = None,
) -> OutboxMessage:
    """Build the validated event for ``result``, keyed by the order id.

    ``event_id`` defaults to a new UUID and ``occurred_at`` to now (UTC); both are stored with
    the message and never change on a republish.
    """
    event = OrderTriageCompleted(
        event_id=event_id or uuid4(),
        aggregate_id=case.order_id,
        occurred_at=(occurred_at or datetime.now(UTC)).astimezone(UTC),
        version=1,
        order_id=case.order_id,
        cause=result.cause,
        explanation=result.explanation,
        suggested_action=result.suggested_action,
        confidence=result.confidence,
        prompt_version=result.prompt_version,
        model_id=result.model_id,
        source=result.source,
    )
    return OutboxMessage(
        event_id=event.event_id,
        topic=ORDER_TRIAGE_COMPLETED_TOPIC,
        key=str(case.order_id),
        payload=event.model_dump(mode="json", by_alias=True),
        occurred_at=event.occurred_at,
    )
