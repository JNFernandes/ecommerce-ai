"""Guard events and ports (US-07 research §10): content-free, frozen, ordered severities."""

import pytest
from pydantic import ValidationError

from ecommerce_ai.agents.guardrails.events import (
    GuardAction,
    GuardEvent,
    GuardSeverity,
    GuardStage,
    RateWindow,
)
from ecommerce_ai.infrastructure.rate_limit import SlidingWindowLimiter


def _event() -> GuardEvent:
    return GuardEvent(
        stage=GuardStage.INPUT,
        rule="instruction_override",
        action=GuardAction.BLOCKED,
        severity=GuardSeverity.HIGH,
    )


def test_should_use_the_stored_values_when_enums_are_serialised() -> None:
    assert [s.value for s in GuardStage] == ["input", "tool", "output"]
    assert [a.value for a in GuardAction] == ["blocked", "replaced", "sanitized", "cooldown"]
    assert [s.value for s in GuardSeverity] == ["low", "medium", "high"]


def test_should_order_severities_when_ranked() -> None:
    assert GuardSeverity.LOW.rank < GuardSeverity.MEDIUM.rank < GuardSeverity.HIGH.rank


def test_should_refuse_changes_when_an_event_is_built() -> None:
    event = _event()

    with pytest.raises(ValidationError):
        event.rule = "other"  # type: ignore[misc]  # frozen model: assignment must fail


def test_should_refuse_extra_fields_when_an_event_carries_content() -> None:
    with pytest.raises(ValidationError):
        GuardEvent.model_validate({**_event().model_dump(), "text": "secret"})


@pytest.mark.parametrize("rule", ["", "x" * 65])
def test_should_refuse_a_rule_name_when_it_is_empty_or_too_long(rule: str) -> None:
    with pytest.raises(ValidationError):
        GuardEvent.model_validate({**_event().model_dump(), "rule": rule})


def test_should_dump_only_the_four_fields_when_serialised() -> None:
    assert _event().model_dump(mode="json") == {
        "stage": "input",
        "rule": "instruction_override",
        "action": "blocked",
        "severity": "high",
    }


def test_should_satisfy_the_rate_window_port_when_the_limiter_is_used() -> None:
    window: RateWindow = SlidingWindowLimiter(1, 60.0, lambda: 0.0)

    assert window.hit("u") is True
    assert window.hit("u") is False
    assert window.retry_after("u") == 60
