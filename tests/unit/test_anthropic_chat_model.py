"""The Anthropic adapter of ``ChatModel``, driven through a fake streaming client (no network)."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.lib.streaming import TextEvent
from anthropic.types import Message
from pydantic import JsonValue, SecretStr

from ecommerce_ai.agents.assistant.model import (
    AssistantText,
    ThinkingItem,
    ToolCallItem,
    ToolResultItem,
    Usage,
    UserTurn,
)
from ecommerce_ai.agents.assistant.ports import (
    ChatModel,
    ChatModelEvent,
    ChatModelRequest,
    ModelTimeout,
    ModelTurnEnd,
    ModelUnavailable,
    TextDelta,
    ToolSpec,
    ToolUseRequest,
)
from ecommerce_ai.agents.assistant.tool_results import tool_result_text
from ecommerce_ai.infrastructure.llm.anthropic_chat_model import (
    AnthropicChatModel,
    build_anthropic_chat_model,
    to_messages,
)
from ecommerce_ai.infrastructure.settings import LlmEffort, Settings

MODEL = "claude-test-model"
REQUEST = httpx2.Request("POST", "https://api.anthropic.test/v1/messages")
SPEC = ToolSpec(
    name="list_my_orders",
    description="List my orders.",
    input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
)
THINKING: dict[str, JsonValue] = {"type": "thinking", "thinking": "", "signature": "sig-1"}


def _message(content: list[dict[str, Any]], stop_reason: str = "end_turn") -> Message:
    # Any: raw content blocks of mixed shapes, validated by the SDK model.
    return Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 300, "output_tokens": 40},
        }
    )


class FakeStream:
    """Yields the scripted events, then returns the final message (or raises)."""

    def __init__(self, events: Sequence[object | Exception], final: Message) -> None:
        self.events = events
        self.final = final
        self.read = 0

    async def __aiter__(self) -> AsyncIterator[object]:
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            self.read += 1
            yield event

    async def get_final_message(self) -> Message:
        return self.final


class FakeMessages:
    """Records each ``stream`` call; ``open_error`` fails before the stream opens."""

    def __init__(self, stream: FakeStream, open_error: Exception | None = None) -> None:
        self.stream_obj = stream
        self.open_error = open_error
        self.calls: list[dict[str, Any]] = []  # Any: recorded keyword arguments of mixed types
        self.closed = False

    @asynccontextmanager
    async def stream(self, **kwargs: Any) -> AsyncIterator[FakeStream]:  # noqa: ANN401
        self.calls.append(kwargs)
        if self.open_error is not None:
            raise self.open_error
        try:
            yield self.stream_obj
        finally:
            self.closed = True


def _text(delta: str) -> TextEvent:
    return TextEvent(type="text", text=delta, snapshot=delta)


def _model(messages: FakeMessages) -> AnthropicChatModel:
    return AnthropicChatModel(
        messages,  # a structural fake of the SDK resource
        model=MODEL,
        max_output_tokens=1024,
        timeout_seconds=12.5,
        effort=LlmEffort.LOW,
    )


async def _collect(model: ChatModel, request: ChatModelRequest) -> list[ChatModelEvent]:
    return [event async for event in model.stream(request)]


def _request(*transcript: Any) -> ChatModelRequest:  # noqa: ANN401  # transcript items
    return ChatModelRequest(
        system="You are the shop assistant.",
        transcript=transcript or (UserTurn(text="Hi"),),
        tools=(SPEC,),
    )


async def test_should_stream_text_deltas_then_tool_uses_then_the_turn_end() -> None:
    final = _message(
        [
            THINKING,
            {"type": "text", "text": "Let me check. "},
            {"type": "tool_use", "id": "toolu_01", "name": "list_my_orders", "input": {"limit": 5}},
        ],
        stop_reason="tool_use",
    )
    messages = FakeMessages(FakeStream([_text("Let me "), object(), _text("check. ")], final))

    events = await _collect(_model(messages), _request())

    assert events == [
        TextDelta(text="Let me "),
        TextDelta(text="check. "),
        ToolUseRequest(id="toolu_01", name="list_my_orders", input={"limit": 5}),
        ModelTurnEnd(
            stop_reason="tool_use",
            usage=Usage(input_tokens=300, output_tokens=40),
            model_id=MODEL,
            content=(
                THINKING,
                {"type": "text", "text": "Let me check. "},
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "list_my_orders",
                    "input": {"limit": 5},
                },
            ),
        ),
    ]


async def test_should_call_the_api_with_limits_effort_system_and_registry_tools() -> None:
    messages = FakeMessages(FakeStream([], _message([{"type": "text", "text": "Hi."}])))

    await _collect(_model(messages), _request())

    (call,) = messages.calls
    assert call["model"] == MODEL
    assert call["max_tokens"] == 1024
    assert call["timeout"] == 12.5
    assert call["output_config"] == {"effort": "low"}
    assert call["system"] == "You are the shop assistant."
    assert call["tools"] == [
        {"name": SPEC.name, "description": SPEC.description, "input_schema": SPEC.input_schema}
    ]
    assert call["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}]


def test_should_merge_same_role_blocks_and_delimit_tool_results() -> None:
    result = ToolResultItem(call_id="toolu_01", name="get_order", ok=False, payload={"error": "x"})
    transcript = (
        UserTurn(text="First?"),
        UserTurn(text="Order o-1?"),
        ThinkingItem(block=THINKING),
        AssistantText(text=""),
        AssistantText(text="Checking. "),
        ToolCallItem(call_id="toolu_01", name="get_order", arguments={"orderId": "o-1"}),
        result,
    )

    assert to_messages(transcript) == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "First?"},
                {"type": "text", "text": "Order o-1?"},
            ],
        },
        {
            "role": "assistant",
            "content": [
                THINKING,
                {"type": "text", "text": "Checking. "},
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "get_order",
                    "input": {"orderId": "o-1"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01",
                    "content": tool_result_text(result),
                    "is_error": True,
                }
            ],
        },
    ]


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APITimeoutError(request=REQUEST),
        anthropic.APIConnectionError(request=REQUEST),
        anthropic.InternalServerError(
            "overloaded", response=httpx2.Response(529, request=REQUEST), body=None
        ),
    ],
)
async def test_should_raise_unavailable_when_the_api_fails_before_the_first_delta(
    error: anthropic.APIError,
) -> None:
    messages = FakeMessages(FakeStream([], _message([])), open_error=error)

    with pytest.raises(ModelUnavailable):
        await _collect(_model(messages), _request())


async def test_should_raise_timeout_when_the_api_times_out() -> None:
    error = anthropic.APITimeoutError(request=REQUEST)
    messages = FakeMessages(FakeStream([], _message([])), open_error=error)

    with pytest.raises(ModelTimeout):
        await _collect(_model(messages), _request())


async def test_should_raise_unavailable_when_the_stream_breaks_after_output() -> None:
    stream = FakeStream(
        [_text("Your "), anthropic.APIConnectionError(request=REQUEST)], _message([])
    )
    seen: list[ChatModelEvent] = []

    with pytest.raises(ModelUnavailable):
        async for event in _model(FakeMessages(stream)).stream(_request()):
            seen.append(event)

    assert seen == [TextDelta(text="Your ")]


async def test_should_close_the_http_stream_when_the_consumer_stops_reading() -> None:
    final = _message([{"type": "text", "text": "A. B."}])
    messages = FakeMessages(FakeStream([_text("A. "), _text("B.")], final))
    events = _model(messages).stream(_request())

    assert await anext(events) == TextDelta(text="A. ")
    await events.aclose()  # type: ignore[attr-defined]  # an async generator

    assert messages.closed is True
    assert messages.stream_obj.read == 1


def test_should_build_the_chat_model_on_the_shared_client_only_when_configured() -> None:
    settings = Settings(anthropic_api_key=SecretStr("k"), anthropic_model=MODEL)
    client = anthropic.AsyncAnthropic(api_key="k", max_retries=0)

    assert isinstance(build_anthropic_chat_model(settings, client), AnthropicChatModel)
    with pytest.raises(ValueError, match="not configured"):
        build_anthropic_chat_model(Settings(), client)
