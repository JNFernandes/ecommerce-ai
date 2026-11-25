"""The real model on triage (one case per cause) and the chat assistant (``live_llm``).

Run with ``uv run pytest -m live_llm`` and ``ANTHROPIC_API_KEY``/``ANTHROPIC_MODEL`` in the
environment (read at import, before the per-test environment reset). Every triage result must
either come from the model and pass the gatekeeper, or be the cause's template; the assistant's
streamed answer must pass the chat gatekeeper or end with the safe message.
"""

import os
from uuid import uuid4

import pytest
from pydantic import JsonValue, SecretStr

from ecommerce_ai.agents.assistant.gatekeeper import SAFE_MESSAGE, ChatEvidence, check_answer
from ecommerce_ai.agents.assistant.model import FinishReason, Token
from ecommerce_ai.agents.prompts.loader import load_prompt
from ecommerce_ai.agents.safety.rules import json_leaves
from ecommerce_ai.agents.triage.gatekeeper import check
from ecommerce_ai.agents.triage.graph import TriageLimits, build_triage_graph
from ecommerce_ai.agents.triage.model import DraftOutput, ResultSource
from ecommerce_ai.agents.triage.ports import SystemClock
from ecommerce_ai.agents.triage.templates import fallback_draft
from ecommerce_ai.agents.triage_intake.model import TriageCause
from ecommerce_ai.infrastructure.llm.anthropic_chat_model import build_anthropic_chat_model
from ecommerce_ai.infrastructure.llm.anthropic_model import build_anthropic_model
from ecommerce_ai.infrastructure.settings import Settings
from tests.support.chat import FakeToolRunner, chat_graph, collect_run, envelope
from tests.support.stores import InMemoryTokenBudget
from tests.support.triage import make_case

_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_MODEL = os.environ.get("ANTHROPIC_MODEL", "")

pytestmark = [
    pytest.mark.live_llm,
    pytest.mark.skipif(not (_API_KEY and _MODEL), reason="no ANTHROPIC_API_KEY/ANTHROPIC_MODEL"),
]


@pytest.mark.parametrize("cause", list(TriageCause))
async def test_should_pass_the_gatekeeper_or_fall_back_when_the_real_model_drafts(
    cause: TriageCause,
) -> None:
    settings = Settings(anthropic_api_key=SecretStr(_API_KEY), anthropic_model=_MODEL)
    model, client = build_anthropic_model(settings)
    graph = build_triage_graph(
        model=model,
        budget=InMemoryTokenBudget(budget=settings.case_token_cap),
        gatekeeper=check,
        prompt=load_prompt("triage", "v1"),
        limits=TriageLimits(
            max_retries=settings.llm_max_retries,
            case_token_cap=settings.case_token_cap,
            max_output_tokens=settings.llm_max_output_tokens,
            backoff_base_seconds=1.0,
        ),
        clock=SystemClock(),
    )
    case = make_case(cause)
    try:
        result = await graph.run(case)
    finally:
        await client.close()

    if result.source is ResultSource.LLM:
        draft = DraftOutput(
            explanation=result.explanation,
            suggested_action=result.suggested_action,
            confidence=result.confidence,
        )
        assert check(draft, case) == []
        assert result.model_id
    else:
        assert result.explanation == fallback_draft(cause).explanation


ORDERS: JsonValue = [
    {
        "id": "5f1d2c3b-0000-4000-8000-000000000042",
        "status": "SHIPPED",
        "totalAmount": "59.90",
        "createdAt": "2026-11-20T10:00:00Z",
    }
]
"""What the scripted ``list_my_orders`` returns to the real model."""


async def test_should_stream_a_gatekept_answer_when_the_real_model_answers_the_assistant() -> None:
    settings = Settings(anthropic_api_key=SecretStr(_API_KEY), anthropic_model=_MODEL)
    _, client = build_anthropic_model(settings)
    tools = FakeToolRunner(results={"list_my_orders": envelope(ORDERS)})
    graph = chat_graph(build_anthropic_chat_model(settings, client), tools)
    try:
        events, outcome = await collect_run(graph, uuid4(), "Where is my last order?")
    finally:
        await client.close()

    assert outcome.error is None
    assert outcome.finish_reason in {FinishReason.COMPLETED, FinishReason.REPLACED}
    assert outcome.model_id
    assert any(isinstance(event, Token) for event in events)
    if outcome.finish_reason is FinishReason.COMPLETED:
        evidence = ChatEvidence(json_leaves(ORDERS), [])
        assert check_answer(outcome.text, evidence) == ()
    else:
        assert outcome.text.endswith(SAFE_MESSAGE)
