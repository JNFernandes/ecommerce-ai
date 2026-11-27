"""The triage graph (research 1): classify → draft → gatekeeper → finalize | revise | fallback.

A LangGraph ``StateGraph`` over the frozen ``TriageState``, compiled without a checkpointer: a
run is short and re-runnable from the stored case, so a crash simply leaves the case pending.
Every path ends in exactly one ``TriageResult``; a draft is used only when the gatekeeper finds
no violation. A rejected draft, or invalid model output, is drafted again with the violations as
feedback, at most ``MAX_REVISIONS`` times; then the fixed template of the cause is used
(FR-005, FR-007, FR-008, FR-010).

Every model call, and every refused one, is stored as an ``LlmCall`` by ``LlmUsage``; a run
that ends on the template adds one ``fallback`` record with no tokens (US-08 FR-010).
"""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from typing import Final, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field

from ecommerce_ai.agents.ports import LlmCall, LlmCallKind, LlmCallOutcome, llm_outcome
from ecommerce_ai.agents.prompts.loader import Prompt
from ecommerce_ai.agents.triage.model import (
    MAX_REVISIONS,
    CaseForTriage,
    DraftOutput,
    ResultSource,
    SuggestedAction,
    TriageResult,
    Violation,
)
from ecommerce_ai.agents.triage.policy import allowed_actions, default_action
from ecommerce_ai.agents.triage.ports import (
    Clock,
    ModelError,
    ModelOutputInvalid,
    ModelReply,
    ModelUnavailable,
    PromptInput,
    TokenBudget,
    TriageModel,
)
from ecommerce_ai.agents.triage.templates import fallback_draft
from ecommerce_ai.agents.triage_intake.model import TriageCause
from ecommerce_ai.agents.usage import LlmUsage
from ecommerce_ai.infrastructure.retry import call_with_retry
from ecommerce_ai.telemetry.attributes import LATENCY_MS, LLM_OUTCOME, ORDER_ID, PROMPT_VERSION
from ecommerce_ai.telemetry.nodes import traced_node
from ecommerce_ai.telemetry.spans import LLM_CALL, llm_call_attributes
from ecommerce_ai.telemetry.tracing import Attributes, set_attributes, span

type Gatekeeper = Callable[[DraftOutput, CaseForTriage, int], list[Violation]]
"""Checks one draft attempt of a case; returns its violations (empty = usable)."""

type StateUpdate = dict[str, object]
"""Fields a node changes; LangGraph merges them into a new state."""

type Sleep = Callable[[float], Awaitable[None]]
"""Waits between retries of an unavailable model (injected in tests)."""

_CLASSIFY: Final = "classify"
_DRAFT: Final = "draft"
_GATEKEEPER: Final = "gatekeeper"
_FINALIZE: Final = "finalize"
_FALLBACK: Final = "fallback"
_MAX_ATTEMPTS: Final = 1 + MAX_REVISIONS


class TriageLimits(BaseModel):
    """LLM limits of one triage run (from settings, wired in ``main.py``)."""

    model_config = ConfigDict(frozen=True)

    max_retries: int = Field(ge=0, description="Retries of an unavailable model per attempt.")
    case_token_cap: int = Field(gt=0, description="Most tokens one case may spend.")
    max_output_tokens: int = Field(gt=0, description="Most output tokens of one call.")
    backoff_base_seconds: float = Field(gt=0, description="First retry delay, doubled after.")


class _Halted(Exception):
    """No further model call is allowed (budget or per-case cap); never retried."""


class _Spend:
    """Tokens a case has spent so far, updated by each call of one draft attempt."""

    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens


class TriageState(BaseModel):
    """State of one run; nodes return only the fields they change."""

    model_config = ConfigDict(frozen=True)

    case: CaseForTriage = Field(description="The locked case.")
    started_at: float = Field(description="Monotonic time the run started.")
    default_action: SuggestedAction | None = Field(default=None, description="Set by classify.")
    attempt: int = Field(default=0, ge=0, description="Draft attempts made so far.")
    draft: DraftOutput | None = Field(default=None, description="Latest draft, if any.")
    model_id: str | None = Field(default=None, description="Model that wrote the latest draft.")
    halted: bool = Field(default=False, description="No further draft may be asked for.")
    violations: tuple[Violation, ...] = Field(default=(), description="Every finding so far.")
    input_tokens: int = Field(default=0, ge=0, description="Input tokens over every attempt.")
    output_tokens: int = Field(default=0, ge=0, description="Output tokens over every attempt.")
    revisions: int = Field(default=0, ge=0, description="Drafts after the first one.")
    result: TriageResult | None = Field(default=None, description="Set by the last node.")

    def current_violations(self) -> tuple[Violation, ...]:
        """Violations of the latest attempt."""
        return tuple(v for v in self.violations if v.attempt == self.attempt)


def classify(state: TriageState) -> StateUpdate:
    """Keep the stored cause (FR-006) and set its default action."""
    return {"default_action": default_action(state.case.cause)}


def _after_classify(state: TriageState) -> Literal["draft", "fallback"]:
    # A case without evidence (should not exist) gets the UNKNOWN template (spec edge case).
    return _DRAFT if state.case.evidence else _FALLBACK


def _retry_or_fallback(state: TriageState) -> Literal["draft", "fallback"]:
    return _DRAFT if not state.halted and state.attempt < _MAX_ATTEMPTS else _FALLBACK


def _after_draft(state: TriageState) -> Literal["gatekeeper", "draft", "fallback"]:
    # Invalid output counts as a failed attempt (FR-007): draft again while attempts remain.
    return _GATEKEEPER if state.draft is not None else _retry_or_fallback(state)


def _after_gatekeeper(state: TriageState) -> Literal["finalize", "draft", "fallback"]:
    return _FINALIZE if not state.current_violations() else _retry_or_fallback(state)


class TriageGraph:
    """The compiled triage graph and the dependencies of its nodes."""

    def __init__(
        self,
        *,
        model: TriageModel | None,
        budget: TokenBudget,
        gatekeeper: Gatekeeper,
        prompt: Prompt,
        limits: TriageLimits,
        clock: Clock,
        sleep: Sleep = asyncio.sleep,
        usage: LlmUsage | None = None,
    ) -> None:
        """Compile the graph; ``model=None`` (not configured) sends every case to the fallback.

        ``usage`` prices and records every call (``None``: nothing is recorded).
        """
        self._model = model
        self._usage = usage or LlmUsage()
        self._budget = budget
        self._sleep = sleep
        self._gatekeeper = gatekeeper
        self._prompt = prompt
        self._limits = limits
        self._clock = clock
        self._compiled = self._compile()

    def _compile(self) -> CompiledStateGraph[TriageState, None, TriageState, TriageState]:
        graph = StateGraph(TriageState)
        graph.add_node(_CLASSIFY, traced_node(_CLASSIFY, classify))
        graph.add_node(_DRAFT, traced_node(_DRAFT, self._draft))
        graph.add_node(_GATEKEEPER, traced_node(_GATEKEEPER, self._check))
        graph.add_node(_FINALIZE, traced_node(_FINALIZE, self._finalize))
        graph.add_node(_FALLBACK, traced_node(_FALLBACK, self._fallback))
        graph.add_edge(START, _CLASSIFY)
        graph.add_conditional_edges(_CLASSIFY, _after_classify)
        graph.add_conditional_edges(_DRAFT, _after_draft)
        graph.add_conditional_edges(_GATEKEEPER, _after_gatekeeper)
        graph.add_edge(_FINALIZE, END)
        graph.add_edge(_FALLBACK, END)
        return graph.compile()

    async def run(self, case: CaseForTriage) -> TriageResult:
        """Triage ``case`` and return its one result."""
        initial = TriageState(case=case, started_at=self._clock.monotonic())
        final = TriageState.model_validate(await self._compiled.ainvoke(initial))
        if final.result is None:  # pragma: no cover - every path ends in finalize or fallback
            raise RuntimeError("the triage graph ended without a result")
        return final.result

    def _prompt_input(self, state: TriageState) -> PromptInput:
        action = state.default_action or default_action(state.case.cause)
        feedback = [v.message for v in state.current_violations()]
        user = self._prompt.render_user(
            state.case, action, allowed_actions(state.case.cause), feedback
        )
        return PromptInput(system=self._prompt.system, user=user)

    async def _draft(self, state: TriageState) -> StateUpdate:
        """Ask the model for one draft, with the latest violations as feedback.

        Each call first checks the per-case cap and reserves the case's remaining allowance from
        the daily budget, then settles the tokens it actually spent (research 4). An unavailable
        model is retried up to ``max_retries`` times with backoff. No model, a spent budget or
        cap, or a model still unavailable after the retries halts the run (fallback, FR-009);
        invalid output leaves ``draft`` unset and counts as an attempt (FR-007).
        """
        if self._model is None:
            return {"draft": None, "halted": True}
        model = self._model
        prompt_input = self._prompt_input(state)
        spend = _Spend(state.input_tokens, state.output_tokens)
        day = self._clock.now().date()
        identity: Attributes = {
            PROMPT_VERSION: self._prompt.version,
            ORDER_ID: str(state.case.order_id),
        }

        async def call(_timeout: float) -> ModelReply:
            # The adapter enforces the per-call timeout itself (LLM_TIMEOUT_SECONDS).
            reservation = self._limits.case_token_cap - spend.total
            if reservation < self._limits.max_output_tokens:
                await self._refused_call(state, identity)
                raise _Halted("the per-case token cap is reached")
            if not await self._budget.reserve(day, reservation):
                await self._refused_call(state, identity)
                raise _Halted("the daily token budget is spent")
            spend.calls += 1
            with span(LLM_CALL, identity) as llm:
                started = time.perf_counter()
                try:
                    reply = await model.draft(prompt_input)
                except ModelError as error:
                    failed = self._call(
                        state,
                        llm_outcome(error),
                        latency_ms=self._elapsed_ms(started),
                        input_tokens=error.input_tokens,
                        output_tokens=error.output_tokens,
                    )
                    set_attributes(llm, self._closing(failed))
                    spend.add(error.input_tokens, error.output_tokens)
                    await self._budget.settle(
                        day, reservation, error.input_tokens + error.output_tokens
                    )
                    await self._usage.record(failed)
                    raise
                answered = self._call(
                    state,
                    LlmCallOutcome.OK,
                    latency_ms=self._elapsed_ms(started),
                    model_id=reply.model_id,
                    input_tokens=reply.input_tokens,
                    output_tokens=reply.output_tokens,
                )
                set_attributes(llm, self._closing(answered))
                spend.add(reply.input_tokens, reply.output_tokens)
                await self._budget.settle(
                    day, reservation, reply.input_tokens + reply.output_tokens
                )
            await self._usage.record(answered)
            return reply

        try:
            reply = await call_with_retry(
                call,
                deadline=math.inf,
                max_retries=self._limits.max_retries,
                backoff_base=self._limits.backoff_base_seconds,
                attempt_timeout=math.inf,
                is_retryable=lambda error: isinstance(error, ModelUnavailable),
                sleep=self._sleep,
            )
        except (_Halted, ModelError) as error:
            attempt = state.attempt + 1 if spend.calls else state.attempt
            return {
                "attempt": attempt,
                "revisions": max(0, attempt - 1),
                "draft": None,
                "halted": not isinstance(error, ModelOutputInvalid),
                "input_tokens": spend.input_tokens,
                "output_tokens": spend.output_tokens,
            }
        attempt = state.attempt + 1
        return {
            "attempt": attempt,
            "revisions": attempt - 1,
            "draft": reply.draft,
            "model_id": reply.model_id,
            "input_tokens": spend.input_tokens,
            "output_tokens": spend.output_tokens,
        }

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        # Wall time, not the injected clock: a span's latency never changes a result.
        return max(0, round((time.perf_counter() - started) * 1000))

    def _call(
        self,
        state: TriageState,
        outcome: LlmCallOutcome,
        *,
        latency_ms: int = 0,
        model_id: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> LlmCall:
        """The usage record of one call of ``state``'s case, priced from its tokens."""
        return LlmCall(
            kind=LlmCallKind.TRIAGE,
            case_id=state.case.case_id,
            model_id=model_id,
            prompt_version=self._prompt.version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=self._usage.cost(model_id, input_tokens, output_tokens),
            latency_ms=latency_ms,
            outcome=outcome,
        )

    @staticmethod
    def _closing(call: LlmCall) -> Attributes:
        """The closing ``llm.call`` span attributes of ``call``."""
        return llm_call_attributes(
            outcome=call.outcome,
            latency_ms=call.latency_ms,
            model=call.model_id,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            cost_usd=call.estimated_cost_usd,
        )

    async def _refused_call(self, state: TriageState, identity: Attributes) -> None:
        """Trace and record a call the budget or the per-case cap refused (no request sent)."""
        with span(LLM_CALL, {**identity, LLM_OUTCOME: LlmCallOutcome.BUDGET.value, LATENCY_MS: 0}):
            pass
        await self._usage.record(self._call(state, LlmCallOutcome.BUDGET))

    def _check(self, state: TriageState) -> StateUpdate:
        """Run the gatekeeper on the latest draft and record what it found."""
        if state.draft is None:  # pragma: no cover - routed here only with a draft
            raise RuntimeError("gatekeeper reached without a draft")
        found = self._gatekeeper(state.draft, state.case, state.attempt)
        return {"violations": (*state.violations, *found)}

    def _latency_ms(self, state: TriageState) -> int:
        return max(0, round((self._clock.monotonic() - state.started_at) * 1000))

    def _result(
        self, state: TriageState, draft: DraftOutput, source: ResultSource, model_id: str | None
    ) -> TriageResult:
        return TriageResult(
            cause=state.case.cause,
            explanation=draft.explanation,
            suggested_action=draft.suggested_action,
            confidence=draft.confidence,
            source=source,
            prompt_version=self._prompt.version,
            model_id=model_id,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            latency_ms=self._latency_ms(state),
            revisions=state.revisions,
            violations=state.violations,
        )

    def _finalize(self, state: TriageState) -> StateUpdate:
        """Use the gated draft as the result (source LLM)."""
        if state.draft is None or state.model_id is None:  # pragma: no cover - see _after_draft
            raise RuntimeError("finalize reached without a draft")
        return {"result": self._result(state, state.draft, ResultSource.LLM, state.model_id)}

    async def _fallback(self, state: TriageState) -> StateUpdate:
        """Use the cause's fixed template (FR-010); tokens and violations stay recorded.

        A case without evidence gets the UNKNOWN template: nothing supports its cause's text.
        The run's one ``fallback`` usage record carries no tokens (its calls have their own).
        """
        cause = state.case.cause if state.case.evidence else TriageCause.UNKNOWN
        template = fallback_draft(cause)
        await self._usage.record(self._call(state, LlmCallOutcome.FALLBACK))
        return {"result": self._result(state, template, ResultSource.FALLBACK_TEMPLATE, None)}


def build_triage_graph(
    *,
    model: TriageModel | None,
    budget: TokenBudget,
    gatekeeper: Gatekeeper,
    prompt: Prompt,
    limits: TriageLimits,
    clock: Clock,
    sleep: Sleep = asyncio.sleep,
    usage: LlmUsage | None = None,
) -> TriageGraph:
    """Build the triage graph with its dependencies."""
    return TriageGraph(
        model=model,
        budget=budget,
        gatekeeper=gatekeeper,
        prompt=prompt,
        limits=limits,
        clock=clock,
        sleep=sleep,
        usage=usage,
    )
