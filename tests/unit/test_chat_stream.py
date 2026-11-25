"""``POST /chat/conversations/{id}/messages``: the SSE answer end to end on fakes (US1)."""

from collections import deque
from collections.abc import Iterator, Mapping
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from pydantic import JsonValue
from sqlalchemy.exc import OperationalError

from ecommerce_ai.agents.assistant.model import FinishReason, MessageRole
from ecommerce_ai.agents.assistant.ports import ChatModel, ToolOutcome
from ecommerce_ai.infrastructure.caller import current_caller
from ecommerce_ai.infrastructure.settings import Settings
from tests.support.chat import FakeToolRunner, chat_app, envelope
from tests.support.llm import MODEL_ID, ScriptedChatModel, text_turn, tool_turn
from tests.support.sse import SseEvent, events_of, parse_sse
from tests.support.stores import InMemoryConversationStore
from tests.support.tokens import jwks, make_token
from tests.support.triage import NOW

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default
TOKEN = make_token(sub="user-1")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
ORDERS = envelope({"items": [{"id": "o-1", "status": "SHIPPED"}], "totalCount": 1})


@pytest.fixture(autouse=True)
def identity() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        yield route


def _client(
    model: ChatModel | None,
    store: InMemoryConversationStore,
    tools: FakeToolRunner | None = None,
) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=chat_app(model, tools, store))
    return httpx.AsyncClient(transport=transport, base_url="http://localhost")


async def _conversation(client: httpx.AsyncClient) -> str:
    response = await client.post("/chat/conversations", json={}, headers=HEADERS)
    conversation_id = response.json()["id"]
    assert isinstance(conversation_id, str)
    return conversation_id


async def _post(client: httpx.AsyncClient, conversation_id: str, content: str) -> httpx.Response:
    return await client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": content},
        headers=HEADERS,
    )


def _events(response: httpx.Response) -> list[SseEvent]:
    return events_of(parse_sse(response.text))


async def test_should_stream_the_contract_sequence_and_store_both_messages() -> None:
    store = InMemoryConversationStore()
    model = ScriptedChatModel(
        [
            tool_turn(("toolu_01", "list_my_orders", {"limit": 5})),
            text_turn("Your last order is SHIPPED."),
        ]
    )
    tools = FakeToolRunner(results={"list_my_orders": ORDERS})
    async with _client(model, store, tools) as client:
        conversation_id = await _conversation(client)
        response = await _post(client, conversation_id, "  Where is my last order?  ")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    events = _events(response)
    assert [(e.id, e.event) for e in events] == [
        ("1", "message.start"),
        ("2", "tool.call"),
        ("3", "tool.result"),
        ("4", "token"),
        ("5", "message.end"),
    ]
    start, call, result, token, end = (e.data for e in events)
    assert isinstance(start, dict)
    assert start["conversationId"] == conversation_id
    assert call == {"callId": "toolu_01", "name": "list_my_orders", "arguments": {"limit": 5}}
    assert result == {
        "callId": "toolu_01",
        "name": "list_my_orders",
        "ok": True,
        "summary": "1 order found",
    }
    assert token == {"text": "Your last order is SHIPPED."}
    assert end == {
        "messageId": start["messageId"],
        "finishReason": "completed",
        "promptVersion": "assistant.v1",
        "modelId": MODEL_ID,
        "usage": {"inputTokens": 200, "outputTokens": 30},
    }
    question, answer = store.messages[UUID(conversation_id)]
    assert (question.role, question.content) == (MessageRole.USER, "Where is my last order?")
    assert answer.role is MessageRole.ASSISTANT
    assert str(answer.id) == start["messageId"]
    assert answer.content == "Your last order is SHIPPED."
    assert answer.finish_reason is FinishReason.COMPLETED
    assert answer.prompt_version == "assistant.v1"


async def test_should_answer_404_and_store_nothing_when_the_conversation_is_not_mine() -> None:
    store = InMemoryConversationStore()
    foreign = await store.create("user-2", None, None, NOW)
    async with _client(ScriptedChatModel(), store) as client:
        responses = [
            await _post(client, target, "Hi") for target in (str(foreign.id), str(uuid4()), "x")
        ]

    assert [r.status_code for r in responses] == [404, 404, 404]
    assert responses[0].json() == {"code": "NOT_FOUND", "message": "Conversation not found."}
    assert store.messages[foreign.id] == []


@pytest.mark.parametrize("content", ["", "   ", "x" * 2001])
async def test_should_answer_422_and_store_nothing_when_the_content_is_invalid(
    content: str,
) -> None:
    store = InMemoryConversationStore()
    model = ScriptedChatModel()
    async with _client(model, store) as client:
        conversation_id = await _conversation(client)
        response = await _post(client, conversation_id, content)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"
    assert store.messages[UUID(conversation_id)] == []
    assert model.calls == 0


async def test_should_end_with_llm_unavailable_and_keep_only_the_question_without_a_model() -> None:
    store = InMemoryConversationStore()
    async with _client(None, store) as client:
        conversation_id = await _conversation(client)
        response = await _post(client, conversation_id, "Hi")

    events = _events(response)
    assert [e.event for e in events] == ["message.start", "error"]
    assert events[-1].data == {
        "code": "LLM_UNAVAILABLE",
        "message": "The assistant is unavailable right now. Please try again later.",
    }
    assert [m.role for m in store.messages[UUID(conversation_id)]] == [MessageRole.USER]


async def test_should_answer_503_when_the_question_cannot_be_stored() -> None:
    store = InMemoryConversationStore()
    async with _client(ScriptedChatModel(), store) as client:
        conversation_id = await _conversation(client)
        store.failures["add_user_message"] = deque([OperationalError("x", {}, OSError("down"))])
        response = await _post(client, conversation_id, "Hi")

    assert response.status_code == 503
    assert response.json()["code"] == "UNAVAILABLE"


async def test_should_end_internal_when_the_answer_cannot_be_stored() -> None:
    store = InMemoryConversationStore()
    store.failures["add_assistant_message"] = deque([OperationalError("x", {}, OSError("down"))])
    async with _client(ScriptedChatModel([text_turn("Hello.")]), store) as client:
        conversation_id = await _conversation(client)
        response = await _post(client, conversation_id, "Hi")

    assert [e.event for e in _events(response)] == ["message.start", "token", "error"]
    assert _events(response)[-1].data == {
        "code": "INTERNAL",
        "message": "Something went wrong. Please try again.",
    }


async def test_should_end_internal_when_the_graph_fails() -> None:
    class BrokenModel:
        def stream(self, request: object) -> object:
            raise RuntimeError("bug")

    store = InMemoryConversationStore()
    async with _client(BrokenModel(), store) as client:  # type: ignore[arg-type]  # broken fake
        conversation_id = await _conversation(client)
        response = await _post(client, conversation_id, "Hi")

    assert [e.event for e in _events(response)] == ["message.start", "error"]
    assert [m.role for m in store.messages[UUID(conversation_id)]] == [MessageRole.USER]


async def test_should_run_tools_with_the_callers_identity_bound() -> None:
    seen: list[str] = []

    class RecordingRunner(FakeToolRunner):
        async def run(self, name: str, arguments: Mapping[str, JsonValue]) -> ToolOutcome:
            caller = current_caller()
            seen.append(caller.identity.subject)
            assert caller.token.get_secret_value() == TOKEN
            return await super().run(name, arguments)

    model = ScriptedChatModel([tool_turn(("toolu_01", "list_my_orders", {})), text_turn("Ok.")])
    store = InMemoryConversationStore()
    async with _client(model, store, RecordingRunner()) as client:
        conversation_id = await _conversation(client)
        await _post(client, conversation_id, "Orders?")

    assert seen == ["user-1"]
