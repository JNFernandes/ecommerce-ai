"""``ChatModel`` over the Anthropic Messages API, streamed (research §6, FR-011, FR-013).

One ``messages.stream`` call per turn with the system prompt, the transcript and the registry's
tools; ``max_tokens``, the per-call timeout and the effort come from settings, and the SDK's own
retries are off (``max_retries=0`` on the client) so every attempt passes the budget check. Text
deltas are yielded as they arrive; then each ``tool_use`` block of the final message and one
``ModelTurnEnd`` carrying stop reason, usage, model id and the turn's ordered content (thinking
blocks included, so they are replayed unchanged on the next turn). Tool results are sent as
``tool_result`` blocks holding the delimited text of ``tool_result_text``. Any API error, before
or after the first delta, is ``ModelUnavailable``; logs carry the error class only (FR-020).
Closing the iterator early closes the HTTP stream.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Final, Protocol, cast

import anthropic
import structlog
from anthropic.lib.streaming import TextEvent
from anthropic.types import (
    AssistantMessageParam,
    ContentBlockParam,
    Message,
    MessageParam,
    OutputConfigParam,
    ToolParam,
    UserMessageParam,
)
from pydantic import JsonValue

from ecommerce_ai.agents.assistant.model import (
    AssistantText,
    ThinkingItem,
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
    ModelUnavailable,
    TextDelta,
    ToolSpec,
    ToolUseRequest,
)
from ecommerce_ai.agents.assistant.tool_results import tool_result_text
from ecommerce_ai.infrastructure.settings import LlmEffort, Settings

logger = structlog.get_logger(__name__)

_THINKING_BLOCKS: Final = frozenset({"thinking", "redacted_thinking"})


class MessageStreamLike(Protocol):
    """The part of the SDK's ``AsyncMessageStream`` the adapter uses."""

    def __aiter__(self) -> AsyncIterator[object]:
        """The stream's events (``TextEvent`` carries a text delta)."""
        ...

    async def get_final_message(self) -> Message:
        """The complete message once the stream has ended."""
        ...


class MessagesStreamer(Protocol):
    """The part of ``AsyncAnthropic().messages`` the adapter uses (a fake in unit tests)."""

    def stream(
        self,
        *,
        max_tokens: int,
        messages: list[MessageParam],
        model: str,
        output_config: OutputConfigParam,
        system: str,
        tools: list[ToolParam],
        timeout: float,
    ) -> AbstractAsyncContextManager[MessageStreamLike]:
        """Open one streamed Messages call."""
        ...


def _block(item: TranscriptItem) -> tuple[str, ContentBlockParam]:
    """The role and content block of one transcript item."""
    if isinstance(item, UserTurn):
        return "user", {"type": "text", "text": item.text}
    if isinstance(item, AssistantText):
        return "assistant", {"type": "text", "text": item.text}
    if isinstance(item, ThinkingItem):
        # Replayed exactly as the model returned it (preserved thinking).
        return "assistant", cast("ContentBlockParam", dict(item.block))
    if isinstance(item, ToolCallItem):
        return "assistant", {
            "type": "tool_use",
            "id": item.call_id,
            "name": item.name,
            "input": dict(item.arguments),
        }
    result: ToolResultItem = item
    return "user", {
        "type": "tool_result",
        "tool_use_id": result.call_id,
        "content": tool_result_text(result),
        "is_error": not result.ok,
    }


def to_messages(transcript: Sequence[TranscriptItem]) -> list[MessageParam]:
    """The Messages API conversation of ``transcript``; consecutive same-role blocks merge."""
    turns: list[tuple[str, list[ContentBlockParam]]] = []
    for item in transcript:
        if isinstance(item, AssistantText) and not item.text:
            continue  # the API rejects empty text blocks
        role, block = _block(item)
        if turns and turns[-1][0] == role:
            turns[-1][1].append(block)
        else:
            turns.append((role, [block]))
    messages: list[MessageParam] = []
    for role, blocks in turns:
        if role == "user":
            messages.append(UserMessageParam(role="user", content=blocks))
        else:
            messages.append(AssistantMessageParam(role="assistant", content=blocks))
    return messages


def to_tools(specs: Sequence[ToolSpec]) -> list[ToolParam]:
    """The tool definitions offered to the model: exactly the registry's."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": cast("dict[str, object]", dict(spec.input_schema)),
        }
        for spec in specs
    ]


def _content(message: Message) -> tuple[dict[str, JsonValue], ...]:
    """The final message's thinking, text and tool-use blocks in order, as plain JSON."""
    blocks: list[dict[str, JsonValue]] = []
    for block in message.content:
        if block.type in _THINKING_BLOCKS:
            blocks.append(block.model_dump(mode="json", exclude_none=True))
        elif block.type == "text":
            blocks.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            blocks.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": cast("JsonValue", block.input),
                }
            )
    return tuple(blocks)


class AnthropicChatModel:
    """The chat model, one streamed Messages API call per turn."""

    def __init__(
        self,
        messages: MessagesStreamer,
        *,
        model: str,
        max_output_tokens: int,
        timeout_seconds: float,
        effort: LlmEffort,
    ) -> None:
        """Call ``model`` through ``messages`` with the given limits."""
        self._messages = messages
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        self._effort = effort

    async def stream(self, request: ChatModelRequest) -> AsyncIterator[ChatModelEvent]:
        """Stream one turn, ending with ``ModelTurnEnd``; ``ModelUnavailable`` on API errors."""
        try:
            async with self._messages.stream(
                max_tokens=self._max_output_tokens,
                messages=to_messages(request.transcript),
                model=self._model,
                output_config={"effort": self._effort.value},
                system=request.system,
                tools=to_tools(request.tools),
                timeout=self._timeout_seconds,
            ) as stream:
                async for event in stream:
                    if isinstance(event, TextEvent) and event.text:
                        yield TextDelta(text=event.text)
                message = await stream.get_final_message()
        except anthropic.APIError as error:
            logger.warning("llm_call_failed", error_type=type(error).__name__)
            raise ModelUnavailable("the model did not answer") from None
        for block in message.content:
            if block.type == "tool_use":
                yield ToolUseRequest(
                    id=block.id, name=block.name, input=cast("dict[str, JsonValue]", block.input)
                )
        usage = Usage(
            input_tokens=message.usage.input_tokens, output_tokens=message.usage.output_tokens
        )
        logger.info(
            "llm_call",
            model_id=message.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            stop_reason=message.stop_reason,
        )
        yield ModelTurnEnd(
            stop_reason=message.stop_reason or "end_turn",
            usage=usage,
            model_id=message.model,
            content=_content(message),
        )


def build_anthropic_chat_model(
    settings: Settings, client: anthropic.AsyncAnthropic
) -> AnthropicChatModel:
    """The chat model on ``client`` (shared with triage, built with ``max_retries=0``)."""
    if settings.anthropic_model is None:
        raise ValueError("the LLM is not configured")
    return AnthropicChatModel(
        client.messages,
        model=settings.anthropic_model,
        max_output_tokens=settings.llm_max_output_tokens,
        timeout_seconds=settings.llm_timeout_seconds,
        effort=settings.llm_effort,
    )
