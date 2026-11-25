"""The chat graph (FR-010, research §3, §4): guard_input → agent ⇄ tools → gatekeeper → finalize.

A LangGraph ``StateGraph`` over ``ChatState``, checkpointed per conversation (thread id =
conversation id), so a later message continues the transcript, also after a restart (FR-014).
The transcript is a list of plain JSON dicts dumped from the frozen transcript items and
re-validated on load (``guard_input`` and every model request). Each run resets the per-run fields
(rounds, pending calls, segments, outcome, usage, model id, tool stats) and appends one
``UserTurn``.

The nodes publish ``Token``, ``ToolCall`` and ``ToolResult`` events through LangGraph's custom
stream writer; ``ChatGraph.run`` yields them in order and ends with one ``RunOutcome``. Answer
text is released one segment at a time (``Segmenter``), each checked by ``check_segment`` before
its ``Token`` (FR-016): a violating segment is never sent, the model stream stops being read, the
safe message is sent instead and the run ends ``replaced``. The ``gatekeeper`` node checks the
whole answer once more (``check_answer``) and replaces an empty one. A turn asking for tools after
``max_tool_rounds`` rounds runs none and ends ``tool_limit`` (FR-011). Whenever the customer gets
a fixed message, the transcript stores exactly what was sent, so unsafe text never reaches the
model again. The model is offered only the registry's tools (FR-011); tool results reach it as
delimited data (``tool_result_text``, FR-013).

Limits (US5): each model call is first admitted by the run's ``RunAllowance`` (daily budget and
per-message cap, else ``BUDGET_EXCEEDED``). A call failing with ``ModelUnavailable`` before any
segment was sent is retried (``call_with_retry``); once text was sent, it ends
``LLM_UNAVAILABLE``. After a cancelled run, ``record_interrupted`` puts what the customer saw into
the transcript and closes calls left without a result, so the next request stays valid.

Approvals (US-06, research §1, §2, §5): the ``tools`` node never runs a mutating call. It checks a
``cancel_order`` call (``approvals.py``: valid id, nothing else pending, a fresh ``get_order`` read
showing the caller's order ``PENDING``) and either answers a refusal result or puts a
``PendingApproval`` in the state and routes to ``approval``. That node starts with
``interrupt()``, so the run pauses there with no side effect; ``resume`` continues it with the
decision. Only then, with the arguments stored in the state, the order is re-read and cancelled
once (``ToolRunner.run_approved``); the node emits ``approval.resolved`` and ``tool.result``,
answers the paused call and hands over to the model, which explains the outcome. When the model
cannot answer after a decision, a fixed per-outcome text is the answer (ADR 0003).
``close_approval`` ends a pause that can no longer be decided (expired, withdrawn, orphaned).
"""

import asyncio
import math
import operator
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Annotated, Final, Literal, TypedDict, cast
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import JsonValue

from ecommerce_ai.agents.assistant.approvals import (
    APPROVAL_ACTIONS,
    READ_TOOL,
    TARGET_ARGUMENT,
    Refusal,
    approval_status,
    outcome_after_reread,
    outcome_message,
    outcome_of_cancel,
    outcome_payload,
    outcome_summary,
    parse_target,
    read_order,
    refusal_payload,
    refusal_summary,
    summary_of,
)
from ecommerce_ai.agents.assistant.gatekeeper import (
    NO_ANSWER_MESSAGE,
    SAFE_MESSAGE,
    TOOL_LIMIT_MESSAGE,
    ChatEvidence,
    check_answer,
    check_segment,
)
from ecommerce_ai.agents.assistant.limits import ProposalQuota, RunAllowance
from ecommerce_ai.agents.assistant.model import (
    AnswerReplaced,
    ApprovalExecution,
    ApprovalOutcome,
    ApprovalResolved,
    AssistantText,
    ChatErrorCode,
    Decision,
    FinishReason,
    PendingApproval,
    RunOutcome,
    ThinkingItem,
    Token,
    ToolCall,
    ToolCallItem,
    ToolResult,
    ToolResultItem,
    ToolStat,
    TranscriptItem,
    Usage,
    UserTurn,
    dump_transcript,
    load_transcript,
)
from ecommerce_ai.agents.assistant.ports import (
    ChatModel,
    ChatModelRequest,
    Checkpointer,
    ModelTurnEnd,
    TextDelta,
    ToolOutcome,
    ToolRunner,
    ToolUseRequest,
)
from ecommerce_ai.agents.assistant.segmenter import Segmenter
from ecommerce_ai.agents.assistant.summaries import summarize
from ecommerce_ai.agents.guardrails import Guardrails
from ecommerce_ai.agents.guardrails.events import GuardEvent
from ecommerce_ai.agents.ports import Clock, ModelError, ModelUnavailable, SystemClock
from ecommerce_ai.agents.prompts.loader import SystemPrompt, load_system_prompt
from ecommerce_ai.infrastructure.retry import call_with_retry

type GraphEvent = (
    Token
    | ToolCall
    | ToolResult
    | ApprovalResolved
    | ApprovalExecution
    | AnswerReplaced
    | GuardEvent
)
"""An event a node publishes while the graph runs.

``ApprovalExecution`` and ``GuardEvent`` are never streamed to the client: the service records
them (a guard event as soon as it comes, so it is audited even when the client goes away).
"""

_GRAPH_EVENTS: Final = (
    Token,
    ToolCall,
    ToolResult,
    ApprovalResolved,
    ApprovalExecution,
    AnswerReplaced,
    GuardEvent,
)

type StateUpdate = dict[str, object]
"""Fields a node changes; LangGraph merges them into the state."""

_GUARD: Final = "guard_input"
_AGENT: Final = "agent"
_TOOLS: Final = "tools"
_APPROVAL: Final = "approval"
_GATEKEEPER: Final = "gatekeeper"
_FINALIZE: Final = "finalize"
_THINKING_BLOCKS: Final = frozenset({"thinking", "redacted_thinking"})
DEFAULT_MAX_TOOL_ROUNDS: Final = 5
DEFAULT_APPROVAL_TTL_SECONDS: Final = 600
"""Seconds a proposal stays approvable (``APPROVAL_TTL_SECONDS``)."""


_MODEL_FAILURES: Final = frozenset(
    {ChatErrorCode.LLM_UNAVAILABLE.value, ChatErrorCode.BUDGET_EXCEEDED.value}
)


class ApprovalNotPaused(LookupError):
    """``resume`` found no pause on that approval in the conversation's state."""


ASSISTANT_PROMPT_VERSION: Final = "v3"
"""The active assistant prompt: v3 calls ``cancel_order`` directly for a named or single clear
order (no confirmation in the chat); ``assistant.v1`` and ``v2`` stay unchanged."""


def load_assistant_prompt() -> SystemPrompt:
    """The active assistant system prompt, ``assistant.<ASSISTANT_PROMPT_VERSION>``."""
    return load_system_prompt("assistant", ASSISTANT_PROMPT_VERSION)


"""Tool-call rounds a run may use (``CHAT_MAX_TOOL_ROUNDS``)."""
DEFAULT_MAX_RETRIES: Final = 2
"""Retries of a model call that failed before sending anything (``LLM_MAX_RETRIES``)."""
RETRY_BACKOFF_SECONDS: Final = 0.5
"""First retry delay of a model call; doubles per retry, with full jitter."""
_CANCELLED_RESULT: Final[dict[str, JsonValue]] = {
    "error": "cancelled",
    "message": "The call was cancelled before it finished.",
}

type Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RunContext:
    """Per-run dependencies of the nodes; never checkpointed."""

    allowance: RunAllowance | None = None
    quota: ProposalQuota | None = None
    """The caller's proposals left this hour (``None``: no limit)."""


@dataclass(slots=True)
class _Turn:
    """What one model call produced and what of it was sent."""

    sent: list[str] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    calls: list[ToolCallItem] = field(default_factory=list)
    end: ModelTurnEnd | None = None
    replaced: bool = False
    violations: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Checked:
    """A checked ``cancel_order`` call: a proposal, or a refusal with its reason."""

    proposal: PendingApproval | None = None
    refusal: Refusal | None = None
    status: str | None = None
    stats: Sequence[dict[str, JsonValue]] = ()
    unauthenticated: bool = False
    order_id: UUID | None = None

    @classmethod
    def refused(
        cls,
        refusal: Refusal,
        status: str | None = None,
        stats: Sequence[dict[str, JsonValue]] = (),
        *,
        unauthenticated: bool = False,
        order_id: UUID | None = None,
    ) -> "_Checked":
        """A refused call (FR-003, FR-005): no approval."""
        return cls(
            refusal=refusal,
            status=status,
            stats=stats,
            unauthenticated=unauthenticated,
            order_id=order_id,
        )

    def refusal_result(self, call: ToolCallItem) -> tuple[ToolResultItem, str]:
        """The refused call's result for the model and its fixed ``tool.result`` summary."""
        refusal = self.refusal or Refusal.UNAVAILABLE
        result = ToolResultItem(
            call_id=call.call_id,
            name=call.name,
            ok=False,
            payload=refusal_payload(refusal, self.status, self.order_id),
        )
        return result, refusal_summary(refusal)


class _BudgetSpent(Exception):
    """The run's allowance admits no further model call."""


class _TurnFailed(Exception):
    """A model call failed; carries what it had sent."""

    def __init__(self, turn: _Turn, error: ModelError) -> None:
        super().__init__("the model call failed")
        self.turn = turn
        self.error = error


def _retryable(error: Exception) -> bool:
    """Only an unavailable model that sent nothing yet is retried."""
    return (
        isinstance(error, _TurnFailed)
        and isinstance(error.error, ModelUnavailable)
        and not error.turn.sent
    )


class ChatState(TypedDict):
    """The graph state of one conversation; only ``transcript`` survives across runs."""

    transcript: Annotated[list[dict[str, JsonValue]], operator.add]
    rounds: int
    pending_tool_calls: list[dict[str, JsonValue]]
    answer_segments: list[str]
    outcome: str | None
    usage: dict[str, int]
    model_id: str | None
    tool_stats: list[dict[str, JsonValue]]
    pending_approval: dict[str, JsonValue] | None
    """The ``PendingApproval`` the run is paused on, until decided or closed."""
    execution: dict[str, JsonValue] | None
    """The ``ApprovalExecution`` of the decision this run resumed from."""
    violations: list[str]
    """Names of the gatekeeper rules this run's answer broke (never its text)."""
    guard_events: list[dict[str, JsonValue]]
    """The ``GuardEvent``s of this run (US-07), content-free."""


def record_guard_events(state: ChatState, events: Sequence[GuardEvent]) -> StateUpdate:
    """Publish ``events`` to the service and return the state update that keeps them.

    Call from a node; merge the result into the node's own update.
    """
    writer = get_stream_writer()
    for event in events:
        writer(event)
    kept = list(state.get("guard_events") or [])
    kept.extend(event.model_dump(mode="json") for event in events)
    return {"guard_events": kept}


def _initial(text: str) -> ChatState:
    return {
        "transcript": dump_transcript([UserTurn(text=text)]),
        "rounds": 0,
        "pending_tool_calls": [],
        "answer_segments": [],
        "outcome": None,
        "usage": Usage().model_dump(),
        "model_id": None,
        "tool_stats": [],
        "pending_approval": None,
        "execution": None,
        "violations": [],
        "guard_events": [],
    }


def _usage(state: ChatState) -> Usage:
    return Usage.model_validate(state["usage"])


def _turn_items(
    text_parts: Sequence[str], calls: Sequence[ToolCallItem], end: ModelTurnEnd
) -> list[TranscriptItem]:
    """The transcript items of one model turn, in the order the model produced them."""
    if not end.content:
        text = "".join(text_parts)
        return [*([AssistantText(text=text)] if text else []), *calls]
    items: list[TranscriptItem] = []
    for block in end.content:
        kind = block.get("type")
        if kind in _THINKING_BLOCKS:
            items.append(ThinkingItem(block=block))
        elif kind == "text" and isinstance(block.get("text"), str) and block["text"]:
            items.append(AssistantText(text=str(block["text"])))
        elif kind == "tool_use":
            items.append(
                ToolCallItem.model_validate(
                    {
                        "call_id": block.get("id"),
                        "name": block.get("name"),
                        "arguments": block.get("input"),
                    }
                )
            )
    return items


def _after_agent(state: ChatState) -> Literal["tools", "gatekeeper", "finalize"]:
    if state["outcome"] is not None:
        return _FINALIZE
    return _TOOLS if state["pending_tool_calls"] else _GATEKEEPER


def _after_tools(state: ChatState) -> Literal["agent", "approval", "finalize"]:
    if state["outcome"] is not None:
        return _FINALIZE
    return _APPROVAL if state.get("pending_approval") is not None else _AGENT


def _after_approval(state: ChatState) -> Literal["agent", "finalize"]:
    return _FINALIZE if state["outcome"] is not None else _AGENT


def _resume_value(approval_id: UUID, decision: Decision) -> dict[str, str]:
    """The value ``resume`` hands to the paused node: only an id and a decision (research §4)."""
    return {"approval_id": str(approval_id), "decision": decision.value}


def _decision_for(value: object, approval_id: UUID) -> Decision | None:
    """The decision in a resume ``value``, or ``None`` unless it names ``approval_id``."""
    if not isinstance(value, dict) or value.get("approval_id") != str(approval_id):
        return None
    raw = value.get("decision")
    return Decision(raw) if isinstance(raw, str) and raw in Decision else None


def _replacement(state: ChatState) -> str:
    """What replaces an unsafe answer: after a decision, the outcome's fixed text, else the
    safe message, so a resolved approval is always answered by what actually happened."""
    raw = state.get("execution")
    if raw is None:
        return SAFE_MESSAGE
    execution = ApprovalExecution.model_validate(raw)
    return outcome_message(execution.outcome, execution.order_id)


def _stat(name: str, outcome: ToolOutcome) -> dict[str, JsonValue]:
    stat = ToolStat(name=name, outcome=outcome.outcome, duration_ms=outcome.duration_ms)
    return stat.model_dump(mode="json")


def _config(conversation_id: UUID) -> RunnableConfig:
    """The checkpointer thread of the conversation."""
    return {"configurable": {"thread_id": str(conversation_id)}}


def _ending(outcome: str | None) -> FinishReason | ChatErrorCode:
    if outcome is None:
        return FinishReason.COMPLETED
    if outcome in FinishReason:
        return FinishReason(outcome)
    return ChatErrorCode(outcome)


class ChatGraph:
    """The compiled chat graph and the dependencies of its nodes."""

    def __init__(
        self,
        *,
        model: ChatModel | None,
        tools: ToolRunner,
        prompt: SystemPrompt,
        checkpointer: Checkpointer,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Sleep = asyncio.sleep,
        clock: Clock | None = None,
        approval_ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
        guardrails: Guardrails | None = None,
    ) -> None:
        """Answer with ``model`` (``None``: every run ends ``LLM_UNAVAILABLE``) and ``tools``.

        ``clock`` stamps proposals, which stay approvable for ``approval_ttl_seconds``.
        ``guardrails`` are the US-07 guards (``None``: ``Guardrails.noop()``).
        """
        self.guardrails = guardrails or Guardrails.noop()
        self._model = model
        self._max_tool_rounds = max_tool_rounds
        self._max_retries = max_retries
        self._sleep = sleep
        self._tools = tools
        self._prompt = prompt
        self._checkpointer = checkpointer
        self._clock = clock or SystemClock()
        self._approval_ttl = timedelta(seconds=approval_ttl_seconds)
        self._compiled = self._compile()

    @property
    def prompt_version(self) -> str:
        """Version of the system prompt, reported as ``promptVersion``."""
        return self._prompt.version

    def _compile(self) -> CompiledStateGraph[ChatState, RunContext, ChatState, ChatState]:
        graph = StateGraph(ChatState, context_schema=RunContext)
        graph.add_node(_GUARD, self._guard_input)
        graph.add_node(_AGENT, self._agent)
        graph.add_node(_TOOLS, self._run_tools)
        graph.add_node(_APPROVAL, self._approval)
        graph.add_node(_GATEKEEPER, self._gatekeeper)
        graph.add_node(_FINALIZE, self._finalize)
        graph.add_edge(START, _GUARD)
        graph.add_edge(_GUARD, _AGENT)
        graph.add_conditional_edges(_AGENT, _after_agent)
        graph.add_conditional_edges(_TOOLS, _after_tools)
        graph.add_conditional_edges(_APPROVAL, _after_approval)
        graph.add_edge(_GATEKEEPER, _FINALIZE)
        graph.add_edge(_FINALIZE, END)
        return graph.compile(checkpointer=self._checkpointer)

    async def run(
        self,
        conversation_id: UUID,
        text: str,
        allowance: RunAllowance | None = None,
        quota: ProposalQuota | None = None,
    ) -> AsyncGenerator[GraphEvent | RunOutcome]:
        """Answer ``text`` in the conversation: the events in order, then one ``RunOutcome``.

        Every model call is admitted by ``allowance`` (``None``: no token limit), every proposal
        by ``quota`` (``None``: no hourly limit). A run that pauses for an approval ends
        ``awaiting_approval`` with the outcome's ``proposal``.
        """
        context = RunContext(allowance=allowance, quota=quota)
        async for item in self._stream(_initial(text), conversation_id, context):
            yield item

    async def resume(
        self,
        conversation_id: UUID,
        approval_id: UUID,
        decision: Decision,
        allowance: RunAllowance | None = None,
        quota: ProposalQuota | None = None,
    ) -> AsyncGenerator[GraphEvent | RunOutcome]:
        """Continue the run paused on ``approval_id`` with the customer's recorded ``decision``.

        Raises ``ApprovalNotPaused`` when the conversation is not paused on that approval.
        """
        snapshot = await self._compiled.aget_state(_config(conversation_id))
        values = cast("ChatState", snapshot.values or {})
        pending = values.get("pending_approval")
        if (
            pending is None
            or _APPROVAL not in snapshot.next
            or PendingApproval.model_validate(pending).approval_id != approval_id
        ):
            raise ApprovalNotPaused(str(approval_id))
        # The paused run's guard events were recorded with it; this run starts with none.
        command: Command[str] = Command(
            resume=_resume_value(approval_id, decision), update={"guard_events": []}
        )
        context = RunContext(allowance=allowance, quota=quota)
        async for item in self._stream(command, conversation_id, context):
            yield item

    async def _stream(
        self,
        graph_input: ChatState | Command[str],
        conversation_id: UUID,
        context: RunContext,
    ) -> AsyncGenerator[GraphEvent | RunOutcome]:
        final: ChatState | None = None
        stream = self._compiled.astream(
            graph_input,
            _config(conversation_id),
            context=context,
            stream_mode=["custom", "values"],
        )
        try:
            async for mode, chunk in stream:
                if mode == "custom" and isinstance(chunk, _GRAPH_EVENTS):
                    yield chunk
                elif mode == "values" and isinstance(chunk, dict):
                    final = cast("ChatState", chunk)
        finally:
            if isinstance(stream, AsyncGenerator):  # cancel the nodes still running
                await stream.aclose()
        if final is None:  # pragma: no cover - LangGraph always emits the final values
            raise RuntimeError("the chat graph produced no state")
        yield self._outcome(final)

    async def forget(self, conversation_id: UUID) -> None:
        """Delete the stored state of the conversation (FR-004)."""
        await self._checkpointer.adelete_thread(str(conversation_id))

    async def record_interrupted(
        self,
        conversation_id: UUID,
        sent: str,
        results: Sequence[ToolResultItem] = (),
    ) -> None:
        """After a cancelled run, keep the transcript what the customer saw (FR-014, US6).

        Calls left without a result get their entry of ``results`` (an approval executed before
        the client went away) or a ``cancelled`` result; the part of ``sent`` not yet in the
        transcript (the turn that was streaming) is added as the assistant's text. A pause on an
        approval is ended.
        """
        config = _config(conversation_id)
        snapshot = await self._compiled.aget_state(config)
        values = cast("ChatState | None", snapshot.values or None)
        if values is None:
            return
        transcript = load_transcript(values["transcript"])
        answered = {item.call_id for item in transcript if isinstance(item, ToolResultItem)}
        known = {result.call_id: result for result in results}
        items: list[TranscriptItem] = [
            known.get(item.call_id)
            or ToolResultItem(
                call_id=item.call_id, name=item.name, ok=False, payload=_CANCELLED_RESULT
            )
            for item in transcript
            if isinstance(item, ToolCallItem) and item.call_id not in answered
        ]
        committed = "".join(values.get("answer_segments", []))
        rest = sent.removeprefix(committed) if sent.startswith(committed) else sent
        if rest:
            items.append(AssistantText(text=rest))
        paused = values.get("pending_approval") is not None
        if items or paused:
            update: StateUpdate = {"transcript": dump_transcript(items), "pending_approval": None}
            await self._compiled.aupdate_state(config, update, as_node=_FINALIZE)

    async def close_approval(self, conversation_id: UUID, outcome: ApprovalOutcome) -> bool:
        """End the pause of a conversation without a decision (research §6).

        The paused call gets the fixed "nothing changed" result of ``outcome`` (or "could not be
        confirmed" for ``failed``), so the transcript stays valid for the model. ``False`` when
        the conversation was not paused.
        """
        config = _config(conversation_id)
        snapshot = await self._compiled.aget_state(config)
        values = cast("ChatState", snapshot.values or {})
        raw = values.get("pending_approval")
        if raw is None:
            return False
        pending = PendingApproval.model_validate(raw)
        result = ToolResultItem(
            call_id=pending.call_id, name=pending.action, ok=False, payload=outcome_payload(outcome)
        )
        update: StateUpdate = {"transcript": dump_transcript([result]), "pending_approval": None}
        await self._compiled.aupdate_state(config, update, as_node=_FINALIZE)
        return True

    def _outcome(self, state: ChatState) -> RunOutcome:
        raw_execution = state.get("execution")
        execution = (
            None if raw_execution is None else ApprovalExecution.model_validate(raw_execution)
        )
        common = {
            "text": "".join(state["answer_segments"]),
            "usage": _usage(state),
            "model_id": state["model_id"],
            "tools": tuple(ToolStat.model_validate(stat) for stat in state["tool_stats"]),
            "execution": execution,
            "violations": tuple(dict.fromkeys(state.get("violations") or [])),
            "guard_events": tuple(
                GuardEvent.model_validate(raw) for raw in state.get("guard_events") or []
            ),
        }
        pending = state.get("pending_approval")
        if pending is not None and state["outcome"] is None:
            return RunOutcome.model_validate(
                common
                | {
                    "finish_reason": FinishReason.AWAITING_APPROVAL,
                    "proposal": PendingApproval.model_validate(pending),
                }
            )
        ending = _ending(state["outcome"])
        return RunOutcome.model_validate(
            common
            | {
                "finish_reason": ending if isinstance(ending, FinishReason) else None,
                "error": ending if isinstance(ending, ChatErrorCode) else None,
            }
        )

    # Nodes ------------------------------------------------------------------------------------

    async def _guard_input(self, state: ChatState) -> StateUpdate:
        """Re-validate the stored transcript; it must end with the customer's new message."""
        transcript = load_transcript(state["transcript"])
        if not transcript or not isinstance(transcript[-1], UserTurn):
            raise ValueError("the chat transcript must end with the customer's message")
        return {}

    async def _agent(self, state: ChatState, runtime: Runtime[RunContext]) -> StateUpdate:
        """One model turn: stream gatekept segments, collect tool calls, add usage."""
        update = await self._model_turn(state, runtime)
        failed = update.get("outcome") in _MODEL_FAILURES
        if failed and state.get("execution") is not None and not update.get("answer_segments"):
            return self._outcome_message(state, update)
        return update

    def _outcome_message(self, state: ChatState, failed: StateUpdate) -> StateUpdate:
        """After a decision the model could not answer: the outcome's fixed text (ADR 0003)."""
        execution = ApprovalExecution.model_validate(state["execution"])
        message = outcome_message(execution.outcome, execution.order_id)
        get_stream_writer()(Token(text=message))
        usage = Usage.model_validate(failed.get("usage", state["usage"]))
        return self._fixed_ending(state, [message], FinishReason.COMPLETED, usage, None)

    async def _model_turn(self, state: ChatState, runtime: Runtime[RunContext]) -> StateUpdate:
        if self._model is None:
            return {"outcome": ChatErrorCode.LLM_UNAVAILABLE.value}
        model = self._model
        allowance = runtime.context.allowance
        write = get_stream_writer()
        transcript = tuple(load_transcript(state["transcript"]))
        evidence = ChatEvidence.from_transcript(
            transcript, approval_resolved=state.get("execution") is not None
        )
        replacement = _replacement(state)
        request = ChatModelRequest(
            system=self._prompt.system, transcript=transcript, tools=self._tools.tool_specs()
        )
        usage = _usage(state)
        failed = Usage()

        async def attempt(_timeout: float) -> _Turn:
            # The adapter enforces the per-call timeout itself (LLM_TIMEOUT_SECONDS).
            nonlocal failed
            if allowance is not None and not await allowance.admit():
                raise _BudgetSpent
            turn = _Turn()
            try:
                await self._stream_turn(model, request, evidence, write, turn, replacement)
            except ModelError as error:
                spent = Usage(input_tokens=error.input_tokens, output_tokens=error.output_tokens)
                failed = failed + spent
                if allowance is not None:
                    allowance.spend(spent)
                raise _TurnFailed(turn, error) from error
            if allowance is not None and turn.end is not None:
                allowance.spend(turn.end.usage, turn.end.model_id)
            return turn

        try:
            turn = await call_with_retry(
                attempt,
                deadline=math.inf,
                max_retries=self._max_retries,
                backoff_base=RETRY_BACKOFF_SECONDS,
                attempt_timeout=math.inf,
                is_retryable=_retryable,
                sleep=self._sleep,
            )
        except _BudgetSpent:
            return {
                "outcome": ChatErrorCode.BUDGET_EXCEEDED.value,
                "usage": (usage + failed).model_dump(),
            }
        except _TurnFailed as error:
            return {
                "outcome": ChatErrorCode.LLM_UNAVAILABLE.value,
                "answer_segments": [*state["answer_segments"], *error.turn.sent],
                "usage": (usage + failed).model_dump(),
            }
        usage = usage + failed
        sent, end = turn.sent, turn.end
        if turn.replaced:
            ending = self._fixed_ending(state, sent, FinishReason.REPLACED, usage, end)
            ending["violations"] = [*(state.get("violations") or []), *turn.violations]
            return ending
        if end is None:
            return {
                "outcome": ChatErrorCode.LLM_UNAVAILABLE.value,
                "answer_segments": [*state["answer_segments"], *sent],
                "usage": usage.model_dump(),
            }
        items = _turn_items(turn.text_parts, turn.calls, end)
        pending = [item for item in items if isinstance(item, ToolCallItem)]
        if pending and state["rounds"] >= self._max_tool_rounds:
            write(Token(text=TOOL_LIMIT_MESSAGE))
            sent.append(TOOL_LIMIT_MESSAGE)
            return self._fixed_ending(state, sent, FinishReason.TOOL_LIMIT, usage, end)
        return {
            "transcript": dump_transcript(items),
            "pending_tool_calls": dump_transcript(pending),
            "answer_segments": [*state["answer_segments"], *sent],
            "usage": (usage + end.usage).model_dump(),
            "model_id": end.model_id,
        }

    async def _stream_turn(
        self,
        model: ChatModel,
        request: ChatModelRequest,
        evidence: ChatEvidence,
        write: Callable[[GraphEvent], None],
        turn: _Turn,
        replacement: str = SAFE_MESSAGE,
    ) -> None:
        """Stream one model call into ``turn``, releasing each segment once it is gatekept.

        A violating segment is replaced by ``replacement`` and the model stream stops being
        read (``turn.replaced``); the remainder is released when the turn ends (FR-016).
        """
        segmenter = Segmenter()

        def release(segment: str) -> bool:
            """Send ``segment`` if it is safe; else send the safe message and return False."""
            rules = check_segment(segment, evidence)
            turn.violations.extend(rule.value for rule in rules)
            text = replacement if rules else segment
            write(Token(text=text))
            turn.sent.append(text)
            return not rules

        stream = model.stream(request)
        try:
            async for event in stream:
                if isinstance(event, TextDelta):
                    turn.text_parts.append(event.text)
                    if not all(release(segment) for segment in segmenter.feed(event.text)):
                        turn.replaced = True
                        return
                elif isinstance(event, ToolUseRequest):
                    turn.calls.append(
                        ToolCallItem(call_id=event.id, name=event.name, arguments=event.input)
                    )
                else:
                    turn.end = event
        finally:
            if isinstance(stream, AsyncGenerator):  # stop reading the model (FR-016)
                await stream.aclose()
        rest = segmenter.finish()
        if rest is not None and not release(rest):
            turn.replaced = True

    async def _run_tools(self, state: ChatState, runtime: Runtime[RunContext]) -> StateUpdate:
        """Run every pending call; an upstream 401 ends the run ``TOOL_FAILED`` (FR-011).

        A ``cancel_order`` call never runs here: it becomes a checked proposal (routing to
        ``approval``) or a refusal result (FR-003, FR-005).
        """
        write = get_stream_writer()
        results: list[ToolResultItem] = []
        stats: list[dict[str, JsonValue]] = []
        unauthenticated = False
        proposal: PendingApproval | None = None
        for raw in state["pending_tool_calls"]:
            call = ToolCallItem.model_validate(raw)
            write(ToolCall(call_id=call.call_id, name=call.name, arguments=call.arguments))
            if call.name in APPROVAL_ACTIONS:
                checked = await self._propose(
                    call, already_pending=proposal is not None, quota=runtime.context.quota
                )
                stats.extend(checked.stats)
                unauthenticated = unauthenticated or checked.unauthenticated
                if checked.proposal is not None:
                    proposal = checked.proposal
                    continue
                result, summary = checked.refusal_result(call)
            else:
                outcome = await self._tools.run(call.name, call.arguments)
                result = ToolResultItem(
                    call_id=call.call_id, name=call.name, ok=outcome.ok, payload=outcome.payload
                )
                summary = summarize(call.name, outcome)
                stats.append(_stat(call.name, outcome))
                unauthenticated = unauthenticated or outcome.upstream_unauthenticated
            write(ToolResult(call_id=call.call_id, name=call.name, ok=result.ok, summary=summary))
            results.append(result)
        if unauthenticated and proposal is not None:
            # The run ends TOOL_FAILED: no pause, and the call still gets its result.
            results.append(
                ToolResultItem(
                    call_id=proposal.call_id,
                    name=proposal.action,
                    ok=False,
                    payload=refusal_payload(Refusal.UNAVAILABLE),
                )
            )
            proposal = None
        update: StateUpdate = {
            "transcript": dump_transcript(results),
            "pending_tool_calls": [],
            "rounds": state["rounds"] + 1,
            "tool_stats": [*state["tool_stats"], *stats],
            "pending_approval": None if proposal is None else proposal.model_dump(mode="json"),
        }
        if unauthenticated:
            update["outcome"] = ChatErrorCode.TOOL_FAILED.value
        return update

    async def _propose(
        self, call: ToolCallItem, *, already_pending: bool, quota: ProposalQuota | None
    ) -> "_Checked":
        """Check a ``cancel_order`` call: a ``PendingApproval``, or the refusal the model gets.

        Checked in order: a valid id, nothing else pending, the hourly limit (FR-015), then a
        fresh read showing the caller's order ``PENDING``.
        """
        target = parse_target(call.arguments)
        if target is None:
            return _Checked.refused(Refusal.INVALID_ID)
        if already_pending:
            return _Checked.refused(Refusal.ALREADY_PENDING)
        if quota is not None and not await quota.allows():
            return _Checked.refused(Refusal.RATE_LIMITED)
        outcome = await self._tools.run(READ_TOOL, {TARGET_ARGUMENT: str(target)})
        stats = [_stat(READ_TOOL, outcome)]
        read = read_order(outcome)
        if read.refusal is not None:
            return _Checked.refused(
                read.refusal,
                read.status,
                stats,
                unauthenticated=read.unauthenticated,
                order_id=target,
            )
        now = self._clock.now()
        proposal = PendingApproval(
            approval_id=uuid4(),
            action=call.name,
            order_id=target,
            arguments={TARGET_ARGUMENT: str(target)},
            summary=summary_of(read),
            call_id=call.call_id,
            proposed_at=now,
            expires_at=now + self._approval_ttl,
        )
        return _Checked(proposal=proposal, stats=stats)

    async def _approval(self, state: ChatState) -> StateUpdate:
        """Pause for the customer's decision, then carry it out (research §1, §5).

        ``interrupt`` comes first: on resume the node runs again from the top, so nothing before
        it may have a side effect. A resume value naming another approval is ignored (the node
        pauses again). Only the arguments stored in the state are ever run.
        """
        raw = state.get("pending_approval")
        if raw is None:  # pragma: no cover - routed here only with a pending approval
            return {}
        pending = PendingApproval.model_validate(raw)
        payload = pending.required_event().data()
        decision = _decision_for(interrupt(payload), pending.approval_id)
        while decision is None:
            decision = _decision_for(interrupt(payload), pending.approval_id)
        stats: list[dict[str, JsonValue]] = []
        unauthenticated = False
        result_payload: dict[str, JsonValue] | None = None
        if decision is Decision.REJECT:
            outcome = ApprovalOutcome.REJECTED
        else:
            read_outcome = await self._tools.run(
                READ_TOOL, {TARGET_ARGUMENT: str(pending.order_id)}
            )
            stats.append(_stat(READ_TOOL, read_outcome))
            read = read_order(read_outcome)
            unauthenticated = read.unauthenticated
            blocked = outcome_after_reread(read)
            if blocked is not None:
                outcome = blocked
            else:
                cancel = await self._tools.run_approved(pending.action, pending.arguments)
                stats.append(_stat(pending.action, cancel))
                unauthenticated = cancel.upstream_unauthenticated
                outcome = outcome_of_cancel(cancel)
                if outcome is ApprovalOutcome.CANCELLED:
                    # The Orders envelope, with the decision and outcome stated explicitly.
                    result_payload = cancel.payload | {
                        "approval": approval_status(decision, outcome)
                    }
        cancelled = outcome is ApprovalOutcome.CANCELLED
        result = ToolResultItem(
            call_id=pending.call_id,
            name=pending.action,
            ok=cancelled,
            payload=(
                result_payload if result_payload is not None else outcome_payload(outcome, decision)
            ),
        )
        execution = ApprovalExecution(
            approval_id=pending.approval_id,
            order_id=pending.order_id,
            decision=decision,
            outcome=outcome,
            result=result,
        )
        write = get_stream_writer()
        write(ApprovalResolved(approval_id=pending.approval_id, decision=decision, outcome=outcome))
        write(
            ToolResult(
                call_id=pending.call_id,
                name=pending.action,
                ok=cancelled,
                summary=outcome_summary(outcome),
            )
        )
        write(execution)
        # The decision starts a new answer: the per-run fields of the paused run are reset.
        return {
            "transcript": dump_transcript([result]),
            "pending_approval": None,
            "execution": execution.model_dump(mode="json"),
            "rounds": 0,
            "pending_tool_calls": [],
            "answer_segments": [],
            "outcome": ChatErrorCode.TOOL_FAILED.value if unauthenticated else None,
            "usage": Usage().model_dump(),
            "model_id": None,
            "tool_stats": stats,
            "violations": [],
        }

    def _fixed_ending(
        self,
        state: ChatState,
        sent: Sequence[str],
        reason: FinishReason,
        usage: Usage,
        end: ModelTurnEnd | None,
    ) -> StateUpdate:
        """End the run with a fixed message already sent; the transcript keeps only ``sent``."""
        return {
            "outcome": reason.value,
            "transcript": dump_transcript([AssistantText(text="".join(sent))]),
            "pending_tool_calls": [],
            "answer_segments": [*state["answer_segments"], *sent],
            "usage": (usage if end is None else usage + end.usage).model_dump(),
            "model_id": state["model_id"] if end is None else end.model_id,
        }

    async def _gatekeeper(self, state: ChatState) -> StateUpdate:
        """Check the whole answer; an unsafe or empty one ends ``replaced`` (FR-015, FR-016).

        Each segment already passed ``check_segment`` before it was sent, so only a rule spanning
        segments fails here, on text the customer has seen: ``AnswerReplaced`` makes the stream
        send ``message.replaced`` and the message keeps only the fixed text. An empty answer
        showed nothing, so its fixed text is a plain ``token``.
        """
        answer = "".join(state["answer_segments"])
        if answer.strip():
            evidence = ChatEvidence.from_transcript(
                load_transcript(state["transcript"]),
                approval_resolved=state.get("execution") is not None,
            )
            rules = check_answer(answer, evidence)
            if not rules:
                return {}
            violations = [*(state.get("violations") or []), *(rule.value for rule in rules)]
            message = _replacement(state)
            get_stream_writer()(AnswerReplaced(text=message))
            segments = [message]
        else:
            violations = list(state.get("violations") or [])
            message = NO_ANSWER_MESSAGE
            get_stream_writer()(Token(text=message))
            segments = [*state["answer_segments"], message]
        return {
            "outcome": FinishReason.REPLACED.value,
            "transcript": dump_transcript([AssistantText(text=message)]),
            "answer_segments": segments,
            "violations": violations,
        }

    async def _finalize(self, state: ChatState) -> StateUpdate:
        """Record how the run ended: ``completed`` unless an error or the gatekeeper decided."""
        return {"outcome": _ending(state["outcome"]).value}
