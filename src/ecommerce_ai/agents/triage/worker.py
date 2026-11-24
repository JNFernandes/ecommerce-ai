"""Triage worker: a background loop that triages settled cases one after another (research 11).

While cases are settled it takes the next one immediately; when none is, it waits
``triage_poll_interval_seconds`` (or until stopped). After each committed result it sets the
``wake`` event so the outbox relay publishes without waiting for its own poll (SC-004).

A transient failure (``is_transient``, injected: the database being down) is retried after a
delay that doubles from ``triage_backoff_base_seconds`` up to ``triage_backoff_max_seconds``;
``WorkerState.last_error`` holds its class name until a case is triaged or none is due. Any other
failure is recorded and ends the task. Stopping interrupts any wait; a case in flight when the
task is cancelled is rolled back and stays pending (FR-004). Uses asyncio and ports only; the
wait is injected so tests never sleep.
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

import structlog

from ecommerce_ai.agents.triage.service import TriageService
from ecommerce_ai.infrastructure.errors import error_name

logger = structlog.get_logger(__name__)

type StopWait = Callable[[asyncio.Event, float], Awaitable[None]]
"""Waits ``delay`` seconds or until the stop event is set, whichever comes first."""


async def wait_or_stop(stop_event: asyncio.Event, delay: float) -> None:
    """Return after ``delay`` seconds, or as soon as ``stop_event`` is set."""
    with suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), delay)


def _never_transient(_error: BaseException) -> bool:
    return False


@dataclass(slots=True)
class WorkerState:
    """Live state of the triage worker, reported by ``GET /ready``."""

    model_configured: bool
    running: bool = False
    last_error: str | None = None


class TriageWorker:
    """Runs ``TriageService.triage_one`` until asked to stop."""

    def __init__(
        self,
        service: TriageService,
        *,
        poll_interval_seconds: float,
        wake: asyncio.Event | None = None,
        wait: StopWait = wait_or_stop,
        state: WorkerState | None = None,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 30.0,
        is_transient: Callable[[BaseException], bool] = _never_transient,
    ) -> None:
        """Triage with ``service``; set ``wake`` after every saved result."""
        self._service = service
        self._poll_interval = poll_interval_seconds
        self._wake = wake
        self._wait = wait
        self.state = state or WorkerState(model_configured=False)
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._is_transient = is_transient

    def _backoff_delay(self, failures: int) -> float:
        return min(self._backoff_base * 2.0 ** min(failures, 32), self._backoff_max)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Triage settled cases until ``stop_event`` is set."""
        self.state.running = True
        failures = 0
        try:
            while not stop_event.is_set():
                try:
                    triaged = await self._service.triage_one()
                except Exception as error:
                    self.state.last_error = error_name(error)
                    if not self._is_transient(error):
                        raise
                    logger.warning("triage_worker_retry", error=self.state.last_error)
                    await self._wait(stop_event, self._backoff_delay(failures))
                    failures += 1
                    continue
                failures = 0
                self.state.last_error = None
                if triaged:
                    if self._wake is not None:
                        self._wake.set()
                else:
                    await self._wait(stop_event, self._poll_interval)
        finally:
            self.state.running = False
