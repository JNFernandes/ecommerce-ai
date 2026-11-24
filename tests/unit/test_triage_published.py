"""A triage result becomes one contract-valid ``ai.order-triage-completed`` outbox message."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from ecommerce_ai.agents.triage.model import (
    CaseForTriage,
    ResultSource,
    SuggestedAction,
    TriageResult,
)
from ecommerce_ai.agents.triage_intake.model import TriageCause
from ecommerce_ai.messaging.contracts import ORDER_TRIAGE_COMPLETED_TOPIC, OrderTriageCompleted
from ecommerce_ai.messaging.triage_published import to_outbox_message

ORDER_ID = UUID("00000000-0000-4000-8000-000000000001")
EVENT_ID = UUID("00000000-0000-4000-8000-0000000000ee")
CASE = CaseForTriage(
    case_id=UUID("00000000-0000-4000-8000-0000000000aa"),
    order_id=ORDER_ID,
    cause=TriageCause.INSUFFICIENT_STOCK,
    evidence=(),
)


def _result(source: ResultSource = ResultSource.LLM) -> TriageResult:
    llm = source is ResultSource.LLM
    return TriageResult(
        cause=TriageCause.INSUFFICIENT_STOCK,
        explanation="Stock for product SKU-002 was short, so the order was cancelled.",
        suggested_action=SuggestedAction.RESTOCK,
        confidence=0.9,
        source=source,
        prompt_version="triage.v1",
        model_id="claude-opus-5-5" if llm else None,
        input_tokens=10,
        output_tokens=5,
        latency_ms=100,
        revisions=0,
        violations=(),
    )


def test_should_build_the_contract_payload_keyed_by_order_id_when_given_an_llm_result() -> None:
    occurred_at = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)

    message = to_outbox_message(_result(), CASE, event_id=EVENT_ID, occurred_at=occurred_at)

    assert message.topic == ORDER_TRIAGE_COMPLETED_TOPIC == "ai.order-triage-completed"
    assert message.key == str(ORDER_ID)
    assert message.event_id == EVENT_ID
    assert message.occurred_at == occurred_at
    assert message.payload == {
        "eventId": str(EVENT_ID),
        "aggregateId": str(ORDER_ID),
        "occurredAt": "2026-11-24T10:00:00Z",
        "version": 1,
        "orderId": str(ORDER_ID),
        "cause": "INSUFFICIENT_STOCK",
        "explanation": "Stock for product SKU-002 was short, so the order was cancelled.",
        "suggestedAction": "RESTOCK",
        "confidence": 0.9,
        "promptVersion": "triage.v1",
        "modelId": "claude-opus-5-5",
        "source": "LLM",
    }


def test_should_publish_a_null_model_id_when_the_result_is_a_fallback() -> None:
    message = to_outbox_message(_result(ResultSource.FALLBACK_TEMPLATE), CASE)

    assert message.payload["modelId"] is None
    assert message.payload["source"] == "FALLBACK_TEMPLATE"


def test_should_give_a_new_event_id_and_utc_time_when_none_is_given() -> None:
    first = to_outbox_message(_result(), CASE)
    second = to_outbox_message(_result(), CASE)

    assert first.event_id != second.event_id
    assert first.payload["eventId"] == str(first.event_id)
    assert first.occurred_at.utcoffset() == timedelta(0)


def test_should_convert_the_time_to_utc_when_given_another_offset() -> None:
    lisbon_summer = timezone(timedelta(hours=1))

    message = to_outbox_message(
        _result(), CASE, occurred_at=datetime(2026, 7, 1, 11, 0, tzinfo=lisbon_summer)
    )

    assert message.payload["occurredAt"] == "2026-07-01T10:00:00Z"


def test_should_read_back_the_same_event_when_the_payload_is_validated() -> None:
    message = to_outbox_message(_result(), CASE, event_id=EVENT_ID)

    event = OrderTriageCompleted.model_validate(message.payload)

    assert event.event_id == EVENT_ID
    assert event.order_key == ORDER_ID


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = dict(to_outbox_message(_result(), CASE).payload)
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "overrides",
    [
        {"modelId": None},
        {"source": "FALLBACK_TEMPLATE"},
        {"orderId": "00000000-0000-4000-8000-000000000002"},
        {"explanation": "x" * 1001},
        {"confidence": 1.5},
        {"suggestedAction": "REFUND"},
        {"version": 2},
        {"unexpected": "field"},
    ],
)
def test_should_reject_the_event_when_it_breaks_the_contract(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        OrderTriageCompleted.model_validate(_payload(**overrides))
