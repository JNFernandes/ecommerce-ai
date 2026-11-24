"""Triage worker: a background loop that triages settled cases one after another (research 11).

While cases are settled it takes the next one immediately; when none is, it waits
``triage_poll_interval_seconds`` (or until stopped). After each committed result it sets the
``wake`` event so the outbox relay publishes without waiting for its own poll (SC-004). Uses
asyncio and ports only; the wait is injected so tests never sleep.
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from ecommerce_ai.agents.triage.service import TriageService

type StopWait = Callable[[asyncio.Event, float], Awaitable[None]]
"""Waits ``delay`` seconds or until the stop event is set, whichever comes first."""


async def wait_or_stop(stop_event: asyncio.Event, delay: float) -> None:
    """Return after ``delay`` seconds, or as soon as ``stop_event`` is set."""
    with suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), delay)


class TriageWorker:
    """Runs ``TriageService.triage_one`` until asked to stop."""

    def __init__(
        self,
        service: TriageService,
        *,
        poll_interval_seconds: float,
        wake: asyncio.Event | None = None,
        wait: StopWait = wait_or_stop,
    ) -> None:
        """Triage with ``service``; set ``wake`` after every saved result."""
        self._service = service
        self._poll_interval = poll_interval_seconds
        self._wake = wake
        self._wait = wait

    async def run(self, stop_event: asyncio.Event) -> None:
        """Triage settled cases until ``stop_event`` is set."""
        while not stop_event.is_set():
            if await self._service.triage_one():
                if self._wake is not None:
                    self._wake.set()
            else:
                await self._wait(stop_event, self._poll_interval)
