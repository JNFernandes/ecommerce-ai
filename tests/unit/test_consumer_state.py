"""Consumer state transitions reported by readiness."""

from ecommerce_ai.messaging.consumer_state import ConsumerState


class KafkaConnectionError(Exception):
    """Stands in for the aiokafka error of the same name."""


def test_should_start_disconnected_without_an_error() -> None:
    state = ConsumerState()

    assert (state.connected, state.caught_up, state.last_error) == (False, False, None)


def test_should_keep_only_the_error_class_name_when_the_consumer_fails() -> None:
    state = ConsumerState(connected=True, caught_up=True)

    state.mark_failed(KafkaConnectionError("Unable to bootstrap from kafka:9092 user=admin"))

    assert (state.connected, state.caught_up) == (False, False)
    assert state.last_error == "KafkaConnectionError"


def test_should_clear_the_last_error_when_the_consumer_reconnects() -> None:
    state = ConsumerState()
    state.mark_failed(TimeoutError())

    state.mark_connected()

    assert (state.connected, state.last_error) == (True, None)


def test_should_report_caught_up_only_when_no_record_is_left() -> None:
    state = ConsumerState(connected=True)

    state.update_lag(3)
    assert state.caught_up is False

    state.update_lag(0)
    assert state.caught_up is True


def test_should_be_disconnected_and_keep_its_last_error_when_stopped() -> None:
    state = ConsumerState(connected=True, caught_up=True, last_error="TimeoutError")

    state.mark_stopped()

    assert (state.connected, state.caught_up, state.last_error) == (False, False, "TimeoutError")
