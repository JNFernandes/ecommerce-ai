"""The system clock and the model errors of the triage ports."""

from datetime import timedelta

from ecommerce_ai.agents.triage.ports import (
    ModelError,
    ModelOutputInvalid,
    ModelUnavailable,
    SystemClock,
)


def test_should_return_an_aware_utc_time_when_asked_for_now() -> None:
    now = SystemClock().now()

    assert now.utcoffset() == timedelta(0)


def test_should_never_go_back_when_reading_the_monotonic_clock() -> None:
    clock = SystemClock()
    first = clock.monotonic()

    assert clock.monotonic() >= first


def test_should_carry_the_spent_tokens_when_the_output_is_invalid() -> None:
    error = ModelOutputInvalid("refusal", input_tokens=120, output_tokens=7)

    assert isinstance(error, ModelError)
    assert (error.input_tokens, error.output_tokens) == (120, 7)
    assert str(error) == "refusal"


def test_should_count_no_tokens_when_the_model_is_unavailable_without_usage() -> None:
    error = ModelUnavailable("timeout")

    assert (error.input_tokens, error.output_tokens) == (0, 0)
