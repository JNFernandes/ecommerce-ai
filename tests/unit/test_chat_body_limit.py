"""``/chat`` request bodies over 16 KB get 413 before any route runs; disconnects still pass."""

import json
from collections.abc import AsyncIterator, Iterator
from uuid import UUID

import httpx
import pytest
import respx
from starlette.responses import PlainTextResponse
from starlette.types import Message, Receive, Scope, Send

from ecommerce_ai.api.body_limit import BodySizeLimitMiddleware
from ecommerce_ai.api.chat import MAX_BODY_BYTES
from ecommerce_ai.infrastructure.settings import Settings
from tests.support.chat import chat_app
from tests.support.llm import ScriptedChatModel, text_turn
from tests.support.stores import InMemoryConversationStore
from tests.support.tokens import jwks, make_token

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default
HEADERS = {"Authorization": f"Bearer {make_token(sub='user-1')}"}
TOO_LARGE = {"code": "PAYLOAD_TOO_LARGE", "message": "The request is too large."}


@pytest.fixture(autouse=True)
def identity() -> Iterator[None]:
    with respx.mock(assert_all_called=False) as router:
        router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        yield


def _client(store: InMemoryConversationStore, model: ScriptedChatModel) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=chat_app(model, store=store))
    return httpx.AsyncClient(transport=transport, base_url="http://localhost")


async def _conversation(client: httpx.AsyncClient) -> str:
    response = await client.post("/chat/conversations", json={}, headers=HEADERS)
    conversation_id = response.json()["id"]
    assert isinstance(conversation_id, str)
    return conversation_id


async def test_should_answer_413_and_store_nothing_when_the_declared_body_is_too_large() -> None:
    store = InMemoryConversationStore()
    model = ScriptedChatModel()
    async with _client(store, model) as client:
        conversation_id = await _conversation(client)
        response = await client.post(
            f"/chat/conversations/{conversation_id}/messages",
            content=json.dumps({"content": "x" * MAX_BODY_BYTES}),
            headers=HEADERS | {"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json() == TOO_LARGE
    assert response.headers["x-request-id"]
    assert model.calls == 0
    history = await store.get("user-1", UUID(conversation_id))
    assert history is not None
    assert history.messages == ()


async def test_should_answer_413_when_a_chunked_body_grows_past_the_limit() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b" " * (MAX_BODY_BYTES // 4)

    async with _client(InMemoryConversationStore(), ScriptedChatModel()) as client:
        response = await client.post(
            "/chat/conversations",
            content=chunks(),
            headers=HEADERS | {"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json() == TOO_LARGE


async def test_should_stream_the_answer_when_the_longest_valid_message_is_posted() -> None:
    model = ScriptedChatModel([text_turn("Hello.")])
    async with _client(InMemoryConversationStore(), model) as client:
        conversation_id = await _conversation(client)
        response = await client.post(
            f"/chat/conversations/{conversation_id}/messages",
            json={"content": "é" * 2000},
            headers=HEADERS,
        )

    assert response.status_code == 200
    assert "event: message.end" in response.text


class _RecordingApp:
    """An ASGI app that reads every message it is given until the client disconnects."""

    def __init__(self) -> None:
        self.received: list[Message] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            self.received.append(message)
            if message["type"] == "http.disconnect":
                break
        await PlainTextResponse("ok")(scope, receive, send)


def _scripted(*messages: Message) -> Receive:
    queue = list(messages)

    async def receive() -> Message:
        return queue.pop(0)

    return receive


def _scope(path: str) -> Scope:
    return {"type": "http", "path": path, "headers": [], "method": "POST"}


async def _ignore(_message: Message) -> None:
    return None


def _middleware(app: _RecordingApp) -> BodySizeLimitMiddleware:
    return BodySizeLimitMiddleware(
        app, max_bytes=8, path_prefix="/chat", reject=lambda: PlainTextResponse("no", 413)
    )


async def test_should_join_the_body_once_then_pass_the_disconnect_through() -> None:
    app = _RecordingApp()
    receive = _scripted(
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": False},
        {"type": "http.disconnect"},
    )

    await _middleware(app)(_scope("/chat/conversations"), receive, _ignore)

    assert app.received == [
        {"type": "http.request", "body": b"abcdef", "more_body": False},
        {"type": "http.disconnect"},
    ]


async def test_should_not_touch_requests_outside_the_prefix() -> None:
    app = _RecordingApp()
    first: Message = {"type": "http.request", "body": b"x" * 100, "more_body": True}
    receive = _scripted(first, {"type": "http.disconnect"})

    await _middleware(app)(_scope("/chatter"), receive, _ignore)

    assert app.received == [first, {"type": "http.disconnect"}]


async def test_should_not_run_the_app_when_the_client_leaves_before_the_body_ends() -> None:
    app = _RecordingApp()
    receive = _scripted(
        {"type": "http.request", "body": b"ab", "more_body": True}, {"type": "http.disconnect"}
    )

    await _middleware(app)(_scope("/chat"), receive, _ignore)

    assert app.received == []
