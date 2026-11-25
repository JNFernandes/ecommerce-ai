"""Per-user sliding-window rate limit of chat posts (research §13).

Each subject keeps the monotonic times of its accepted hits within the window. A hit beyond
``limit`` is refused with the seconds until the oldest hit leaves the window, rounded up (the
``Retry-After`` of the 429). In process only, like the stream lock (shared limits arrive with
US-07); every ``SWEEP_EVERY`` hits, subjects with no hit left in the window are forgotten.
"""

import math
import time
from collections import deque
from collections.abc import Callable
from typing import Final

SWEEP_EVERY: Final = 1024


class SlidingWindowLimiter:
    """At most ``limit`` hits per subject in any ``window_seconds``."""

    def __init__(
        self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Allow ``limit`` hits per ``window_seconds`` on ``clock`` (seconds, monotonic)."""
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._since_sweep = 0

    def hit(self, subject: str) -> int | None:
        """Count a hit; ``None`` when allowed, else the whole seconds to wait (≥ 1)."""
        now = self._clock()
        self._since_sweep += 1
        if self._since_sweep >= SWEEP_EVERY:
            self._since_sweep = 0
            self._forget_idle(now)
        hits = self._hits.setdefault(subject, deque())
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return max(1, math.ceil(hits[0] + self._window - now))
        hits.append(now)
        return None

    def _forget_idle(self, now: float) -> None:
        """Drop subjects without a hit in the window (keeps memory bounded)."""
        for subject in [s for s, h in self._hits.items() if not h or h[-1] <= now - self._window]:
            del self._hits[subject]
