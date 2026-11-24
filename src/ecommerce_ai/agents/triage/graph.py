"""The triage graph (research 1): classify → draft → gatekeeper → finalize | fallback.

A LangGraph ``StateGraph`` over the frozen ``TriageState``, compiled without a checkpointer: a
run is short and re-runnable from the stored case, so a crash simply leaves the case pending.
Every path ends in exactly one ``TriageResult``; a draft is used only when the gatekeeper finds
no violation, otherwise the fixed template of the cause is (FR-008, FR-010).
"""

from collections.abc import Callable
from typing import Final, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field

from ecommerce_ai.agents.prompts.loader import Prompt
from ecommerce_ai.agents.triage.model import (
    CaseForTriage,
    DraftOutput,
    ResultSource,
    SuggestedAction,
    TriageResult,
    Violation,
)
from ecommerce_ai.agents.triage.policy import allowed_actions, default_action
from ecommerce_ai.agents.triage.ports import Clock, ModelError, PromptInput, TriageModel
from ecommerce_ai.agents.triage.templates import fallback_draft
from ecommerce_ai.agents.triage_intake.model import TriageCause

type Gatekeeper = Callable[[DraftOutput, CaseForTriage, int], list[Violation]]
"""Checks one draft attempt of a case; returns its violations (empty = usable)."""

type StateUpdate = dict[str, object]
"""Fields a node changes; LangGraph merges them into a new state."""

_CLASSIFY: Final = "classify"
_DRAFT: Final = "draft"
_GATEKEEPER: Final = "gatekeeper"
_FINALIZE: Final = "finalize"
_FALLBACK: Final = "fallback"


class TriageLimits(BaseModel):
    """LLM limits of one triage run (from settings, wired in ``main.py``)."""

    model_config = ConfigDict(frozen=True)

    max_retries: int = Field(ge=0, description="Retries of an unavailable model per attempt.")
    case_token_cap: int = Field(gt=0, description="Most tokens one case may spend.")
    max_output_tokens: int = Field(gt=0, description="Most output tokens of one call.")
    backoff_base_seconds: float = Field(gt=0, description="First retry delay, doubled after.")


class TriageState(BaseModel):
    """State of one run; nodes return only the fields they change."""

    model_config = ConfigDict(frozen=True)

    case: CaseForTriage = Field(description="The locked case.")
    started_at: float = Field(description="Monotonic time the run started.")
    default_action: SuggestedAction | None = Field(default=None, description="Set by classify.")
    attempt: int = Field(default=0, ge=0, description="Draft attempts made so far.")
    draft: DraftOutput | None = Field(default=None, description="Latest draft, if any.")
    model_id: str | None = Field(default=None, description="Model that wrote the latest draft.")
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


def _after_draft(state: TriageState) -> Literal["gatekeeper", "fallback"]:
    return _GATEKEEPER if state.draft is not None else _FALLBACK


def _after_gatekeeper(state: TriageState) -> Literal["finalize", "fallback"]:
    return _FINALIZE if not state.current_violations() else _FALLBACK


class TriageGraph:
    """The compiled triage graph and the dependencies of its nodes."""

    def __init__(
        self,
        *,
        model: TriageModel | None,
        gatekeeper: Gatekeeper,
        prompt: Prompt,
        limits: TriageLimits,
        clock: Clock,
    ) -> None:
        """Compile the graph; ``model=None`` (not configured) sends every case to the fallback."""
        self._model = model
        self._gatekeeper = gatekeeper
        self._prompt = prompt
        self._limits = limits
        self._clock = clock
        self._compiled = self._compile()

    def _compile(self) -> CompiledStateGraph[TriageState, None, TriageState, TriageState]:
        graph = StateGraph(TriageState)
        graph.add_node(_CLASSIFY, classify)
        graph.add_node(_DRAFT, self._draft)
        graph.add_node(_GATEKEEPER, self._check)
        graph.add_node(_FINALIZE, self._finalize)
        graph.add_node(_FALLBACK, self._fallback)
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
        """Ask the model for one draft; no model or a failed call leaves ``draft`` unset."""
        if self._model is None:
            return {"draft": None}
        attempt = state.attempt + 1
        try:
            reply = await self._model.draft(self._prompt_input(state))
        except ModelError as error:
            return {
                "attempt": attempt,
                "draft": None,
                "input_tokens": state.input_tokens + error.input_tokens,
                "output_tokens": state.output_tokens + error.output_tokens,
            }
        return {
            "attempt": attempt,
            "draft": reply.draft,
            "model_id": reply.model_id,
            "input_tokens": state.input_tokens + reply.input_tokens,
            "output_tokens": state.output_tokens + reply.output_tokens,
        }

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

    def _fallback(self, state: TriageState) -> StateUpdate:
        """Use the cause's fixed template (FR-010); tokens and violations stay recorded.

        A case without evidence gets the UNKNOWN template: nothing supports its cause's text.
        """
        cause = state.case.cause if state.case.evidence else TriageCause.UNKNOWN
        template = fallback_draft(cause)
        return {"result": self._result(state, template, ResultSource.FALLBACK_TEMPLATE, None)}


def build_triage_graph(
    *,
    model: TriageModel | None,
    gatekeeper: Gatekeeper,
    prompt: Prompt,
    limits: TriageLimits,
    clock: Clock,
) -> TriageGraph:
    """Build the triage graph with its dependencies."""
    return TriageGraph(
        model=model, gatekeeper=gatekeeper, prompt=prompt, limits=limits, clock=clock
    )
