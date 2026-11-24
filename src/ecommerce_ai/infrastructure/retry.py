"""Deadline-bounded retries with exponential backoff and full jitter (constitution IV).

``call_with_retry`` runs one read-only attempt at a time. Each attempt receives its own timeout,
the smaller of the per-attempt timeout and the time left before the deadline, and must enforce
it. A failure is retried only when ``is_retryable`` says so, at most ``max_retries`` times, after
a random delay in ``[0, min(backoff_base * 2**n, backoff_max)]``; no retry starts if that delay
would reach the deadline. The clock, sleep and random source are injectable so tests never wait.
"""

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable
from typing import Protocol


class _Uniform(Protocol):
    """The part of ``random.Random`` the backoff needs."""

    def uniform(self, a: float, b: float) -> float:
        """Return a random float in ``[a, b]``."""
        ...


_SYSTEM_RANDOM = random.SystemRandom()


async def call_with_retry[T](
    attempt: Callable[[float], Awaitable[T]],
    *,
    deadline: float,
    max_retries: int,
    backoff_base: float,
    attempt_timeout: float,
    backoff_max: float = math.inf,
    is_retryable: Callable[[Exception], bool],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: _Uniform = _SYSTEM_RANDOM,
) -> T:
    """Return the first successful result of ``attempt``, retrying retryable failures.

    ``deadline`` is a ``clock()`` value. Raises ``TimeoutError`` when the deadline has already
    passed before the first attempt, otherwise the last attempt's error.
    """
    retries = 0
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("the call's time budget is spent")
        try:
            return await attempt(min(attempt_timeout, remaining))
        except Exception as error:
            if not is_retryable(error) or retries >= max_retries:
                raise
            delay = rng.uniform(0.0, min(backoff_base * 2**retries, backoff_max))
            if clock() + delay >= deadline:
                raise
        await sleep(delay)
        retries += 1
