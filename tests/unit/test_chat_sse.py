"""SSE framing: frame format, increasing ids, keep-alive on silence, headers (research §1)."""

import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID

import pytest

from ecommerce_ai.agents.assistant.model import (
    ChatErrorCode,
    MessageStart,
    StreamError,
    StreamEvent,
    Token,
)
from ecommerce_ai.api.sse import KEEPALIVE, encode_event, sse_frames, sse_response
from tests.support.polling import wait_until
from tests.support.sse import SseComment, SseEvent, parse_sse

CONVERSATION = UUID("00000000-0000-4000-8000-0000000000c1")
MESSAGE = UUID("00000000-0000-4000-8000-0000000000a1")


async def _events(*events: StreamEvent) -> AsyncGenerator[StreamEvent]:
    for event in events:
        yield event


def test_should_frame_an_event_with_id_name_compact_json_and_a_blank_line() -> None:
    frame = encode_event(3, Token(text='Line one\nsaid "hi"'))

    assert frame == 'id: 3\nevent: token\ndata: {"text":"Line one\\nsaid \\"hi\\""}\n\n'


async def test_should_number_events_from_one_in_order() -> None:
    start = MessageStart(conversation_id=CONVERSATION, message_id=MESSAGE)
    events = _events(start, Token(text="A. "), StreamError.for_code(ChatErrorCode.INTERNAL))

    body = "".join([frame async for frame in sse_frames(events, keepalive_seconds=60)])

    items = parse_sse(body)
    assert [(i.id, i.event) for i in items if isinstance(i, SseEvent)] == [
        ("1", "message.start"),
        ("2", "token"),
        ("3", "error"),
    ]
    assert items[0] == SseEvent(
        id="1",
        event="message.start",
        data={"conversationId": str(CONVERSATION), "messageId": str(MESSAGE)},
    )


async def test_should_send_a_keep_alive_comment_while_the_run_is_silent() -> None:
    gate = asyncio.Event()

    async def slow() -> AsyncGenerator[StreamEvent]:
        yield Token(text="First. ")
        await gate.wait()
        yield Token(text="Second.")

    frames: list[str] = []

    async def consume() -> None:
        async for frame in sse_frames(slow(), keepalive_seconds=0.01):
            frames.append(frame)
            if frame == KEEPALIVE:
                gate.set()

    await asyncio.wait_for(consume(), timeout=5)

    items = parse_sse("".join(frames))
    assert [i.id for i in items if isinstance(i, SseEvent)] == ["1", "2"]
    assert SseComment("keep-alive") in items
    assert KEEPALIVE in frames


async def test_should_raise_the_runs_error_to_the_response() -> None:
    async def failing() -> AsyncGenerator[StreamEvent]:
        yield Token(text="A. ")
        raise RuntimeError("boom")

    frames: list[str] = []
    with pytest.raises(RuntimeError, match="boom"):
        async for frame in sse_frames(failing(), keepalive_seconds=60):
            frames.append(frame)
    assert len(frames) == 1


async def test_should_cancel_and_close_the_run_when_the_response_stops_early() -> None:
    closed = asyncio.Event()
    gate = asyncio.Event()

    async def endless() -> AsyncGenerator[StreamEvent]:
        try:
            yield Token(text="A. ")
            await gate.wait()
            yield Token(text="never")
        finally:
            closed.set()

    frames = sse_frames(endless(), keepalive_seconds=60)
    first = await anext(frames)
    await frames.aclose()

    assert first.startswith("id: 1\n")
    await wait_until(closed.is_set, timeout=5)


def test_should_answer_with_event_stream_headers() -> None:
    response = sse_response(_events(), keepalive_seconds=15)

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["content-type"].startswith("text/event-stream")
