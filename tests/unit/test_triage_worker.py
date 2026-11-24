"""The triage worker loop: settled cases processed back to back, idle wait otherwise (US1)."""

import asyncio
from datetime import timedelta

from ecommerce_ai.agents.prompts.loader import load_prompt
from ecommerce_ai.agents.triage.gatekeeper import check
from ecommerce_ai.agents.triage.graph import TriageLimits, build_triage_graph
from ecommerce_ai.agents.triage.service import TriageService
from ecommerce_ai.agents.triage.worker import TriageWorker, wait_or_stop
from ecommerce_ai.agents.triage_intake.model import TriageStatus
from tests.support.polling import wait_until
from tests.support.stores import InMemoryTokenBudget, InMemoryTriageCaseQueue
from tests.support.triage import NOW, FakeClock, make_case

POLL_SECONDS = 0.75
SETTLE_SECONDS = 3.0


class RecordingWait:
    """Records idle waits instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, _stop: asyncio.Event, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


def _worker(
    queue: InMemoryTriageCaseQueue, wait: RecordingWait, wake: asyncio.Event | None = None
) -> TriageWorker:
    clock = FakeClock()
    graph = build_triage_graph(
        model=None,
        budget=InMemoryTokenBudget(budget=1_000_000),
        gatekeeper=check,
        prompt=load_prompt("triage", "v1"),
        limits=TriageLimits(
            max_retries=0, case_token_cap=8000, max_output_tokens=1024, backoff_base_seconds=0.01
        ),
        clock=clock,
    )
    service = TriageService(queue, graph, clock, settle_seconds=SETTLE_SECONDS)
    return TriageWorker(service, poll_interval_seconds=POLL_SECONDS, wake=wake, wait=wait)


async def _run_until_idle(worker: TriageWorker, wait: RecordingWait) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await wait_until(lambda: bool(wait.delays) or task.done())
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_should_triage_every_settled_case_before_waiting_and_wake_the_relay() -> None:
    queue, wait, wake = InMemoryTriageCaseQueue(), RecordingWait(), asyncio.Event()
    cases = [make_case() for _ in range(3)]
    for index, case in enumerate(cases):
        queue.add(case, NOW - timedelta(seconds=SETTLE_SECONDS + index))

    await _run_until_idle(_worker(queue, wait, wake), wait)

    assert all(queue.status(case.case_id) is TriageStatus.COMPLETED for case in cases)
    assert wait.delays[0] == POLL_SECONDS
    assert wake.is_set()


async def test_should_wait_the_poll_interval_without_waking_the_relay_when_idle() -> None:
    queue, wait, wake = InMemoryTriageCaseQueue(), RecordingWait(), asyncio.Event()
    unsettled = make_case()
    queue.add(unsettled, NOW)

    await _run_until_idle(_worker(queue, wait, wake), wait)

    assert queue.status(unsettled.case_id) is TriageStatus.PENDING_TRIAGE
    assert set(wait.delays) == {POLL_SECONDS}
    assert not wake.is_set()


async def test_should_triage_without_a_wake_event() -> None:
    queue, wait = InMemoryTriageCaseQueue(), RecordingWait()
    case = make_case()
    queue.add(case, NOW - timedelta(seconds=SETTLE_SECONDS))

    await _run_until_idle(_worker(queue, wait), wait)

    assert queue.status(case.case_id) is TriageStatus.COMPLETED


async def test_should_end_the_wait_when_stopped_or_after_the_delay() -> None:
    stop = asyncio.Event()
    await asyncio.wait_for(wait_or_stop(stop, 0.001), timeout=2)
    stop.set()
    await asyncio.wait_for(wait_or_stop(stop, 30), timeout=2)
