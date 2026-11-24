"""The triage worker loop: cases back to back, idle wait, backoff and bounded stop (US1, US5)."""

import asyncio
import time
from datetime import timedelta

import pytest

from ecommerce_ai.agents.prompts.loader import load_prompt
from ecommerce_ai.agents.triage.gatekeeper import check
from ecommerce_ai.agents.triage.graph import TriageLimits, build_triage_graph
from ecommerce_ai.agents.triage.ports import ModelReply, PromptInput
from ecommerce_ai.agents.triage.service import TriageService
from ecommerce_ai.agents.triage.worker import StopWait, TriageWorker, WorkerState, wait_or_stop
from ecommerce_ai.agents.triage_intake.model import TriageStatus
from ecommerce_ai.main import stop_task
from tests.support.llm import ScriptedModel
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


class TransientError(ConnectionRefusedError):
    """A database outage, as the injected predicate sees it."""


def _is_transient(error: BaseException) -> bool:
    return isinstance(error, TransientError)


def _resilient_worker(
    queue: InMemoryTriageCaseQueue,
    wait: StopWait,
    state: WorkerState,
    model: ScriptedModel | None = None,
) -> TriageWorker:
    clock = FakeClock()
    graph = build_triage_graph(
        model=model,
        budget=InMemoryTokenBudget(budget=1_000_000),
        gatekeeper=check,
        prompt=load_prompt("triage", "v1"),
        limits=TriageLimits(
            max_retries=0, case_token_cap=8000, max_output_tokens=1024, backoff_base_seconds=0.01
        ),
        clock=clock,
    )
    service = TriageService(queue, graph, clock, settle_seconds=SETTLE_SECONDS)
    return TriageWorker(
        service,
        poll_interval_seconds=POLL_SECONDS,
        wait=wait,
        state=state,
        backoff_base_seconds=BACKOFF_BASE,
        backoff_max_seconds=BACKOFF_MAX,
        is_transient=_is_transient,
    )


BACKOFF_BASE = 1.0
BACKOFF_MAX = 3.0


async def test_should_back_off_with_growing_bounded_delays_when_the_database_is_down() -> None:
    queue, state = InMemoryTriageCaseQueue(), WorkerState(model_configured=False)
    queue.failures.extend(TransientError("db down") for _ in range(4))
    case = make_case()
    queue.add(case, NOW - timedelta(seconds=SETTLE_SECONDS))
    wait = RecordingWait()
    seen: list[str | None] = []

    async def recording(stop: asyncio.Event, delay: float) -> None:
        seen.append(state.last_error)
        await wait(stop, delay)

    worker = _resilient_worker(queue, recording, state)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await wait_until(lambda: queue.status(case.case_id) is TriageStatus.COMPLETED)
    await wait_until(lambda: POLL_SECONDS in wait.delays)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert wait.delays[:4] == [1.0, 2.0, 3.0, 3.0]
    assert seen[:4] == ["TransientError"] * 4
    assert state.last_error is None
    assert not state.running


async def test_should_report_running_while_the_loop_runs() -> None:
    queue, state = InMemoryTriageCaseQueue(), WorkerState(model_configured=True)
    wait = RecordingWait()
    running: list[bool] = []

    async def recording(stop: asyncio.Event, delay: float) -> None:
        running.append(state.running)
        await wait(stop, delay)

    stop = asyncio.Event()
    task = asyncio.create_task(_resilient_worker(queue, recording, state).run(stop))
    await wait_until(lambda: bool(wait.delays))
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert running[0] is True
    assert state.model_configured
    assert not state.running


async def test_should_record_the_error_and_stop_when_the_failure_is_not_transient() -> None:
    queue, state = InMemoryTriageCaseQueue(), WorkerState(model_configured=False)
    queue.failures.append(RuntimeError("bug"))

    with pytest.raises(RuntimeError):
        await _resilient_worker(queue, RecordingWait(), state).run(asyncio.Event())

    assert state.last_error == "RuntimeError"
    assert not state.running


async def test_should_end_the_backoff_when_stopped() -> None:
    queue, state = InMemoryTriageCaseQueue(), WorkerState(model_configured=False)
    queue.failures.append(TransientError("db down"))
    stop = asyncio.Event()

    async def stopping(_stop: asyncio.Event, _delay: float) -> None:
        stop.set()

    await asyncio.wait_for(_resilient_worker(queue, stopping, state).run(stop), timeout=2)

    assert not state.running


class BlockingModel:
    """A model that never answers until cancelled."""

    def __init__(self) -> None:
        self.called = asyncio.Event()

    async def draft(self, prompt_input: PromptInput) -> ModelReply:
        self.called.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


async def test_should_abandon_the_in_flight_case_and_stop_within_the_timeout() -> None:
    queue, state = InMemoryTriageCaseQueue(), WorkerState(model_configured=True)
    case = make_case()
    queue.add(case, NOW - timedelta(seconds=SETTLE_SECONDS))
    model = BlockingModel()
    clock = FakeClock()
    graph = build_triage_graph(
        model=model,
        budget=InMemoryTokenBudget(budget=1_000_000),
        gatekeeper=check,
        prompt=load_prompt("triage", "v1"),
        limits=TriageLimits(
            max_retries=0, case_token_cap=8000, max_output_tokens=1024, backoff_base_seconds=0.01
        ),
        clock=clock,
    )
    worker = TriageWorker(
        TriageService(queue, graph, clock, settle_seconds=SETTLE_SECONDS),
        poll_interval_seconds=POLL_SECONDS,
        state=state,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop), name="triage-worker")
    await asyncio.wait_for(model.called.wait(), timeout=2)

    started = time.monotonic()
    await stop_task(task, stop, timeout=0.05)

    assert time.monotonic() - started < 1
    assert task.cancelled()
    assert queue.status(case.case_id) is TriageStatus.PENDING_TRIAGE
    assert not state.running
