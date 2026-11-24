"""Upstream reads retry only retryable failures, with jittered backoff inside one deadline."""

import math
from dataclasses import dataclass, field
from time import monotonic

import pytest

from ecommerce_ai.infrastructure.retry import call_with_retry


class FakeUpstreamError(Exception):
    """Stands in for the Orders client's error, with its retryable flag."""

    def __init__(self, *, retryable: bool) -> None:
        super().__init__("upstream failed")
        self.retryable = retryable


def _is_retryable(error: Exception) -> bool:
    return isinstance(error, FakeUpstreamError) and error.retryable


@dataclass
class FakeTime:
    """Manual clock; ``sleep`` advances it and records each delay."""

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


@dataclass
class FakeRandom:
    """Returns ``fraction`` of the jitter range and records each range."""

    fraction: float = 1.0
    ranges: list[tuple[float, float]] = field(default_factory=list)

    def uniform(self, a: float, b: float) -> float:
        self.ranges.append((a, b))
        return a + (b - a) * self.fraction


@dataclass
class ScriptedCall:
    """An attempt that fails with the scripted errors, then returns ``"ok"``."""

    time: FakeTime
    errors: list[Exception]
    duration: float = 0.0
    timeouts: list[float] = field(default_factory=list)

    async def __call__(self, timeout: float) -> str:
        self.timeouts.append(timeout)
        self.time.now += self.duration
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


async def _run(
    call: ScriptedCall,
    time: FakeTime,
    *,
    deadline: float = 5.0,
    max_retries: int = 2,
    attempt_timeout: float = 5.0,
    rng: FakeRandom | None = None,
    backoff_max: float = math.inf,
) -> str:
    return await call_with_retry(
        call,
        deadline=deadline,
        max_retries=max_retries,
        backoff_base=0.2,
        attempt_timeout=attempt_timeout,
        backoff_max=backoff_max,
        is_retryable=_is_retryable,
        clock=time.clock,
        sleep=time.sleep,
        rng=rng or FakeRandom(),
    )


async def test_should_return_at_once_when_the_first_attempt_succeeds() -> None:
    time = FakeTime()
    call = ScriptedCall(time, errors=[])

    assert await _run(call, time) == "ok"
    assert call.timeouts == [5.0]
    assert time.sleeps == []


async def test_should_retry_with_doubling_backoff_when_failures_are_retryable() -> None:
    time = FakeTime()
    call = ScriptedCall(time, errors=[FakeUpstreamError(retryable=True)] * 2)

    assert await _run(call, time) == "ok"
    assert len(call.timeouts) == 3
    assert time.sleeps == pytest.approx([0.2, 0.4])


async def test_should_draw_full_jitter_between_zero_and_the_backoff_cap() -> None:
    time = FakeTime()
    rng = FakeRandom(fraction=0.5)
    call = ScriptedCall(time, errors=[FakeUpstreamError(retryable=True)] * 2)

    await _run(call, time, rng=rng)

    assert [low for low, _ in rng.ranges] == [0.0, 0.0]
    assert [high for _, high in rng.ranges] == pytest.approx([0.2, 0.4])
    assert time.sleeps == pytest.approx([0.1, 0.2])


async def test_should_cap_the_backoff_when_a_maximum_is_given() -> None:
    time = FakeTime()
    call = ScriptedCall(time, errors=[FakeUpstreamError(retryable=True)] * 3)

    await _run(call, time, max_retries=3, backoff_max=0.5)

    assert time.sleeps == pytest.approx([0.2, 0.4, 0.5])


async def test_should_raise_the_last_error_after_one_plus_max_retries_attempts() -> None:
    time = FakeTime()
    errors: list[Exception] = [FakeUpstreamError(retryable=True) for _ in range(5)]
    last = errors[2]
    call = ScriptedCall(time, errors=errors)

    with pytest.raises(FakeUpstreamError) as raised:
        await _run(call, time)

    assert raised.value is last
    assert len(call.timeouts) == 3


async def test_should_make_a_single_attempt_when_max_retries_is_zero() -> None:
    time = FakeTime()
    call = ScriptedCall(time, errors=[FakeUpstreamError(retryable=True)])

    with pytest.raises(FakeUpstreamError):
        await _run(call, time, max_retries=0)

    assert len(call.timeouts) == 1


async def test_should_raise_at_once_when_the_error_is_not_retryable() -> None:
    time = FakeTime()
    call = ScriptedCall(time, errors=[FakeUpstreamError(retryable=False)])

    with pytest.raises(FakeUpstreamError):
        await _run(call, time)

    assert len(call.timeouts) == 1
    assert time.sleeps == []


async def test_should_raise_at_once_when_the_error_is_unexpected() -> None:
    time = FakeTime()
    call = ScriptedCall(time, errors=[KeyError("bug")])

    with pytest.raises(KeyError):
        await _run(call, time)

    assert len(call.timeouts) == 1


async def test_should_cap_each_attempt_timeout_by_the_remaining_budget() -> None:
    time = FakeTime()
    call = ScriptedCall(time, errors=[FakeUpstreamError(retryable=True)] * 2, duration=0.3)

    await _run(call, time, deadline=2.0, attempt_timeout=1.5)

    # attempt 1 at t=0; attempt 2 at t=0.5 (0.3 + 0.2); attempt 3 at t=1.2 (0.8 + 0.4)
    assert call.timeouts == pytest.approx([1.5, 1.5, 0.8])


async def test_should_not_retry_when_the_backoff_would_pass_the_deadline() -> None:
    time = FakeTime()
    call = ScriptedCall(time, errors=[FakeUpstreamError(retryable=True)] * 2, duration=0.2)

    with pytest.raises(FakeUpstreamError):
        await _run(call, time, deadline=0.3)

    assert len(call.timeouts) == 1
    assert time.sleeps == []


async def test_should_raise_timeout_without_calling_when_the_deadline_has_passed() -> None:
    time = FakeTime(now=10.0)
    call = ScriptedCall(time, errors=[])

    with pytest.raises(TimeoutError):
        await _run(call, time, deadline=10.0)

    assert call.timeouts == []


async def test_should_use_the_real_clock_sleep_and_system_random_by_default() -> None:
    attempts: list[float] = []

    async def succeed(timeout: float) -> int:
        attempts.append(timeout)
        return 1

    result = await call_with_retry(
        succeed,
        deadline=monotonic() + 1.0,
        max_retries=1,
        backoff_base=0.01,
        attempt_timeout=0.5,
        is_retryable=_is_retryable,
    )

    assert result == 1
    assert attempts == [0.5]
