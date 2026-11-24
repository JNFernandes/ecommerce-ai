"""Follow-up questions see the conversation so far, also after a restart; nothing is shared
between conversations, and deleting one forgets its state (FR-004, FR-014)."""

from collections.abc import Iterator

import httpx
import pytest
import respx
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from ecommerce_ai.agents.assistant.model import (
    AssistantText,
    ToolCallItem,
    ToolResultItem,
    UserTurn,
)
from ecommerce_ai.infrastructure.settings import Settings
from tests.support.chat import FakeToolRunner, chat_app, envelope
from tests.support.llm import ScriptedChatModel, text_turn, tool_turn
from tests.support.stores import InMemoryConversationStore
from tests.support.tokens import jwks, make_token

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default
HEADERS = {"Authorization": f"Bearer {make_token(sub='user-1')}"}
ORDERS = envelope({"items": [{"id": "o-1", "status": "SHIPPED"}], "totalCount": 1})


@pytest.fixture(autouse=True)
def identity() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        yield route


def _client(
    model: ScriptedChatModel, store: InMemoryConversationStore, saver: InMemorySaver
) -> httpx.AsyncClient:
    tools = FakeToolRunner(results={"list_my_orders": ORDERS})
    transport = httpx.ASGITransport(app=chat_app(model, tools, store, saver))
    return httpx.AsyncClient(transport=transport, base_url="http://localhost")


async def _create(client: httpx.AsyncClient) -> str:
    response = await client.post("/chat/conversations", json={}, headers=HEADERS)
    conversation_id = response.json()["id"]
    assert isinstance(conversation_id, str)
    return conversation_id


async def _ask(client: httpx.AsyncClient, conversation_id: str, content: str) -> None:
    response = await client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": content},
        headers=HEADERS,
    )
    assert response.status_code == 200


def _config(conversation_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": conversation_id}}


async def test_should_send_the_first_exchange_with_the_follow_up_after_a_restart() -> None:
    store = InMemoryConversationStore()
    saver = InMemorySaver()
    first_model = ScriptedChatModel(
        [tool_turn(("toolu_01", "list_my_orders", {})), text_turn("Order o-1 is SHIPPED.")]
    )
    async with _client(first_model, store, saver) as client:
        conversation_id = await _create(client)
        await _ask(client, conversation_id, "Where is my last order?")

    second_model = ScriptedChatModel([text_turn("It was o-1.")])
    async with _client(second_model, store, saver) as client:  # a rebuilt app, same saver
        await _ask(client, conversation_id, "Which one was it?")

    (request,) = second_model.requests
    assert request.transcript == (
        UserTurn(text="Where is my last order?"),
        ToolCallItem(call_id="toolu_01", name="list_my_orders", arguments={}),
        ToolResultItem(call_id="toolu_01", name="list_my_orders", ok=True, payload=ORDERS),
        AssistantText(text="Order o-1 is SHIPPED."),
        UserTurn(text="Which one was it?"),
    )


async def test_should_not_share_state_between_two_conversations_of_one_user() -> None:
    store = InMemoryConversationStore()
    saver = InMemorySaver()
    model = ScriptedChatModel([text_turn("Answer one."), text_turn("Answer two.")])
    async with _client(model, store, saver) as client:
        first = await _create(client)
        second = await _create(client)
        await _ask(client, first, "Question one?")
        await _ask(client, second, "Question two?")

    assert model.requests[1].transcript == (UserTurn(text="Question two?"),)


async def test_should_forget_the_state_when_the_conversation_is_deleted() -> None:
    store = InMemoryConversationStore()
    saver = InMemorySaver()
    model = ScriptedChatModel([text_turn("Answer.")])
    async with _client(model, store, saver) as client:
        conversation_id = await _create(client)
        await _ask(client, conversation_id, "Question?")
        assert await saver.aget_tuple(_config(conversation_id)) is not None

        response = await client.delete(f"/chat/conversations/{conversation_id}", headers=HEADERS)

    assert response.status_code == 204
    assert await saver.aget_tuple(_config(conversation_id)) is None
