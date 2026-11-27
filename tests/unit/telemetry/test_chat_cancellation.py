"""A reader leaving a chat stream is not a failed run: only real exceptions mark ``chat.run``.

Cancelled after ``message.end`` → ``OK`` with ``stream.client_disconnected``; cancelled mid-run →
``UNSET`` with ``chat.aborted``; neither writes an error ``llm_calls`` row. A real exception keeps
``ERROR`` with the class name only.
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Literal

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode

from ecommerce_ai.agents.assistant.graph import ChatGraph, load_assistant_prompt
from ecommerce_ai.agents.assistant.model import MessageEnd, MessageStart, StreamEvent
from ecommerce_ai.agents.assistant.service import ChatService
from ecommerce_ai.agents.ports import LlmCallOutcome
from ecommerce_ai.agents.usage import LlmUsage
from ecommerce_ai.telemetry import attributes
from tests.support.approvals import OWNER
from tests.support.chat import FakeToolRunner
from tests.support.llm import ScriptedChatModel, TurnStep, text_turn
from tests.support.polling import wait_until
from tests.support.stores import InMemoryConversationStore, InMemoryLlmCallRecorder
from tests.support.tracing import SpanRecorder
from tests.support.triage import NOW, FakeClock

PRIVATE_TEXT = "jane@example.com secret-XYZ"


class _Hold(asyncio.Event):
    """An event that remembers that the model stream started waiting on it."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting = False

    async def wait(self) -> Literal[True]:
        self.waiting = True
        return await super().wait()


async def _stream(
    turn: list[TurnStep], recorder: InMemoryLlmCallRecorder
) -> AsyncGenerator[StreamEvent]:
    """The events of one chat run answering with ``turn``, recorded into ``recorder``."""
    store = InMemoryConversationStore()
    graph = ChatGraph(
        model=ScriptedChatModel([turn]),
        tools=FakeToolRunner(),
        prompt=load_assistant_prompt(),
        checkpointer=InMemorySaver(),
        usage=LlmUsage(recorder),
    )
    service = ChatService(store, graph, FakeClock())
    conversation = await store.create(OWNER, None, None, NOW)
    run = await service.start(OWNER, conversation.id, "Where is my order?")
    assert run is not None
    return run.events()


def _run_span(spans: SpanRecorder) -> ReadableSpan:
    return spans.one("chat.run")


async def _until_end(stream: AsyncGenerator[StreamEvent]) -> None:
    async for event in stream:
        if isinstance(event, MessageEnd):
            return
    raise AssertionError("the run sent no message.end")


async def test_should_end_ok_when_the_run_completes(spans: SpanRecorder) -> None:
    stream = await _stream(text_turn("Shipped."), InMemoryLlmCallRecorder())

    _ = [event async for event in stream]

    run = _run_span(spans)
    assert run.status.status_code is StatusCode.OK
    assert attributes.STREAM_CLIENT_DISCONNECTED not in (run.attributes or {})


async def test_should_end_ok_and_flag_the_disconnect_when_cancelled_after_message_end(
    spans: SpanRecorder,
) -> None:
    stream = await _stream(text_turn("Shipped."), InMemoryLlmCallRecorder())
    await _until_end(stream)

    with pytest.raises(asyncio.CancelledError):
        await stream.athrow(asyncio.CancelledError())  # the SSE task is cancelled

    run = _run_span(spans)
    assert run.status.status_code is StatusCode.OK
    assert (run.attributes or {})[attributes.STREAM_CLIENT_DISCONNECTED] is True
    assert attributes.ERROR_TYPE not in (run.attributes or {})


async def test_should_end_ok_and_flag_the_disconnect_when_closed_after_message_end(
    spans: SpanRecorder,
) -> None:
    stream = await _stream(text_turn("Shipped."), InMemoryLlmCallRecorder())
    await _until_end(stream)

    await stream.aclose()

    run = _run_span(spans)
    assert run.status.status_code is StatusCode.OK
    assert (run.attributes or {})[attributes.STREAM_CLIENT_DISCONNECTED] is True


async def test_should_mark_the_run_aborted_not_failed_when_cancelled_mid_run(
    spans: SpanRecorder,
) -> None:
    hold = _Hold()
    recorder = InMemoryLlmCallRecorder()
    stream = await _stream([*text_turn("Your order")[:-1], hold], recorder)
    assert isinstance(await anext(stream), MessageStart)
    step = asyncio.create_task(anext(stream))  # the segmenter holds the text: waits in the model
    await wait_until(lambda: hold.waiting)

    step.cancel()  # the client aborted while the model was still answering
    with pytest.raises(asyncio.CancelledError):
        await step

    run = _run_span(spans)
    assert run.status.status_code is StatusCode.UNSET
    assert (run.attributes or {})[attributes.CHAT_ABORTED] is True
    assert attributes.ERROR_TYPE not in (run.attributes or {})
    for child in spans.spans:
        assert child.status.status_code is not StatusCode.ERROR, child.name
    failed = {LlmCallOutcome.ERROR, LlmCallOutcome.TIMEOUT}
    assert [call for call in recorder.calls if call.outcome in failed] == []


async def test_should_mark_the_run_failed_by_class_name_when_a_real_exception_escapes(
    spans: SpanRecorder,
) -> None:
    stream = await _stream(text_turn("Shipped."), InMemoryLlmCallRecorder())
    assert isinstance(await anext(stream), MessageStart)

    with pytest.raises(RuntimeError):
        await stream.athrow(RuntimeError(PRIVATE_TEXT))

    run = _run_span(spans)
    assert run.status.status_code is StatusCode.ERROR
    assert not run.status.description
    assert (run.attributes or {})[attributes.ERROR_TYPE] == "RuntimeError"
    assert attributes.CHAT_ABORTED not in (run.attributes or {})
    assert run.events == ()
    assert PRIVATE_TEXT not in str(run.to_json())
