"""Valid records decode into the intake's input, with the order id each contract defines."""

from uuid import uuid4

import pytest

from ecommerce_ai.agents.triage_intake.model import TriageEventInput
from ecommerce_ai.messaging.contracts import (
    ORDER_CANCELLED_TOPIC,
    PAYMENT_FAILED_TOPIC,
    STOCK_REJECTED_TOPIC,
)
from ecommerce_ai.messaging.decoding import DecodeFailure, decode
from tests.support.kafka import (
    OCCURRED_AT,
    TOPIC_PAYLOADS,
    make_record,
    order_cancelled_payload,
    payment_failed_payload,
    stock_rejected_payload,
)


def _decoded(record_value: object) -> TriageEventInput:
    assert isinstance(record_value, TriageEventInput), record_value
    return record_value


@pytest.mark.parametrize("topic", sorted(TOPIC_PAYLOADS))
def test_should_decode_a_valid_event_when_it_matches_its_topic_contract(topic: str) -> None:
    order_id, event_id = uuid4(), uuid4()
    payload = TOPIC_PAYLOADS[topic](order_id, event_id=event_id)

    event = _decoded(decode(make_record(topic, payload)))

    assert event.topic == topic
    assert event.event_id == event_id
    assert event.order_id == order_id
    assert event.occurred_at == OCCURRED_AT


def test_should_take_the_order_from_aggregate_id_when_the_event_is_a_cancellation() -> None:
    order_id = uuid4()

    event = _decoded(decode(make_record(ORDER_CANCELLED_TOPIC, order_cancelled_payload(order_id))))

    assert event.order_id == order_id
    assert event.reason is None


def test_should_take_the_order_from_order_id_when_the_event_is_a_payment_failure() -> None:
    order_id = uuid4()
    payload = payment_failed_payload(order_id, reason="card declined")

    event = _decoded(decode(make_record(PAYMENT_FAILED_TOPIC, payload)))

    assert event.order_id == order_id
    assert str(event.order_id) != payload["aggregateId"]
    assert event.reason == "card declined"


def test_should_pass_the_stock_reason_as_plain_text_when_stock_is_rejected() -> None:
    payload = stock_rejected_payload(uuid4(), reason="unknown product")

    event = _decoded(decode(make_record(STOCK_REJECTED_TOPIC, payload)))

    assert event.reason == "unknown product"
    assert type(event.reason) is str


def test_should_keep_the_validated_payload_in_camel_case_without_unknown_fields() -> None:
    order_id = uuid4()
    payload = {**payment_failed_payload(order_id), "trackingHint": "ignored"}

    event = _decoded(decode(make_record(PAYMENT_FAILED_TOPIC, payload)))

    assert event.payload == {
        "eventId": payload["eventId"],
        "aggregateId": payload["aggregateId"],
        "occurredAt": "2026-11-24T10:15:30Z",
        "version": 1,
        "orderId": str(order_id),
        "reason": "card declined",
    }


def test_should_fail_without_echoing_values_when_the_payload_is_not_json() -> None:
    result = decode(make_record(PAYMENT_FAILED_TOPIC, b"card 4111-1111 declined"))

    assert isinstance(result, DecodeFailure)
    assert "4111" not in result.reason
