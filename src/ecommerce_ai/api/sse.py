"""Server-Sent Events framing of a chat run (contract, research §1).

Each event is ``id: <n>`` (1, 2, 3… per stream), ``event: <name>``, ``data: <JSON>`` and a blank
line. The run is pumped by one task into a one-slot queue, so the run keeps a single context and
the framer can wait for the next event with a timeout: after ``keepalive_seconds`` of silence a
``: keep-alive`` comment is sent (FR-008). When the response stops early (the client went away),
the pump task is cancelled: the run sees a ``CancelledError`` (or ``GeneratorExit``) and ends
itself, shielded (research §2). When the run is cancelled from elsewhere (a delete), the stream
ends without a terminal event. ``on_close`` runs once the response is over, also when it never
started streaming (so a run that never began frees its conversation's slot).
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Callable
from typing import Final

from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from ecommerce_ai.agents.assistant.model import StreamEvent

MEDIA_TYPE: Final = "text/event-stream"
HEADERS: Final = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
KEEPALIVE: Final = ": keep-alive\n\n"


class _Done:
    """Marks the end of the run in the queue."""


_DONE: Final = _Done()


def encode_event(event_id: int, event: StreamEvent) -> str:
    """One SSE frame of ``event``; the JSON is compact and on a single line."""
    data = json.dumps(event.data(), separators=(",", ":"), ensure_ascii=False)
    return f"id: {event_id}\nevent: {event.EVENT}\ndata: {data}\n\n"


async def sse_frames(
    events: AsyncGenerator[StreamEvent], keepalive_seconds: float
) -> AsyncGenerator[str]:
    """Frame ``events`` with increasing ids, sending a keep-alive comment on silence."""
    queue: asyncio.Queue[StreamEvent | _Done | BaseException] = asyncio.Queue(maxsize=1)

    async def pump() -> None:
        try:
            async with contextlib.aclosing(events):
                async for event in events:
                    await queue.put(event)
        except asyncio.CancelledError:
            # Wake the framer, dropping an event it has not taken: the run was cancelled.
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(_DONE)
            raise
        except Exception as error:
            await queue.put(error)
            return
        await queue.put(_DONE)

    task = asyncio.create_task(pump(), name="chat-stream")
    try:
        event_id = 0
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=keepalive_seconds)
            except TimeoutError:
                yield KEEPALIVE
                continue
            if isinstance(item, _Done):
                break
            if isinstance(item, BaseException):
                raise item
            event_id += 1
            yield encode_event(event_id, item)
    finally:
        task.cancel()
        await asyncio.wait({task})


class _EventStreamResponse(StreamingResponse):
    """A streaming response that calls ``on_close`` when it is over, however it ended."""

    def __init__(self, frames: AsyncGenerator[str], on_close: Callable[[], None]) -> None:
        super().__init__(frames, media_type=MEDIA_TYPE, headers=HEADERS)
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._on_close()


def sse_response(
    events: AsyncGenerator[StreamEvent],
    keepalive_seconds: float,
    on_close: Callable[[], None] = lambda: None,
) -> StreamingResponse:
    """A ``text/event-stream`` response streaming ``events``; ``on_close`` runs at its end."""
    return _EventStreamResponse(sse_frames(events, keepalive_seconds), on_close)
