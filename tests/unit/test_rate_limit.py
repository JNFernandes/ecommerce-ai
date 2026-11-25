"""``SlidingWindowLimiter``: per-user sliding window with an injected clock (US5, research §13)."""

from ecommerce_ai.api.rate_limit import SWEEP_EVERY, SlidingWindowLimiter


class FakeClock:
    """A monotonic clock moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_should_allow_up_to_the_limit_then_refuse_when_the_window_is_full() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(3, 60.0, clock)

    assert [limiter.hit("u") for _ in range(3)] == [None, None, None]
    assert limiter.hit("u") == 60


def test_should_give_retry_after_until_the_oldest_hit_leaves_when_refused() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(2, 60.0, clock)
    limiter.hit("u")
    clock.now += 10.5
    limiter.hit("u")
    clock.now += 20.0

    assert limiter.hit("u") == 30  # ceil(60 - 30.5)
    clock.now += 29.4
    assert limiter.hit("u") == 1  # 0.1 s left, rounded up


def test_should_allow_again_when_the_oldest_hit_left_the_window() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(1, 60.0, clock)
    limiter.hit("u")
    clock.now += 60.0

    assert limiter.hit("u") is None
    assert limiter.hit("u") == 60


def test_should_not_count_refused_hits_when_the_window_is_full() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(1, 60.0, clock)
    limiter.hit("u")
    for _ in range(5):
        limiter.hit("u")
    clock.now += 60.0

    assert limiter.hit("u") is None


def test_should_isolate_subjects_when_one_is_limited() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(1, 60.0, clock)
    limiter.hit("alice")

    assert limiter.hit("alice") == 60
    assert limiter.hit("bob") is None


def test_should_forget_idle_subjects_when_sweeping() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(1, 60.0, clock)
    limiter.hit("idle")
    clock.now += 61.0
    for index in range(SWEEP_EVERY - 1):
        limiter.hit(f"user-{index}")

    assert "idle" not in limiter._hits
    assert limiter.hit("idle") is None
