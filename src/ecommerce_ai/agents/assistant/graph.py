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
"""

import operator
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from typing import Annotated, Final, Literal, TypedDict, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import JsonValue

from ecommerce_ai.agents.assistant.gatekeeper import (
    NO_ANSWER_MESSAGE,
    SAFE_MESSAGE,
    TOOL_LIMIT_MESSAGE,
    ChatEvidence,
    check_answer,
    check_segment,
)
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
from ecommerce_ai.agents.ports import ModelError
from ecommerce_ai.agents.prompts.loader import SystemPrompt

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
    ) -> None:
        """Answer with ``model`` (``None``: every run ends ``LLM_UNAVAILABLE``) and ``tools``."""
        self._model = model
        self._max_tool_rounds = max_tool_rounds
        self._tools = tools
        self._prompt = prompt
        self._checkpointer = checkpointer
        self._compiled = self._compile()

    @property
    def prompt_version(self) -> str:
        """Version of the system prompt, reported as ``promptVersion``."""
        return self._prompt.version

    def _compile(self) -> CompiledStateGraph[ChatState, None, ChatState, ChatState]:
        graph = StateGraph(ChatState)
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

    async def run(self, conversation_id: UUID, text: str) -> AsyncIterator[GraphEvent | RunOutcome]:
        """Answer ``text`` in the conversation: the events in order, then one ``RunOutcome``."""
        config: RunnableConfig = {"configurable": {"thread_id": str(conversation_id)}}
        final: ChatState | None = None
        async for mode, chunk in self._compiled.astream(
            _initial(text), config, stream_mode=["custom", "values"]
        ):
            if mode == "custom" and isinstance(chunk, Token | ToolCall | ToolResult):
                yield chunk
            elif mode == "values" and isinstance(chunk, dict):
                final = cast("ChatState", chunk)
        if final is None:  # pragma: no cover - LangGraph always emits the final values
            raise RuntimeError("the chat graph produced no state")
        yield self._outcome(final)

    async def forget(self, conversation_id: UUID) -> None:
        """Delete the stored state of the conversation (FR-004)."""
        await self._checkpointer.adelete_thread(str(conversation_id))

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

    async def _agent(self, state: ChatState) -> StateUpdate:
        """One model turn: stream gatekept segments, collect tool calls, add usage."""
        if self._model is None:
            return {"outcome": ChatErrorCode.LLM_UNAVAILABLE.value}
        write = get_stream_writer()
        transcript = tuple(load_transcript(state["transcript"]))
        evidence = ChatEvidence.from_transcript(transcript)
        request = ChatModelRequest(
            system=self._prompt.system, transcript=transcript, tools=self._tools.tool_specs()
        )
        segmenter = Segmenter()
        sent: list[str] = []
        text_parts: list[str] = []
        calls: list[ToolCallItem] = []
        end: ModelTurnEnd | None = None

        def release(segment: str) -> bool:
            """Send ``segment`` if it is safe; else send the safe message and return False."""
            safe = not check_segment(segment, evidence)
            text = segment if safe else SAFE_MESSAGE
            write(Token(text=text))
            sent.append(text)
            return safe

        usage = _usage(state)
        stream = self._model.stream(request)
        replaced = False
        try:
            async for event in stream:
                if isinstance(event, TextDelta):
                    text_parts.append(event.text)
                    if not all(release(segment) for segment in segmenter.feed(event.text)):
                        replaced = True
                        break
                elif isinstance(event, ToolUseRequest):
                    calls.append(
                        ToolCallItem(call_id=event.id, name=event.name, arguments=event.input)
                    )
                else:
                    end = event
        except ModelError as error:
            spent = Usage(input_tokens=error.input_tokens, output_tokens=error.output_tokens)
            return {
                "outcome": ChatErrorCode.LLM_UNAVAILABLE.value,
                "answer_segments": [*state["answer_segments"], *sent],
                "usage": (usage + spent).model_dump(),
            }
        finally:
            if isinstance(stream, AsyncGenerator):  # stop reading the model (FR-016)
                await stream.aclose()
        rest = None if replaced else segmenter.finish()
        if replaced or (rest is not None and not release(rest)):
            return self._fixed_ending(state, sent, FinishReason.REPLACED, usage, end)
        if end is None:
            return {
                "outcome": ChatErrorCode.LLM_UNAVAILABLE.value,
                "answer_segments": [*state["answer_segments"], *sent],
            }
        items = _turn_items(text_parts, calls, end)
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
