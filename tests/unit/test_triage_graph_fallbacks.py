"""The triage graph without a usable model: retries, daily budget and per-case cap (US3)."""

from datetime import date

from ecommerce_ai.agents.prompts.loader import load_prompt
from ecommerce_ai.agents.triage.gatekeeper import check
from ecommerce_ai.agents.triage.graph import TriageGraph, TriageLimits, build_triage_graph
from ecommerce_ai.agents.triage.model import ResultSource
from ecommerce_ai.agents.triage.ports import ModelOutputInvalid, ModelUnavailable
from ecommerce_ai.agents.triage.templates import fallback_draft
from ecommerce_ai.agents.triage_intake.model import TriageCause
from tests.support.llm import ScriptedModel, reply
from tests.support.stores import BudgetCall, InMemoryTokenBudget
from tests.support.triage import NOW, FakeClock, make_case

PROMPT = load_prompt("triage", "v1")
COMPLIANT = "The payment was declined because the card declined, so the order failed."
REFUND = "The order failed, and a refund was issued to the customer."
TODAY = NOW.date()


class _Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _graph(
    model: ScriptedModel | None,
    budget: InMemoryTokenBudget,
    *,
    max_retries: int = 2,
    case_token_cap: int = 8000,
    max_output_tokens: int = 1024,
    sleeps: _Sleeps | None = None,
) -> TriageGraph:
    return build_triage_graph(
        model=model,
        budget=budget,
        gatekeeper=check,
        prompt=PROMPT,
        limits=TriageLimits(
            max_retries=max_retries,
            case_token_cap=case_token_cap,
            max_output_tokens=max_output_tokens,
            backoff_base_seconds=0.5,
        ),
        clock=FakeClock(),
        sleep=sleeps or _Sleeps(),
    )


async def test_should_fall_back_without_a_call_or_reservation_when_no_model_is_set() -> None:
    budget = InMemoryTokenBudget(budget=100_000)

    result = await _graph(None, budget).run(make_case(TriageCause.PAYMENT_FAILED))

    assert result.source is ResultSource.FALLBACK_TEMPLATE
    assert result.model_id is None
    assert budget.reserves == []


async def test_should_fall_back_after_the_retries_when_the_model_stays_unavailable() -> None:
    model = ScriptedModel([ModelUnavailable("timeout") for _ in range(3)])
    budget = InMemoryTokenBudget(budget=100_000)
    sleeps = _Sleeps()

    result = await _graph(model, budget, max_retries=2, sleeps=sleeps).run(
        make_case(TriageCause.INSUFFICIENT_STOCK)
    )

    assert model.calls == 3
    assert len(sleeps.delays) == 2
    assert all(0 <= delay <= 1.0 for delay in sleeps.delays)
    assert result.source is ResultSource.FALLBACK_TEMPLATE
    assert result.explanation == fallback_draft(TriageCause.INSUFFICIENT_STOCK).explanation
    assert result.revisions == 0


async def test_should_use_the_draft_when_a_retry_succeeds() -> None:
    model = ScriptedModel([ModelUnavailable("timeout"), reply(COMPLIANT)])
    budget = InMemoryTokenBudget(budget=100_000)

    result = await _graph(model, budget).run(make_case(TriageCause.PAYMENT_FAILED))

    assert result.source is ResultSource.LLM
    assert result.revisions == 0


async def test_should_not_call_the_model_when_the_daily_budget_is_spent() -> None:
    model = ScriptedModel()
    budget = InMemoryTokenBudget(budget=100_000)
    budget.usage[TODAY] = 100_000

    result = await _graph(model, budget).run(make_case(TriageCause.PAYMENT_FAILED))

    assert model.calls == 0
    assert result.source is ResultSource.FALLBACK_TEMPLATE
    assert [call.granted for call in budget.reserves] == [False]
    assert budget.settles == []


async def test_should_use_the_model_again_on_the_next_utc_day() -> None:
    model = ScriptedModel([reply(COMPLIANT)])
    budget = InMemoryTokenBudget(budget=100_000)
    budget.usage[date(2026, 11, 23)] = 100_000

    result = await _graph(model, budget).run(make_case(TriageCause.PAYMENT_FAILED))

    assert result.source is ResultSource.LLM


async def test_should_stop_drafting_when_the_case_token_cap_would_be_passed() -> None:
    model = ScriptedModel([reply(REFUND, input_tokens=500, output_tokens=100)])
    budget = InMemoryTokenBudget(budget=100_000)

    result = await _graph(model, budget, case_token_cap=1500, max_output_tokens=1000).run(
        make_case(TriageCause.PAYMENT_FAILED)
    )

    assert model.calls == 1
    assert result.source is ResultSource.FALLBACK_TEMPLATE
    assert (result.input_tokens, result.output_tokens) == (500, 100)


async def test_should_settle_the_actual_tokens_after_every_attempt() -> None:
    model = ScriptedModel(
        [
            ModelUnavailable("timeout"),
            ModelOutputInvalid("schema", input_tokens=200, output_tokens=20),
            reply(REFUND, input_tokens=300, output_tokens=30),
            reply(COMPLIANT, input_tokens=310, output_tokens=40),
        ]
    )
    budget = InMemoryTokenBudget(budget=100_000)

    result = await _graph(model, budget, case_token_cap=8000).run(
        make_case(TriageCause.PAYMENT_FAILED)
    )

    assert result.source is ResultSource.LLM
    assert result.revisions == 2
    assert budget.reserves == [
        BudgetCall(day=TODAY, reserved=8000, granted=True),
        BudgetCall(day=TODAY, reserved=8000, granted=True),
        BudgetCall(day=TODAY, reserved=7780, granted=True),
        BudgetCall(day=TODAY, reserved=7450, granted=True),
    ]
    assert [(call.reserved, call.actual) for call in budget.settles] == [
        (8000, 0),
        (8000, 220),
        (7780, 330),
        (7450, 350),
    ]
    assert budget.usage[TODAY] == 900
