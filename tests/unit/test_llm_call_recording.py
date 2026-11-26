"""One usage record per model call, with its outcome and cost (US-08 FR-010, FR-011)."""

import io
import json
from decimal import Decimal
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from ecommerce_ai.agents.assistant.graph import ChatGraph, load_assistant_prompt
from ecommerce_ai.agents.assistant.limits import ChatLimits, RunAllowance
from ecommerce_ai.agents.assistant.model import ChatErrorCode, FinishReason, RunOutcome
from ecommerce_ai.agents.ports import (
    LlmCall,
    LlmCallKind,
    LlmCallOutcome,
    ModelOutputInvalid,
    ModelTimeout,
    ModelUnavailable,
)
from ecommerce_ai.agents.prompts.loader import load_prompt
from ecommerce_ai.agents.triage.gatekeeper import check
from ecommerce_ai.agents.triage.graph import TriageGraph, TriageLimits, build_triage_graph
from ecommerce_ai.agents.triage.model import ResultSource
from ecommerce_ai.agents.triage_intake.model import TriageCause
from ecommerce_ai.agents.usage import LlmUsage
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.pricing import ModelPrices
from ecommerce_ai.telemetry import attributes
from tests.support.chat import FakeToolRunner
from tests.support.llm import (
    MODEL_ID,
    ScriptedChatModel,
    ScriptedModel,
    TurnStep,
    reply,
    text_turn,
)
from tests.support.stores import InMemoryLlmCallRecorder, InMemoryTokenBudget
from tests.support.tracing import recording_spans
from tests.support.triage import NOW, FakeClock, make_case

PRICES = ModelPrices.model_validate(
    {"version": 1, "models": {MODEL_ID: {"input_per_mtok": "3", "output_per_mtok": "15"}}}
)
COMPLIANT = "The payment was declined because the card declined, so the order failed."
UNLIMITED = 10_000_000


async def _no_sleep(_delay: float) -> None:
    return None


def _triage(
    model: ScriptedModel | None,
    recorder: InMemoryLlmCallRecorder,
    budget: int = UNLIMITED,
) -> TriageGraph:
    return build_triage_graph(
        model=model,
        budget=InMemoryTokenBudget(budget=budget),
        gatekeeper=check,
        prompt=load_prompt("triage", "v1"),
        limits=TriageLimits(
            max_retries=0, case_token_cap=8000, max_output_tokens=1024, backoff_base_seconds=0.1
        ),
        clock=FakeClock(),
        sleep=_no_sleep,
        usage=LlmUsage(recorder, PRICES),
    )


def _outcomes(recorder: InMemoryLlmCallRecorder) -> list[LlmCallOutcome]:
    return [call.outcome for call in recorder.calls]


async def test_should_record_one_priced_ok_call_when_triage_drafts_once() -> None:
    recorder = InMemoryLlmCallRecorder()
    model = ScriptedModel([reply(COMPLIANT, input_tokens=1000, output_tokens=200)])
    case = make_case(TriageCause.PAYMENT_FAILED)

    result = await _triage(model, recorder).run(case)

    assert result.source is ResultSource.LLM
    [call] = recorder.calls
    assert call.kind is LlmCallKind.TRIAGE
    assert (call.case_id, call.conversation_id, call.request_id) == (case.case_id, None, None)
    assert (call.model_id, call.prompt_version) == (MODEL_ID, "triage.v1")
    assert (call.input_tokens, call.output_tokens) == (1000, 200)
    assert call.estimated_cost_usd == Decimal("0.006000")
    assert call.outcome is LlmCallOutcome.OK


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ModelTimeout("timed out", input_tokens=0), LlmCallOutcome.TIMEOUT),
        (ModelUnavailable("unavailable"), LlmCallOutcome.ERROR),
    ],
)
async def test_should_record_the_failure_and_one_fallback_when_the_model_fails(
    error: ModelUnavailable, outcome: LlmCallOutcome
) -> None:
    recorder = InMemoryLlmCallRecorder()

    result = await _triage(ScriptedModel([error]), recorder).run(
        make_case(TriageCause.PAYMENT_FAILED)
    )

    assert result.source is ResultSource.FALLBACK_TEMPLATE
    assert _outcomes(recorder) == [outcome, LlmCallOutcome.FALLBACK]
    failed, fallback = recorder.calls
    assert failed.model_id is None
    assert failed.estimated_cost_usd is None
    assert (fallback.input_tokens, fallback.output_tokens, fallback.latency_ms) == (0, 0, 0)


async def test_should_record_an_error_with_its_tokens_when_the_output_is_invalid() -> None:
    recorder = InMemoryLlmCallRecorder()
    invalid = [ModelOutputInvalid("bad", input_tokens=50, output_tokens=5) for _ in range(3)]

    await _triage(ScriptedModel(invalid), recorder).run(make_case(TriageCause.PAYMENT_FAILED))

    assert _outcomes(recorder) == [LlmCallOutcome.ERROR] * 3 + [LlmCallOutcome.FALLBACK]
    assert recorder.calls[0].input_tokens == 50


async def test_should_record_a_budget_call_and_a_fallback_when_the_budget_is_spent() -> None:
    recorder = InMemoryLlmCallRecorder()
    model = ScriptedModel([])

    await _triage(model, recorder, budget=0).run(make_case(TriageCause.PAYMENT_FAILED))

    assert model.calls == 0
    assert _outcomes(recorder) == [LlmCallOutcome.BUDGET, LlmCallOutcome.FALLBACK]
    assert recorder.calls[0].input_tokens == 0


async def test_should_record_only_a_fallback_when_no_model_is_configured() -> None:
    recorder = InMemoryLlmCallRecorder()

    await _triage(None, recorder).run(make_case(TriageCause.PAYMENT_FAILED))

    assert _outcomes(recorder) == [LlmCallOutcome.FALLBACK]


async def test_should_keep_the_result_and_log_the_class_when_the_recorder_fails() -> None:
    recorder = InMemoryLlmCallRecorder()
    recorder.failures.append(ConnectionError("db down at secret-host"))
    stream = io.StringIO()
    configure_logging("INFO", stream)

    result = await _triage(ScriptedModel([reply(COMPLIANT)]), recorder).run(
        make_case(TriageCause.PAYMENT_FAILED)
    )

    assert result.source is ResultSource.LLM
    assert recorder.calls == []
    [line] = [
        json.loads(raw)
        for raw in stream.getvalue().splitlines()
        if json.loads(raw)["event"] == "llm_call_record_failed"
    ]
    assert line["error_type"] == "ConnectionError"
    assert "secret-host" not in stream.getvalue()


# Chat --------------------------------------------------------------------------------------------


def _chat(model: ScriptedChatModel, recorder: InMemoryLlmCallRecorder) -> ChatGraph:
    return ChatGraph(
        model=model,
        tools=FakeToolRunner(),
        prompt=load_assistant_prompt(),
        checkpointer=InMemorySaver(),
        max_retries=0,
        sleep=_no_sleep,
        usage=LlmUsage(recorder, PRICES),
    )


async def _run(
    graph: ChatGraph, allowance: RunAllowance | None = None, request_id: str | None = "req-1"
) -> RunOutcome:
    outcome: RunOutcome | None = None
    async for item in graph.run(uuid4(), "Where is my order?", allowance, request_id=request_id):
        if isinstance(item, RunOutcome):
            outcome = item
    assert outcome is not None
    return outcome


async def test_should_record_a_priced_chat_call_with_its_conversation_and_request() -> None:
    recorder = InMemoryLlmCallRecorder()
    conversation_id = uuid4()
    graph = _chat(
        ScriptedChatModel([text_turn("Hello.", input_tokens=2000, output_tokens=100)]), recorder
    )

    async for _item in graph.run(conversation_id, "Hi", request_id="req-7"):
        pass

    [call] = recorder.calls
    assert call.kind is LlmCallKind.CHAT
    assert (call.conversation_id, call.case_id, call.request_id) == (
        conversation_id,
        None,
        "req-7",
    )
    assert (call.model_id, call.prompt_version) == (MODEL_ID, graph.prompt_version)
    assert (call.input_tokens, call.output_tokens) == (2000, 100)
    assert call.estimated_cost_usd == Decimal("0.007500")
    assert call.outcome is LlmCallOutcome.OK


@pytest.mark.parametrize(
    ("step", "outcome"),
    [
        (ModelTimeout("timed out"), LlmCallOutcome.TIMEOUT),
        (ModelUnavailable("unavailable", input_tokens=10), LlmCallOutcome.ERROR),
    ],
)
async def test_should_record_the_chat_failure_when_the_model_fails(
    step: TurnStep, outcome: LlmCallOutcome
) -> None:
    recorder = InMemoryLlmCallRecorder()

    result = await _run(_chat(ScriptedChatModel([[step]]), recorder))

    assert result.error is ChatErrorCode.LLM_UNAVAILABLE
    assert _outcomes(recorder) == [outcome]


async def test_should_record_a_budget_call_when_the_allowance_refuses() -> None:
    recorder = InMemoryLlmCallRecorder()
    model = ScriptedChatModel([])
    allowance = RunAllowance(InMemoryTokenBudget(budget=0), NOW.date(), ChatLimits())

    result = await _run(_chat(model, recorder), allowance)

    assert result.error is ChatErrorCode.BUDGET_EXCEEDED
    assert model.calls == 0
    [call] = recorder.calls
    assert call.outcome is LlmCallOutcome.BUDGET
    assert (call.model_id, call.input_tokens, call.estimated_cost_usd) == (None, 0, None)


async def test_should_answer_unchanged_when_the_chat_recorder_fails() -> None:
    recorder = InMemoryLlmCallRecorder()
    recorder.failures.append(RuntimeError("boom"))

    result = await _run(_chat(ScriptedChatModel([text_turn("Hello.")]), recorder))

    assert result.finish_reason is FinishReason.COMPLETED
    assert recorder.calls == []


async def test_should_record_nothing_when_usage_has_no_recorder() -> None:
    usage = LlmUsage()

    await usage.record(
        LlmCall(
            kind=LlmCallKind.CHAT,
            conversation_id=uuid4(),
            prompt_version="v4",
            outcome=LlmCallOutcome.OK,
        )
    )

    assert usage.cost(MODEL_ID, 1, 1) is None


@pytest.mark.parametrize(
    ("kind", "subjects"),
    [
        (LlmCallKind.CHAT, {}),
        (LlmCallKind.CHAT, {"case_id": uuid4()}),
        (LlmCallKind.TRIAGE, {"conversation_id": uuid4()}),
        (LlmCallKind.TRIAGE, {"case_id": uuid4(), "conversation_id": uuid4()}),
    ],
)
def test_should_reject_a_record_when_its_subject_does_not_match_its_kind(
    kind: LlmCallKind, subjects: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="names a conversation"):
        LlmCall.model_validate({"kind": kind, "prompt_version": "v1", "outcome": "ok", **subjects})


# Spans -------------------------------------------------------------------------------------------


async def test_should_put_the_estimated_cost_on_the_llm_call_span_when_priced() -> None:
    recorder = InMemoryLlmCallRecorder()
    with recording_spans() as spans:
        await _triage(
            ScriptedModel([reply(COMPLIANT, input_tokens=1000, output_tokens=200)]), recorder
        ).run(make_case(TriageCause.PAYMENT_FAILED))
        await _run(
            _chat(ScriptedChatModel([text_turn("Hi.", input_tokens=0, output_tokens=0)]), recorder)
        )
        costs = [
            (s.attributes or {}).get(attributes.COST_USD) for s in spans.spans_by_name("llm.call")
        ]

    assert costs == [0.006, 0.0]
