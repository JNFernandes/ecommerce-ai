"""Per-user quotas and cooldown (US-07 US5, research §9): ``UserQuotas`` and the user budget."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ecommerce_ai.agents.assistant.limits import (
    ActiveStreams,
    ChatLimits,
    RunAllowance,
    StreamActive,
    UserBudget,
)
from ecommerce_ai.agents.assistant.model import ChatErrorCode, StreamError, Usage
from ecommerce_ai.agents.guardrails.events import (
    GuardAction,
    GuardEvent,
    GuardSeverity,
    GuardStage,
)
from ecommerce_ai.agents.guardrails.quotas import (
    COOLDOWN_EVENT,
    QuotaExceeded,
    QuotaLimits,
    QuotaReason,
    QuotaScope,
    QuotaVerdict,
    UserQuotas,
    seconds_to_midnight,
)
from ecommerce_ai.infrastructure.rate_limit import SlidingWindowLimiter
from tests.support.stores import (
    InMemoryGuardrailEventStore,
    InMemoryTokenBudget,
    InMemoryUserUsageStore,
)
from tests.support.triage import FakeClock

NOW = datetime(2026, 11, 25, 23, 50, tzinfo=UTC)
USER = "user-1"


def _block(severity: GuardSeverity = GuardSeverity.HIGH) -> GuardEvent:
    return GuardEvent(
        stage=GuardStage.INPUT, rule="override", action=GuardAction.BLOCKED, severity=severity
    )


def _quotas(
    clock: FakeClock,
    events: InMemoryGuardrailEventStore | None = None,
    usage: InMemoryUserUsageStore | None = None,
    per_minute: int = 10,
    limits: QuotaLimits | None = None,
) -> UserQuotas:
    window = SlidingWindowLimiter(per_minute, 60.0, clock.monotonic)
    return UserQuotas(window, usage, events, clock, limits)


def test_should_carry_reason_retry_after_and_user_scope_when_a_verdict_is_made() -> None:
    verdict = QuotaVerdict(QuotaReason.DAY, 10)

    assert QuotaExceeded(verdict).verdict.scope is QuotaScope.USER
    assert str(QuotaExceeded(verdict)) == "day"


def test_should_count_whole_seconds_to_the_next_utc_midnight_when_asked() -> None:
    assert seconds_to_midnight(NOW) == 600
    assert seconds_to_midnight(datetime(2026, 11, 25, 23, 59, 59, 500_000, tzinfo=UTC)) == 1


async def test_should_refuse_the_day_once_the_limit_is_passed_when_messages_add_up() -> None:
    clock = FakeClock(NOW)
    quotas = _quotas(clock, usage=InMemoryUserUsageStore(), limits=QuotaLimits(messages_per_day=2))

    await quotas.before_message(USER)
    await quotas.before_message(USER)
    with pytest.raises(QuotaExceeded) as refused:
        await quotas.before_message(USER)

    assert refused.value.verdict == QuotaVerdict(QuotaReason.DAY, 600)


async def test_should_refuse_with_the_seconds_left_when_the_minute_window_is_full() -> None:
    clock = FakeClock(NOW)
    quotas = _quotas(clock, per_minute=1)

    await quotas.before_request(USER)
    clock.advance(20)
    with pytest.raises(QuotaExceeded) as refused:
        await quotas.before_request(USER)

    assert refused.value.verdict.reason is QuotaReason.MINUTE
    assert refused.value.verdict.retry_after == 40


async def test_should_start_a_cooldown_only_for_medium_or_high_input_blocks_at_the_threshold() -> (
    None
):
    clock = FakeClock(NOW)
    events = InMemoryGuardrailEventStore(clock.now)
    quotas = _quotas(clock, events, limits=QuotaLimits(violation_threshold=2))
    low = _block(GuardSeverity.LOW)
    replaced = GuardEvent(
        stage=GuardStage.OUTPUT,
        rule="canary",
        action=GuardAction.REPLACED,
        severity=GuardSeverity.HIGH,
    )

    await events.append(USER, None, None, [low, low, replaced])
    assert await quotas.after_run(USER, [low, low, replaced]) is None
    await events.append(USER, None, None, [_block(GuardSeverity.MEDIUM)])
    assert await quotas.after_run(USER, [_block(GuardSeverity.MEDIUM)]) is None
    await events.append(USER, None, None, [_block()])

    assert await quotas.after_run(USER, [_block()]) == COOLDOWN_EVENT


async def test_should_count_only_blocks_inside_the_violation_window_when_checking() -> None:
    clock = FakeClock(NOW)
    events = InMemoryGuardrailEventStore(clock.now)
    limits = QuotaLimits(violation_threshold=2, violation_window=timedelta(seconds=60))
    quotas = _quotas(clock, events, limits=limits)

    await events.append(USER, None, None, [_block()])
    clock.advance(61)
    await events.append(USER, None, None, [_block()])

    assert await quotas.after_run(USER, [_block()]) is None


async def test_should_refuse_while_the_newest_cooldown_is_younger_than_the_cooldown() -> None:
    clock = FakeClock(NOW)
    events = InMemoryGuardrailEventStore(clock.now)
    quotas = _quotas(clock, events, limits=QuotaLimits(cooldown=timedelta(seconds=600)))
    await events.append(USER, None, None, [COOLDOWN_EVENT])
    clock.advance(599.5)

    with pytest.raises(QuotaExceeded) as refused:
        await quotas.before_request(USER)
    clock.advance(0.5)
    await quotas.before_request(USER)

    assert refused.value.verdict == QuotaVerdict(QuotaReason.COOLDOWN, 1)
    assert await quotas.cooldown("user-2") is None


async def test_should_not_start_a_second_cooldown_when_one_is_active() -> None:
    clock = FakeClock(NOW)
    events = InMemoryGuardrailEventStore(clock.now)
    quotas = _quotas(clock, events, limits=QuotaLimits(violation_threshold=1))
    await events.append(USER, None, None, [_block(), COOLDOWN_EVENT])

    assert await quotas.after_run(USER, [_block()]) is None


async def test_should_check_cooldown_before_the_minute_window_without_counting_a_hit() -> None:
    clock = FakeClock(NOW)
    events = InMemoryGuardrailEventStore(clock.now)
    quotas = _quotas(clock, events, per_minute=1)
    await events.append(USER, None, None, [COOLDOWN_EVENT])

    with pytest.raises(QuotaExceeded) as refused:
        await quotas.before_request(USER)
    clock.advance(600)
    await quotas.before_request(USER)

    assert refused.value.verdict.reason is QuotaReason.COOLDOWN


async def test_should_turn_day_and_cooldown_off_when_no_store_is_given() -> None:
    quotas = _quotas(FakeClock(NOW), limits=QuotaLimits(messages_per_day=1))

    for _ in range(3):
        await quotas.before_message(USER)

    assert await quotas.after_run(USER, [_block()]) is None


async def test_should_reserve_the_user_budget_first_and_settle_both_when_the_run_ends() -> None:
    usage = InMemoryUserUsageStore()
    budget = InMemoryTokenBudget(10**6)
    limits = ChatLimits(request_token_cap=1000, max_output_tokens=100)
    allowance = RunAllowance(budget, NOW.date(), limits, UserBudget(usage, USER, 5000))

    assert await allowance.admit()
    allowance.spend(Usage(input_tokens=30, output_tokens=20))
    await allowance.settle()

    assert usage.of(USER, NOW.date()).tokens == 50
    assert budget.usage[NOW.date()] == 50


async def test_should_refuse_with_user_scope_when_the_user_day_budget_is_spent() -> None:
    usage = InMemoryUserUsageStore()
    budget = InMemoryTokenBudget(10**6)
    allowance = RunAllowance(
        budget, NOW.date(), ChatLimits(request_token_cap=1000), UserBudget(usage, USER, 999)
    )

    assert not await allowance.admit()
    await allowance.settle()

    assert allowance.denied_scope is QuotaScope.USER
    assert budget.reserves == []


async def test_should_release_the_user_reservation_with_global_scope_when_the_global_refuses() -> (
    None
):
    usage = InMemoryUserUsageStore()
    allowance = RunAllowance(
        InMemoryTokenBudget(0), NOW.date(), ChatLimits(), UserBudget(usage, USER, 25_000)
    )

    assert not await allowance.admit()

    assert allowance.denied_scope is QuotaScope.GLOBAL
    assert usage.of(USER, NOW.date()).tokens == 0


async def test_should_release_the_user_reservation_when_the_global_budget_fails() -> None:
    usage = InMemoryUserUsageStore()
    budget = InMemoryTokenBudget(10**6)
    budget.failures.append(RuntimeError("db down"))
    allowance = RunAllowance(budget, NOW.date(), ChatLimits(), UserBudget(usage, USER, 25_000))

    with pytest.raises(RuntimeError):
        await allowance.admit()

    assert usage.of(USER, NOW.date()).tokens == 0


async def test_should_refuse_with_user_scope_when_the_request_cap_is_reached() -> None:
    allowance = RunAllowance(
        InMemoryTokenBudget(10**6), NOW.date(), ChatLimits(request_token_cap=100)
    )

    assert not await allowance.admit()
    assert allowance.denied_scope is QuotaScope.USER


def test_should_hold_one_stream_per_user_across_conversations_when_acquired() -> None:
    streams = ActiveStreams()
    first, second = uuid4(), uuid4()

    streams.acquire(first, USER)
    with pytest.raises(StreamActive):
        streams.acquire(second, USER)
    streams.acquire(second, "user-2")
    streams.release(first)
    streams.acquire(uuid4(), USER)


def test_should_carry_scope_only_on_limit_errors_when_the_error_event_is_built() -> None:
    budget = StreamError.for_code(ChatErrorCode.BUDGET_EXCEEDED, QuotaScope.GLOBAL)
    internal = StreamError.for_code(ChatErrorCode.INTERNAL, QuotaScope.USER)

    assert budget.data()["scope"] == "global"
    assert "scope" not in internal.data()
