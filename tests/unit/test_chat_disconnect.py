"""Client disconnects and deletes during a stream (US6, research §2, §12).

A stream stopped after its first ``token`` stops the model and the tools, stores the text already
sent as ``client_disconnected``, settles the tokens and frees the conversation; the transcript
keeps what the customer saw, so the next message continues validly. A delete cancels the answer
in progress first.
"""

import asyncio
from collections.abc import AsyncGenerator, Iterator
from uuid import UUID

import httpx
import pytest
import respx

from ecommerce_ai.agents.assistant.limits import ChatLimits
from ecommerce_ai.agents.assistant.model import (
    AssistantText,
    FinishReason,
    MessageRole,
    ToolResultItem,
    UserTurn,
)
from ecommerce_ai.agents.assistant.ports import TextDelta
from ecommerce_ai.agents.assistant.service import ChatService
from ecommerce_ai.agents.ports import SystemClock
from ecommerce_ai.api.sse import sse_frames
from ecommerce_ai.infrastructure.settings import Settings
from tests.support.chat import FakeToolRunner, chat_app, chat_graph
from tests.support.llm import ScriptedChatModel, TurnStep, text_turn, tool_turn, turn_end
from tests.support.polling import wait_until
from tests.support.sse import events_of, parse_sse
from tests.support.stores import InMemoryConversationStore, InMemoryTokenBudget
from tests.support.tokens import jwks, make_token
from tests.support.triage import NOW

USER = "user-1"
CAP = 5000
JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default
HEADERS = {"Authorization": f"Bearer {make_token(sub=USER)}"}


@pytest.fixture(autouse=True)
def identity() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        yield route


def _held_turn(gate: asyncio.Event) -> list[TurnStep]:
    """A turn that sends one segment, then waits on ``gate`` before the rest."""
    return [
        TextDelta(text="Your order is on its way. "),
        TextDelta(text="It left "),
        gate,
        TextDelta(text="today."),
        turn_end(),
    ]


async def _setup(
    model: ScriptedChatModel, tools: FakeToolRunner | None = None
) -> tuple[ChatService, InMemoryConversationStore, InMemoryTokenBudget, UUID]:
    store = InMemoryConversationStore()
    budget = InMemoryTokenBudget(1_000_000)
    service = ChatService(
        store,
        chat_graph(model, tools),
        SystemClock(),
        budget=budget,
        limits=ChatLimits(request_token_cap=CAP),
    )
    conversation = await store.create(USER, None, None, NOW)
    return service, store, budget, conversation.id


async def _until(frames: AsyncGenerator[str], event: str) -> list[str]:
    """Read frames up to and including the first ``event``."""
    seen: list[str] = []
    async for frame in frames:
        seen.append(frame)
        if f"event: {event}\n" in frame:
            return seen
    raise AssertionError(f"the stream ended before {event}")


async def _answer(service: ChatService, conversation_id: UUID, text: str) -> list[str]:
    run = await service.start(USER, conversation_id, text)
    assert run is not None
    return [frame async for frame in sse_frames(run.events(), keepalive_seconds=60)]


async def test_should_store_the_sent_text_and_free_the_conversation_when_the_client_leaves() -> (
    None
):
    gate = asyncio.Event()
    model = ScriptedChatModel([_held_turn(gate), text_turn("Anything else?")])
    service, store, budget, conversation_id = await _setup(model)
    run = await service.start(USER, conversation_id, "Where is my order?")
    assert run is not None

    frames = sse_frames(run.events(), keepalive_seconds=60)
    await _until(frames, "token")
    await frames.aclose()

    await wait_until(lambda: model.closed_early == 1)
    question, answer = store.messages[conversation_id]
    assert question.role is MessageRole.USER
    assert answer.role is MessageRole.ASSISTANT
    assert answer.id == run.message_id
    assert answer.finish_reason is FinishReason.CLIENT_DISCONNECTED
    assert answer.content == "Your order is on its way. "
    assert [(c.reserved, c.actual) for c in budget.settles] == [(CAP, 0)]
    assert not service.streams.is_active(conversation_id)
    assert model.calls == 1

    frames_after = await _answer(service, conversation_id, "Thanks")
    assert any("event: message.end" in frame for frame in frames_after)
    assert model.requests[1].transcript == (
        UserTurn(text="Where is my order?"),
        AssistantText(text="Your order is on its way. "),
        UserTurn(text="Thanks"),
    )


async def test_should_close_unfinished_tool_calls_when_the_client_leaves_during_a_tool() -> None:
    gate = asyncio.Event()
    model = ScriptedChatModel(
        [tool_turn(("toolu_01", "list_my_orders", {})), text_turn("Anything else?")]
    )
    tools = FakeToolRunner(gate=gate)
    service, store, budget, conversation_id = await _setup(model, tools)
    run = await service.start(USER, conversation_id, "Where is my order?")
    assert run is not None

    frames = sse_frames(run.events(), keepalive_seconds=60)
    await _until(frames, "tool.call")
    await frames.aclose()

    assert store.messages[conversation_id][-1].finish_reason is FinishReason.CLIENT_DISCONNECTED
    assert [(c.reserved, c.actual) for c in budget.settles] == [(CAP, 110)]
    await _answer(service, conversation_id, "Hello?")
    results = [i for i in model.requests[1].transcript if isinstance(i, ToolResultItem)]
    assert [(r.call_id, r.ok, r.payload["error"]) for r in results] == [
        ("toolu_01", False, "cancelled")
    ]
    assert len(tools.calls) == 1


async def test_should_free_the_conversation_when_the_stream_never_started() -> None:
    service, _, budget, conversation_id = await _setup(ScriptedChatModel())
    run = await service.start(USER, conversation_id, "Hi")
    assert run is not None
    assert service.streams.is_active(conversation_id)

    run.discard()

    assert not service.streams.is_active(conversation_id)
    assert budget.reserves == []


async def test_should_cancel_the_answer_in_progress_when_the_conversation_is_deleted() -> None:
    gate = asyncio.Event()
    model = ScriptedChatModel([_held_turn(gate)])
    service, store, _, conversation_id = await _setup(model)
    run = await service.start(USER, conversation_id, "Where is my order?")
    assert run is not None
    frames = sse_frames(run.events(), keepalive_seconds=60)
    await _until(frames, "token")

    assert not await service.delete("someone-else", conversation_id)
    assert service.streams.is_active(conversation_id)
    assert await service.delete(USER, conversation_id)

    rest = [frame async for frame in frames]
    assert not any("event: message.end" in f or "event: error" in f for f in rest)
    assert conversation_id not in store.messages
    assert not service.streams.is_active(conversation_id)
    assert model.closed_early == 1


async def test_should_answer_204_and_end_the_stream_when_deleting_during_an_answer() -> None:
    gate = asyncio.Event()
    model = ScriptedChatModel([_held_turn(gate)])
    store = InMemoryConversationStore()
    transport = httpx.ASGITransport(app=chat_app(model, store=store))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        created = await client.post("/chat/conversations", json={}, headers=HEADERS)
        path = f"/chat/conversations/{created.json()['id']}"
        posting = asyncio.create_task(
            client.post(f"{path}/messages", json={"content": "Where?"}, headers=HEADERS)
        )
        await wait_until(lambda: model.calls == 1)
        deleted = await client.delete(path, headers=HEADERS)
        streamed = await posting
        after = await client.get(path, headers=HEADERS)

    assert deleted.status_code == 204
    assert [e.event for e in events_of(parse_sse(streamed.text))][-1] != "message.end"
    assert after.status_code == 404
    assert model.closed_early == 1
