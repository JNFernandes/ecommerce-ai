"""Scripted models for tests: no network, replies and failures given in order.

``ScriptedModel`` plays the triage ``TriageModel``; ``ScriptedChatModel`` the assistant's
streaming ``ChatModel``; ``CompromisedChatModel`` a chat model that obeys a ``Compromise``
(US-07 red team).
"""

import asyncio
import re
from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ecommerce_ai.agents.assistant.model import (
    ToolCallItem,
    ToolResultItem,
    TranscriptItem,
    Usage,
    UserTurn,
)
from ecommerce_ai.agents.assistant.ports import (
    ChatModelEvent,
    ChatModelRequest,
    ModelTurnEnd,
    TextDelta,
    ToolUseRequest,
)
from ecommerce_ai.agents.triage.model import DraftOutput, SuggestedAction
from ecommerce_ai.agents.triage.ports import ModelError, ModelReply, PromptInput

MODEL_ID = "claude-test-model"


def reply(
    explanation: str = "The payment was declined, so the order was cancelled.",
    suggested_action: SuggestedAction = SuggestedAction.CONTACT_CUSTOMER,
    confidence: float = 0.8,
    *,
    input_tokens: int = 300,
    output_tokens: int = 40,
    model_id: str = MODEL_ID,
) -> ModelReply:
    """A valid model reply with the given draft and usage."""
    return ModelReply(
        draft=DraftOutput(
            explanation=explanation, suggested_action=suggested_action, confidence=confidence
        ),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model_id=model_id,
    )


class ScriptedModel:
    """Answers each ``draft`` with the next scripted reply, or raises the next scripted error.

    Every prompt is recorded in ``prompts``. Running out of script fails the test loudly.
    """

    def __init__(self, script: Iterable[ModelReply | ModelError] = ()) -> None:
        """Queue the replies and errors to hand out, in order."""
        self.script: deque[ModelReply | ModelError] = deque(script)
        self.prompts: list[PromptInput] = []

    @property
    def calls(self) -> int:
        """Number of ``draft`` calls so far."""
        return len(self.prompts)

    async def draft(self, prompt_input: PromptInput) -> ModelReply:
        """Record the prompt, then return or raise the next scripted item."""
        self.prompts.append(prompt_input)
        if not self.script:
            raise AssertionError("ScriptedModel was called more often than scripted")
        item = self.script.popleft()
        if isinstance(item, ModelError):
            raise item
        return item


# Chat assistant -------------------------------------------------------------------------------

type TurnStep = ChatModelEvent | ModelError | asyncio.Event
"""One step of a scripted chat turn: an event to yield, an error to raise or a gate to await."""


def text_turn(
    *texts: str, input_tokens: int = 100, output_tokens: int = 20, model_id: str = MODEL_ID
) -> list[TurnStep]:
    """A turn streaming ``texts`` as deltas, then ending with ``end_turn``."""
    steps: list[TurnStep] = [TextDelta(text=text) for text in texts]
    steps.append(turn_end("end_turn", input_tokens, output_tokens, model_id))
    return steps


def tool_turn(
    *calls: tuple[str, str, dict[str, JsonValue]],
    input_tokens: int = 100,
    output_tokens: int = 10,
    model_id: str = MODEL_ID,
) -> list[TurnStep]:
    """A turn asking for ``calls`` (``(call_id, name, input)``), then ending with ``tool_use``."""
    steps: list[TurnStep] = [
        ToolUseRequest(id=call_id, name=name, input=arguments) for call_id, name, arguments in calls
    ]
    steps.append(turn_end("tool_use", input_tokens, output_tokens, model_id))
    return steps


def turn_end(
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 20,
    model_id: str = MODEL_ID,
) -> ModelTurnEnd:
    """The closing event of a turn."""
    return ModelTurnEnd(
        stop_reason=stop_reason,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        model_id=model_id,
    )


class ScriptedChatModel:
    """A ``ChatModel`` playing one scripted turn per ``stream`` call; no network.

    Each turn is a list of steps: events are yielded, a ``ModelError`` is raised where it stands
    (first step = before the first delta), an ``asyncio.Event`` is awaited (to hold the stream
    open for disconnect tests). Every request is recorded in ``requests``; ``closed_early`` counts
    turns whose consumer stopped reading before the end. Running out of script fails loudly.
    """

    def __init__(self, turns: Iterable[Sequence[TurnStep]] = ()) -> None:
        """Queue the turns to play, in order."""
        self.turns: deque[Sequence[TurnStep]] = deque(turns)
        self.requests: list[ChatModelRequest] = []
        self.closed_early = 0

    @property
    def calls(self) -> int:
        """Number of ``stream`` calls so far."""
        return len(self.requests)

    async def stream(self, request: ChatModelRequest) -> AsyncIterator[ChatModelEvent]:
        """Record ``request``, then play the next scripted turn."""
        self.requests.append(request)
        if not self.turns:
            raise AssertionError("ScriptedChatModel was called more often than scripted")
        steps = self.turns.popleft()
        finished = raised = False
        try:
            for step in steps:
                if isinstance(step, ModelError):
                    raised = True
                    raise step
                if isinstance(step, asyncio.Event):
                    await step.wait()
                    continue
                yield step
            finished = True
        finally:
            if not finished and not raised:
                self.closed_early += 1


# Compromised chat model (US-07 red team) --------------------------------------------------------

CANARY_PATTERN = re.compile(r"cnry-[0-9a-f]{24}")
"""A canary token as the guardrails render it into the system prompt."""

COMPROMISED_CALL_PREFIX = "compromised-call-"


class Compromise(BaseModel):
    """What a compromised model does, as a red-team case states it.

    ``call_tool`` with ``tool_arguments`` is requested ``call_times`` times in the message before
    it answers (each as a new call, so a guard sees repeats). The answer is ``say``, then the
    canary found in the system prompt (``leak_canary``), then the text of every tool result of
    the message (``repeat_tool_text``), joined by spaces; ``"OK."`` when that is empty.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_tool: str | None = None
    tool_arguments: dict[str, JsonValue] = Field(default_factory=dict)
    call_times: int = Field(default=1, ge=1)
    say: str | None = None
    leak_canary: bool = False
    repeat_tool_text: bool = False


def _since_last_user(transcript: Sequence[TranscriptItem]) -> list[TranscriptItem]:
    """The items after the newest user turn (this message's model and tool items)."""
    for index in range(len(transcript) - 1, -1, -1):
        if isinstance(transcript[index], UserTurn):
            return list(transcript[index + 1 :])
    return list(transcript)


def _leaves(value: JsonValue) -> list[str]:
    """Every string inside ``value``, depth first."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _leaves(item)]
    return []


class CompromisedChatModel:
    """A ``ChatModel`` that obeys any instruction it is given; no network.

    It stands in for a model an attacker fully controls, so a test proves the guards, not the
    model, keep their promises. Every request is recorded in ``requests``.
    """

    def __init__(self, compromise: Compromise) -> None:
        """Act out ``compromise`` on every message."""
        self.compromise = compromise
        self.requests: list[ChatModelRequest] = []

    @property
    def calls(self) -> int:
        """Number of ``stream`` calls so far."""
        return len(self.requests)

    async def stream(self, request: ChatModelRequest) -> AsyncIterator[ChatModelEvent]:
        """Request the scripted tool call while due, else stream the compromised answer."""
        self.requests.append(request)
        spec = self.compromise
        current = _since_last_user(request.transcript)
        calls = sum(1 for item in current if isinstance(item, ToolCallItem))
        if spec.call_tool is not None and calls < spec.call_times:
            yield ToolUseRequest(
                id=f"{COMPROMISED_CALL_PREFIX}{len(self.requests)}",
                name=spec.call_tool,
                input=dict(spec.tool_arguments),
            )
            yield turn_end("tool_use")
            return
        parts = [spec.say] if spec.say else []
        if spec.leak_canary:
            parts.extend(CANARY_PATTERN.findall(request.system))
        if spec.repeat_tool_text:
            parts.extend(
                leaf
                for item in current
                if isinstance(item, ToolResultItem)
                for leaf in _leaves(item.payload)
            )
        yield TextDelta(text=" ".join(parts) or "OK.")
        yield turn_end("end_turn")
