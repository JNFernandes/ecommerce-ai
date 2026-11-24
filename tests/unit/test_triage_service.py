"""``TriageService.triage_one``: one settled case → one result, COMPLETED, one log line (US1)."""

import io
import json
from datetime import timedelta

import pytest

from ecommerce_ai.agents.prompts.loader import load_prompt
from ecommerce_ai.agents.triage.gatekeeper import check
from ecommerce_ai.agents.triage.graph import TriageLimits, build_triage_graph
from ecommerce_ai.agents.triage.model import ResultSource, SuggestedAction
from ecommerce_ai.agents.triage.policy import default_action
from ecommerce_ai.agents.triage.ports import TriageModel
from ecommerce_ai.agents.triage.service import TriageService
from ecommerce_ai.agents.triage.templates import fallback_draft
from ecommerce_ai.agents.triage_intake.model import TriageCause, TriageStatus
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.messaging.triage_published import to_outbox_message
from tests.support.llm import MODEL_ID, ScriptedModel, reply
from tests.support.stores import InMemoryTriageCaseQueue
from tests.support.triage import NOW, FakeClock, make_case

SETTLE_SECONDS = 3.0
LIMITS = TriageLimits(
    max_retries=0, case_token_cap=8000, max_output_tokens=1024, backoff_base_seconds=0.01
)


def _service(
    queue: InMemoryTriageCaseQueue, model: TriageModel | None = None, clock: FakeClock | None = None
) -> TriageService:
    clock = clock or FakeClock()
    graph = build_triage_graph(
        model=model,
        gatekeeper=check,
        prompt=load_prompt("triage", "v1"),
        limits=LIMITS,
        clock=clock,
    )
    return TriageService(queue, graph, clock, settle_seconds=SETTLE_SECONDS)


def _settled() -> timedelta:
    return timedelta(seconds=SETTLE_SECONDS)


@pytest.mark.parametrize("cause", list(TriageCause))
async def test_should_complete_the_case_with_one_result_when_a_case_of_any_cause_settled(
    cause: TriageCause,
) -> None:
    queue = InMemoryTriageCaseQueue(serializer=to_outbox_message)
    case = make_case(cause)
    queue.add(case, NOW - _settled())

    assert await _service(queue).triage_one() is True

    result = queue.results[case.case_id]
    assert result.cause is cause
    assert result.suggested_action is default_action(cause)
    assert queue.status(case.case_id) is TriageStatus.COMPLETED
    [message] = queue.outbox
    assert message.key == str(case.order_id)
    assert message.payload["cause"] == cause.value


async def test_should_store_the_llm_result_when_the_draft_passes() -> None:
    queue = InMemoryTriageCaseQueue()
    case = make_case(TriageCause.PAYMENT_FAILED)
    queue.add(case, NOW - _settled())
    model = ScriptedModel([reply("The card was declined, so the order failed.")])

    await _service(queue, model).triage_one()

    result = queue.results[case.case_id]
    assert result.source is ResultSource.LLM
    assert result.model_id == MODEL_ID


async def test_should_leave_a_case_untouched_when_it_has_not_settled() -> None:
    queue = InMemoryTriageCaseQueue()
    case = make_case()
    queue.add(case, NOW - _settled() + timedelta(milliseconds=1))

    assert await _service(queue).triage_one() is False

    assert queue.status(case.case_id) is TriageStatus.PENDING_TRIAGE
    assert queue.results == {}


async def test_should_return_false_when_no_case_is_pending() -> None:
    assert await _service(InMemoryTriageCaseQueue()).triage_one() is False


async def test_should_take_the_oldest_settled_case_first() -> None:
    queue = InMemoryTriageCaseQueue()
    older, newer = make_case(), make_case()
    queue.add(newer, NOW - _settled())
    queue.add(older, NOW - _settled() - timedelta(seconds=5))

    await _service(queue).triage_one()

    assert set(queue.results) == {older.case_id}


@pytest.mark.parametrize("cause", [TriageCause.UNKNOWN, TriageCause.PAYMENT_FAILED])
async def test_should_use_the_unknown_template_without_a_call_when_the_case_has_no_evidence(
    cause: TriageCause,
) -> None:
    queue = InMemoryTriageCaseQueue()
    case = make_case(cause, with_evidence=False)
    queue.add(case, NOW - _settled())
    model = ScriptedModel()

    await _service(queue, model).triage_one()

    result = queue.results[case.case_id]
    template = fallback_draft(TriageCause.UNKNOWN)
    assert result.cause is cause
    assert result.source is ResultSource.FALLBACK_TEMPLATE
    assert result.explanation == template.explanation
    assert result.suggested_action is SuggestedAction.REVIEW
    assert model.calls == 0


async def test_should_roll_back_and_keep_the_case_pending_when_saving_fails() -> None:
    queue = InMemoryTriageCaseQueue()
    case = make_case()
    queue.add(case, NOW - _settled())
    queue.complete_failures.append(ConnectionError("db down"))

    with pytest.raises(ConnectionError):
        await _service(queue).triage_one()

    assert queue.status(case.case_id) is TriageStatus.PENDING_TRIAGE
    assert queue.rollbacks == 1


async def test_should_log_one_triage_case_line_with_ids_tokens_and_latency() -> None:
    queue = InMemoryTriageCaseQueue()
    case = make_case(TriageCause.PAYMENT_FAILED)
    queue.add(case, NOW - _settled())
    model = ScriptedModel([reply("The card was declined, so the order failed.")])
    service = _service(queue, model, FakeClock(tick=0.05))
    stream = io.StringIO()
    configure_logging("INFO", stream)

    await service.triage_one()

    [line] = [
        entry
        for entry in map(json.loads, stream.getvalue().splitlines())
        if entry["event"] == "triage_case"
    ]
    assert line["case_id"] == str(case.case_id)
    assert line["order_id"] == str(case.order_id)
    assert line["source"] == "LLM"
    assert line["revisions"] == 0
    assert line["usage"] == {"input": 300, "output": 40}
    assert line["latency_ms"] == 50
    assert line["prompt_version"] == "triage.v1"
    assert line["model_id"] == MODEL_ID
    assert line["outcome"] == "completed"
    assert "card declined" not in stream.getvalue()
    assert "The card was declined" not in stream.getvalue()
