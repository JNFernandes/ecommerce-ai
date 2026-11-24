"""Sanity checks for the chat test fakes: scripted model, SSE parser, store and tool runner."""

import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from ecommerce_ai.agents.assistant.model import FinishReason, MessageRole, Usage, UserTurn
from ecommerce_ai.agents.assistant.ports import (
    ChatModel,
    ChatModelRequest,
    ConversationStore,
    ModelTurnEnd,
    ModelUnavailable,
    TextDelta,
    ToolRunner,
    ToolUseRequest,
)
from tests.support.chat import FakeToolError, FakeToolRunner, envelope
from tests.support.llm import ScriptedChatModel, text_turn, tool_turn
from tests.support.polling import wait_until
from tests.support.sse import SseComment, SseEvent, events_of, parse_sse, read_sse
from tests.support.stores import InMemoryConversationStore

REQUEST = ChatModelRequest(system="system", transcript=(UserTurn(text="Hi"),), tools=())
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


async def _play(model: ChatModel) -> list[object]:
    return [event async for event in model.stream(REQUEST)]


async def test_should_play_scripted_turns_and_record_requests() -> None:
    model = ScriptedChatModel(
        [tool_turn(("t1", "get_order", {"orderId": "x"})), text_turn("Hel", "lo.")]
    )

    first = await _play(model)
    second = await _play(model)

    assert isinstance(first[0], ToolUseRequest)
    assert isinstance(first[-1], ModelTurnEnd)
    assert first[-1].stop_reason == "tool_use"
    assert [e.text for e in second if isinstance(e, TextDelta)] == ["Hel", "lo."]
    assert model.calls == 2
    assert model.requests == [REQUEST, REQUEST]


async def test_should_raise_before_or_after_the_first_delta_when_scripted() -> None:
    model = ScriptedChatModel(
        [[ModelUnavailable("down")], [TextDelta(text="a"), ModelUnavailable("x")]]
    )
    seen: list[object] = []

    with pytest.raises(ModelUnavailable):
        await _play(model)
    with pytest.raises(ModelUnavailable):
        async for event in model.stream(REQUEST):
            seen.append(event)

    assert seen == [TextDelta(text="a")]
    assert model.closed_early == 0


async def test_should_count_a_turn_closed_early_when_the_reader_stops() -> None:
    model = ScriptedChatModel([text_turn("a", "b")])
    stream = model.stream(REQUEST)

    await anext(stream)
    await stream.aclose()  # type: ignore[attr-defined]  # an async generator underneath

    assert model.closed_early == 1


async def test_should_hold_the_stream_until_the_gate_opens() -> None:
    gate = asyncio.Event()
    model = ScriptedChatModel([[TextDelta(text="a"), gate, *text_turn("b")]])
    task = asyncio.create_task(_play(model))

    await wait_until(lambda: model.calls == 1)
    assert not task.done()
    gate.set()

    assert len(await asyncio.wait_for(task, 1)) == 3


async def test_should_fail_loudly_when_the_script_runs_out() -> None:
    with pytest.raises(AssertionError, match="more often than scripted"):
        await _play(ScriptedChatModel())


def test_should_parse_events_and_comments_when_reading_an_sse_body() -> None:
    body = (
        'id: 1\nevent: message.start\ndata: {"messageId":"m"}\n\n'
        ": keep-alive\n\n"
        'id: 2\r\nevent: token\r\ndata: {"text":"Hi."}\r\n\r\n'
        "data: 3"
    )

    items = parse_sse(body)

    assert items == [
        SseEvent(id="1", event="message.start", data={"messageId": "m"}),
        SseComment("keep-alive"),
        SseEvent(id="2", event="token", data={"text": "Hi."}),
        SseEvent(id=None, event="message", data=3),
    ]
    assert [e.event for e in events_of(items)] == ["message.start", "token", "message"]


async def test_should_stop_reading_a_stream_when_asked() -> None:
    body = 'id: 1\nevent: token\ndata: {"text":"a"}\n\nid: 2\nevent: token\ndata: {"text":"b"}\n\n'

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client,
        client.stream("GET", "http://test/") as response,
    ):
        first = await read_sse(response, stop=lambda event: event.event == "token")
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client,
        client.stream("GET", "http://test/") as response,
    ):
        everything = await read_sse(response)

    assert len(first) == 1
    assert len(everything) == 2


async def test_should_scope_conversations_by_user_in_the_memory_store() -> None:
    store: ConversationStore = InMemoryConversationStore()
    older = await store.create("user-1", None, "Old", NOW)
    newer = await store.create("user-1", None, None, NOW + timedelta(minutes=1))
    await store.create("user-2", None, None, NOW)

    await store.add_user_message("user-1", older.id, uuid4(), "Hi", NOW + timedelta(minutes=2))
    await store.add_assistant_message(
        "user-1",
        older.id,
        uuid4(),
        "Hello.",
        FinishReason.COMPLETED,
        "assistant.v1",
        None,
        Usage(),
        NOW + timedelta(minutes=3),
    )
    page = await store.list("user-1", limit=10, offset=0)
    history = await store.get("user-1", older.id)

    assert [c.id for c in page.items] == [older.id, newer.id]
    assert page.total == 2
    assert history is not None
    assert [m.role for m in history.messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert await store.count_messages("user-1", older.id) == 2
    assert await store.get("user-2", older.id) is None
    assert await store.count_messages("user-2", older.id) is None
    assert not await store.delete("user-2", older.id)
    with pytest.raises(LookupError):
        await store.add_user_message("user-2", older.id, uuid4(), "x", NOW)
    assert await store.delete("user-1", older.id)
    assert await store.get("user-1", older.id) is None


async def test_should_raise_the_queued_failure_once_in_the_memory_store() -> None:
    store = InMemoryConversationStore()
    store.failures["create"] = deque([OSError("db down")])

    with pytest.raises(OSError, match="db down"):
        await store.create("user-1", None, None, NOW)

    assert (await store.create("user-1", None, None, NOW)).title is None


async def test_should_answer_scripted_results_and_refuse_unknown_tools() -> None:
    runner: ToolRunner = FakeToolRunner(
        results={
            "get_order": envelope({"status": "SHIPPED"}),
            "list_my_orders": FakeToolError(upstream_unauthenticated=True),
        }
    )

    found = await runner.run("get_order", {"orderId": "x"})
    failed = await runner.run("list_my_orders", {})
    empty = await runner.run("list_products", {})
    refused = await runner.run("cancel_order", {})

    assert found.ok
    assert found.payload["trust"] == "untrusted_data"
    assert (failed.ok, failed.upstream_unauthenticated) == (False, True)
    assert empty.payload["data"] == []
    assert (refused.refused, refused.outcome) == (True, "unknown_tool")
    assert len(runner.tool_specs()) == 5
    assert isinstance(runner, FakeToolRunner)
    assert [call.name for call in runner.calls] == ["get_order", "list_my_orders", "list_products"]
