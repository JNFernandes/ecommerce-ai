"""The triage graph: classify, draft, gatekeeper, then finalize or fallback (US1)."""

import pytest

from ecommerce_ai.agents.prompts.loader import load_prompt
from ecommerce_ai.agents.triage.gatekeeper import check
from ecommerce_ai.agents.triage.graph import (
    TriageGraph,
    TriageLimits,
    TriageState,
    build_triage_graph,
    classify,
)
from ecommerce_ai.agents.triage.model import (
    GateRule,
    ResultSource,
    SuggestedAction,
)
from ecommerce_ai.agents.triage.policy import default_action
from ecommerce_ai.agents.triage.ports import ModelOutputInvalid, ModelUnavailable, TriageModel
from ecommerce_ai.agents.triage.templates import fallback_draft
from ecommerce_ai.agents.triage_intake.model import TriageCause
from tests.support.llm import MODEL_ID, ScriptedModel, reply
from tests.support.triage import FakeClock, make_case

PROMPT = load_prompt("triage", "v1")
LIMITS = TriageLimits(
    max_retries=0,
    case_token_cap=8000,
    max_output_tokens=1024,
    backoff_base_seconds=0.01,
)
COMPLIANT = "The payment was declined because the card declined, so the order failed."
REFUND = "The order failed, and a refund was issued to the customer."


def _graph(model: TriageModel | None, clock: FakeClock | None = None) -> TriageGraph:
    return build_triage_graph(
        model=model,
        gatekeeper=check,
        prompt=PROMPT,
        limits=LIMITS,
        clock=clock or FakeClock(),
    )


@pytest.mark.parametrize("cause", list(TriageCause))
def test_should_keep_the_cause_and_set_the_default_action_when_classifying(
    cause: TriageCause,
) -> None:
    update = classify(TriageState(case=make_case(cause), started_at=0.0))

    assert update == {"default_action": default_action(cause)}


async def test_should_finalize_an_llm_result_when_the_draft_passes_the_gatekeeper() -> None:
    model = ScriptedModel([reply(COMPLIANT, input_tokens=300, output_tokens=40)])
    case = make_case(TriageCause.PAYMENT_FAILED)

    result = await _graph(model, FakeClock(tick=0.25)).run(case)

    assert result.source is ResultSource.LLM
    assert result.cause is TriageCause.PAYMENT_FAILED
    assert result.explanation == COMPLIANT
    assert result.suggested_action is SuggestedAction.CONTACT_CUSTOMER
    assert result.confidence == 0.8
    assert result.prompt_version == "triage.v1"
    assert result.model_id == MODEL_ID
    assert (result.input_tokens, result.output_tokens) == (300, 40)
    assert result.latency_ms == 250
    assert result.revisions == 0
    assert result.violations == ()


async def test_should_send_the_prompt_rendered_for_the_case_when_drafting() -> None:
    model = ScriptedModel([reply(COMPLIANT)])
    case = make_case(TriageCause.PAYMENT_FAILED)

    await _graph(model).run(case)

    [prompt] = model.prompts
    assert prompt.system == PROMPT.system
    assert prompt.user == PROMPT.render_user(
        case,
        SuggestedAction.CONTACT_CUSTOMER,
        {SuggestedAction.CONTACT_CUSTOMER, SuggestedAction.REVIEW},
        (),
    )


async def test_should_fall_back_to_the_template_when_the_gatekeeper_rejects_the_draft() -> None:
    model = ScriptedModel([reply(REFUND, input_tokens=310, output_tokens=30)])
    case = make_case(TriageCause.PAYMENT_FAILED)

    result = await _graph(model).run(case)

    template = fallback_draft(TriageCause.PAYMENT_FAILED)
    assert result.source is ResultSource.FALLBACK_TEMPLATE
    assert result.model_id is None
    assert result.explanation == template.explanation
    assert result.suggested_action is template.suggested_action
    assert result.confidence == template.confidence
    assert result.prompt_version == "triage.v1"
    assert (result.input_tokens, result.output_tokens) == (310, 30)
    assert result.revisions == 0
    assert [(v.rule, v.attempt) for v in result.violations] == [(GateRule.PROMISE, 1)]


async def test_should_fall_back_without_a_call_when_no_model_is_configured() -> None:
    result = await _graph(None).run(make_case(TriageCause.UNKNOWN))

    assert result.source is ResultSource.FALLBACK_TEMPLATE
    assert result.suggested_action is SuggestedAction.REVIEW
    assert (result.input_tokens, result.output_tokens) == (0, 0)


@pytest.mark.parametrize(
    "error",
    [ModelUnavailable("timeout"), ModelOutputInvalid("refusal", input_tokens=5, output_tokens=2)],
)
async def test_should_fall_back_when_the_model_fails(
    error: ModelUnavailable | ModelOutputInvalid,
) -> None:
    model = ScriptedModel([error])

    result = await _graph(model).run(make_case(TriageCause.INSUFFICIENT_STOCK))

    assert result.source is ResultSource.FALLBACK_TEMPLATE
    assert result.suggested_action is SuggestedAction.RESTOCK
    assert (result.input_tokens, result.output_tokens) == (
        error.input_tokens,
        error.output_tokens,
    )
