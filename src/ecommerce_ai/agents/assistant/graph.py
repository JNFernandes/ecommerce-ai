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
"""

import asyncio
import math
import operator
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Final, Literal, TypedDict, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import JsonValue

from ecommerce_ai.agents.assistant.gatekeeper import (
    NO_ANSWER_MESSAGE,
    SAFE_MESSAGE,
    TOOL_LIMIT_MESSAGE,
    ChatEvidence,
    check_answer,
    check_segment,
)
from ecommerce_ai.agents.assistant.limits import RunAllowance
from ecommerce_ai.agents.assistant.model import (
    AssistantText,
    ChatErrorCode,
    FinishReason,
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
    ToolRunner,
    ToolUseRequest,
)
from ecommerce_ai.agents.assistant.segmenter import Segmenter
from ecommerce_ai.agents.assistant.summaries import summarize
from ecommerce_ai.agents.ports import ModelError, ModelUnavailable
from ecommerce_ai.agents.prompts.loader import SystemPrompt
from ecommerce_ai.infrastructure.retry import call_with_retry

type GraphEvent = Token | ToolCall | ToolResult
"""An event a node publishes while the graph runs."""

type StateUpdate = dict[str, object]
"""Fields a node changes; LangGraph merges them into the state."""

_GUARD: Final = "guard_input"
_AGENT: Final = "agent"
_TOOLS: Final = "tools"
_GATEKEEPER: Final = "gatekeeper"
_FINALIZE: Final = "finalize"
_THINKING_BLOCKS: Final = frozenset({"thinking", "redacted_thinking"})
DEFAULT_MAX_TOOL_ROUNDS: Final = 5
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


@dataclass(slots=True)
class _Turn:
    """What one model call produced and what of it was sent."""

    sent: list[str] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    calls: list[ToolCallItem] = field(default_factory=list)
    end: ModelTurnEnd | None = None
    replaced: bool = False


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


def _after_tools(state: ChatState) -> Literal["agent", "finalize"]:
    return _FINALIZE if state["outcome"] is not None else _AGENT


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
    ) -> None:
        """Answer with ``model`` (``None``: every run ends ``LLM_UNAVAILABLE``) and ``tools``."""
        self._model = model
        self._max_tool_rounds = max_tool_rounds
        self._max_retries = max_retries
        self._sleep = sleep
        self._tools = tools
        self._prompt = prompt
        self._checkpointer = checkpointer
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
        graph.add_node(_GATEKEEPER, self._gatekeeper)
        graph.add_node(_FINALIZE, self._finalize)
        graph.add_edge(START, _GUARD)
        graph.add_edge(_GUARD, _AGENT)
        graph.add_conditional_edges(_AGENT, _after_agent)
        graph.add_conditional_edges(_TOOLS, _after_tools)
        graph.add_edge(_GATEKEEPER, _FINALIZE)
        graph.add_edge(_FINALIZE, END)
        return graph.compile(checkpointer=self._checkpointer)

    async def run(
        self, conversation_id: UUID, text: str, allowance: RunAllowance | None = None
    ) -> AsyncGenerator[GraphEvent | RunOutcome]:
        """Answer ``text`` in the conversation: the events in order, then one ``RunOutcome``.

        Every model call is admitted by ``allowance`` (``None``: no token limit).
        """
        final: ChatState | None = None
        stream = self._compiled.astream(
            _initial(text),
            _config(conversation_id),
            context=RunContext(allowance=allowance),
            stream_mode=["custom", "values"],
        )
        try:
            async for mode, chunk in stream:
                if mode == "custom" and isinstance(chunk, Token | ToolCall | ToolResult):
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

    async def record_interrupted(self, conversation_id: UUID, sent: str) -> None:
        """After a cancelled run, keep the transcript what the customer saw (FR-014, US6).

        Calls left without a result get a ``cancelled`` result, and the part of ``sent`` not yet
        in the transcript (the turn that was streaming) is added as the assistant's text.
        """
        config = _config(conversation_id)
        snapshot = await self._compiled.aget_state(config)
        values = cast("ChatState | None", snapshot.values or None)
        if values is None:
            return
        transcript = load_transcript(values["transcript"])
        answered = {item.call_id for item in transcript if isinstance(item, ToolResultItem)}
        items: list[TranscriptItem] = [
            ToolResultItem(
                call_id=item.call_id, name=item.name, ok=False, payload=_CANCELLED_RESULT
            )
            for item in transcript
            if isinstance(item, ToolCallItem) and item.call_id not in answered
        ]
        committed = "".join(values.get("answer_segments", []))
        rest = sent.removeprefix(committed) if sent.startswith(committed) else sent
        if rest:
            items.append(AssistantText(text=rest))
        if items:
            await self._compiled.aupdate_state(
                config, {"transcript": dump_transcript(items)}, as_node=_FINALIZE
            )

    def _outcome(self, state: ChatState) -> RunOutcome:
        ending = _ending(state["outcome"])
        return RunOutcome(
            finish_reason=ending if isinstance(ending, FinishReason) else None,
            error=ending if isinstance(ending, ChatErrorCode) else None,
            text="".join(state["answer_segments"]),
            usage=_usage(state),
            model_id=state["model_id"],
            tools=tuple(ToolStat.model_validate(stat) for stat in state["tool_stats"]),
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
        if self._model is None:
            return {"outcome": ChatErrorCode.LLM_UNAVAILABLE.value}
        model = self._model
        allowance = runtime.context.allowance
        write = get_stream_writer()
        transcript = tuple(load_transcript(state["transcript"]))
        evidence = ChatEvidence.from_transcript(transcript)
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
                await self._stream_turn(model, request, evidence, write, turn)
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
            return self._fixed_ending(state, sent, FinishReason.REPLACED, usage, end)
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
    ) -> None:
        """Stream one model call into ``turn``, releasing each segment once it is gatekept.

        A violating segment is replaced by the safe message and the model stream stops being
        read (``turn.replaced``); the remainder is released when the turn ends (FR-016).
        """
        segmenter = Segmenter()

        def release(segment: str) -> bool:
            """Send ``segment`` if it is safe; else send the safe message and return False."""
            safe = not check_segment(segment, evidence)
            text = segment if safe else SAFE_MESSAGE
            write(Token(text=text))
            turn.sent.append(text)
            return safe

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

    async def _run_tools(self, state: ChatState) -> StateUpdate:
        """Run every pending call; an upstream 401 ends the run ``TOOL_FAILED`` (FR-011)."""
        write = get_stream_writer()
        results: list[TranscriptItem] = []
        stats: list[dict[str, JsonValue]] = []
        unauthenticated = False
        for raw in state["pending_tool_calls"]:
            call = ToolCallItem.model_validate(raw)
            write(ToolCall(call_id=call.call_id, name=call.name, arguments=call.arguments))
            outcome = await self._tools.run(call.name, call.arguments)
            write(
                ToolResult(
                    call_id=call.call_id,
                    name=call.name,
                    ok=outcome.ok,
                    summary=summarize(call.name, outcome),
                )
            )
            results.append(
                ToolResultItem(
                    call_id=call.call_id, name=call.name, ok=outcome.ok, payload=outcome.payload
                )
            )
            stat = ToolStat(
                name=call.name, outcome=outcome.outcome, duration_ms=outcome.duration_ms
            )
            stats.append(stat.model_dump(mode="json"))
            unauthenticated = unauthenticated or outcome.upstream_unauthenticated
        update: StateUpdate = {
            "transcript": dump_transcript(results),
            "pending_tool_calls": [],
            "rounds": state["rounds"] + 1,
            "tool_stats": [*state["tool_stats"], *stats],
        }
        if unauthenticated:
            update["outcome"] = ChatErrorCode.TOOL_FAILED.value
        return update

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
        """Check the whole answer; an unsafe or empty one ends ``replaced`` (FR-015, FR-016)."""
        answer = "".join(state["answer_segments"])
        if answer.strip():
            evidence = ChatEvidence.from_transcript(load_transcript(state["transcript"]))
            if not check_answer(answer, evidence):
                return {}
            message = SAFE_MESSAGE
        else:
            message = NO_ANSWER_MESSAGE
        get_stream_writer()(Token(text=message))
        return {
            "outcome": FinishReason.REPLACED.value,
            "transcript": dump_transcript([AssistantText(text=message)]),
            "answer_segments": [*state["answer_segments"], message],
        }

    async def _finalize(self, state: ChatState) -> StateUpdate:
        """Record how the run ended: ``completed`` unless an error or the gatekeeper decided."""
        return {"outcome": _ending(state["outcome"]).value}
