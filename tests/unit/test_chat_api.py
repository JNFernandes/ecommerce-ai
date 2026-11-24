"""The ``/chat/conversations`` routes: CRUD of one's own conversations (contracts/chat-api.md)."""

from collections import deque
from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.exc import OperationalError

from ecommerce_ai.agents.assistant.model import FinishReason, Usage
from ecommerce_ai.api.auth import RejectReason, rejection_response
from ecommerce_ai.api.route_policy import AUTHENTICATED
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.main import create_app
from tests.support.mcp import serve_app
from tests.support.stores import InMemoryConversationStore
from tests.support.tokens import jwks, make_token
from tests.support.triage import NOW

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default
NOT_FOUND = {"code": "NOT_FOUND", "message": "Conversation not found."}
VALIDATION_FAILED = {"code": "VALIDATION_FAILED", "message": "The request is not valid."}
UNAVAILABLE = {
    "code": "UNAVAILABLE",
    "message": "The assistant is unavailable right now. Please try again later.",
}
OWNER = make_token(sub="user-1")
OTHER = make_token(sub="user-2")


@pytest.fixture
def identity() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        yield route


@pytest.fixture
def store() -> InMemoryConversationStore:
    return InMemoryConversationStore()


@pytest.fixture
def client(identity: respx.Route, store: InMemoryConversationStore) -> httpx.AsyncClient:
    """A client over the app without running its lifespan (no background task is enabled)."""
    transport = httpx.ASGITransport(
        app=create_app(probes=[], conversation_store=store, checkpointer=InMemorySaver())
    )
    return httpx.AsyncClient(transport=transport, base_url="http://localhost")


def _auth(token: str = OWNER) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create(client: httpx.AsyncClient, token: str = OWNER, **body: object) -> str:
    response = await client.post("/chat/conversations", json=body, headers=_auth(token))
    assert response.status_code == 201, response.text
    conversation_id = response.json()["id"]
    assert isinstance(conversation_id, str)
    return conversation_id


async def test_should_create_a_conversation_with_a_trimmed_title_when_one_is_given(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/chat/conversations", json={"title": "  My order  "}, headers=_auth()
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "title", "createdAt", "updatedAt"}
    assert body["title"] == "My order"
    assert body["createdAt"] == body["updatedAt"]


@pytest.mark.parametrize("body", [None, {}, {"title": None}, {"title": "   "}])
async def test_should_create_a_conversation_without_a_title_when_none_is_given(
    client: httpx.AsyncClient, body: dict[str, object] | None
) -> None:
    response = await client.post("/chat/conversations", json=body, headers=_auth())

    assert response.status_code == 201
    assert response.json()["title"] is None


@pytest.mark.parametrize("body", [{"title": "x" * 201}, {"title": 5}, {"other": "x"}])
async def test_should_answer_422_and_store_nothing_when_the_body_is_invalid(
    client: httpx.AsyncClient, store: InMemoryConversationStore, body: dict[str, object]
) -> None:
    response = await client.post("/chat/conversations", json=body, headers=_auth())

    assert response.status_code == 422
    assert response.json() == VALIDATION_FAILED
    assert store.conversations == {}


async def test_should_list_own_conversations_newest_first_with_paging(
    client: httpx.AsyncClient, store: InMemoryConversationStore
) -> None:
    first = await _create(client, title="first")
    second = await _create(client, title="second")
    await _create(client, OTHER, title="foreign")
    await store.add_user_message("user-1", _uuid(first), uuid4(), "hi", NOW + timedelta(days=9))

    page = await client.get("/chat/conversations?limit=1", headers=_auth())
    rest = await client.get("/chat/conversations?limit=1&offset=1", headers=_auth())
    default = await client.get("/chat/conversations", headers=_auth())

    assert page.status_code == rest.status_code == 200
    assert [c["id"] for c in page.json()["items"]] == [first]
    assert [c["id"] for c in rest.json()["items"]] == [second]
    assert {k: v for k, v in page.json().items() if k != "items"} == {
        "total": 2,
        "limit": 1,
        "offset": 0,
    }
    assert (default.json()["limit"], default.json()["offset"]) == (20, 0)


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "limit=abc"])
async def test_should_answer_422_when_paging_is_out_of_range(
    client: httpx.AsyncClient, query: str
) -> None:
    response = await client.get(f"/chat/conversations?{query}", headers=_auth())

    assert response.status_code == 422
    assert response.json() == VALIDATION_FAILED


async def test_should_return_messages_oldest_first_when_reading_a_conversation(
    client: httpx.AsyncClient, store: InMemoryConversationStore
) -> None:
    conversation_id = await _create(client)
    question = uuid4()
    answer = uuid4()
    await store.add_user_message("user-1", _uuid(conversation_id), question, "Where?", NOW)
    await store.add_assistant_message(
        "user-1",
        _uuid(conversation_id),
        answer,
        "It is SHIPPED.",
        FinishReason.COMPLETED,
        "assistant.v1",
        "claude-opus-5-5",
        Usage(input_tokens=10, output_tokens=2),
        NOW + timedelta(seconds=1),
    )

    response = await client.get(f"/chat/conversations/{conversation_id}", headers=_auth())

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == conversation_id
    assert body["messages"] == [
        {
            "id": str(question),
            "role": "user",
            "content": "Where?",
            "createdAt": "2026-11-24T12:00:00Z",
            "finishReason": None,
            "promptVersion": None,
            "modelId": None,
            "usage": None,
        },
        {
            "id": str(answer),
            "role": "assistant",
            "content": "It is SHIPPED.",
            "createdAt": "2026-11-24T12:00:01Z",
            "finishReason": "completed",
            "promptVersion": "assistant.v1",
            "modelId": "claude-opus-5-5",
            "usage": {"inputTokens": 10, "outputTokens": 2},
        },
    ]


async def test_should_delete_a_conversation_then_answer_404(
    client: httpx.AsyncClient, store: InMemoryConversationStore
) -> None:
    conversation_id = await _create(client)

    deleted = await client.delete(f"/chat/conversations/{conversation_id}", headers=_auth())
    again = await client.delete(f"/chat/conversations/{conversation_id}", headers=_auth())
    read = await client.get(f"/chat/conversations/{conversation_id}", headers=_auth())

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert again.status_code == read.status_code == 404
    assert again.json() == read.json() == NOT_FOUND
    assert store.conversations == {}


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_should_answer_the_same_404_when_the_id_is_foreign_unknown_or_malformed(
    client: httpx.AsyncClient, store: InMemoryConversationStore, method: str
) -> None:
    foreign = await _create(client, OTHER)

    responses = [
        await client.request(method, f"/chat/conversations/{target}", headers=_auth())
        for target in (foreign, uuid4(), "not-a-uuid")
    ]

    assert [r.status_code for r in responses] == [404, 404, 404]
    assert {r.content for r in responses} == {responses[0].content}
    assert responses[0].json() == NOT_FOUND
    assert len(store.conversations) == 1


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/chat/conversations"),
        ("GET", "/chat/conversations"),
        ("GET", f"/chat/conversations/{uuid4()}"),
        ("DELETE", f"/chat/conversations/{uuid4()}"),
    ],
)
async def test_should_answer_like_mcp_when_the_token_is_missing_bad_or_roleless(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    missing = await client.request(method, path)
    bad = await client.request(method, path, headers=_auth(OWNER + "x"))
    roleless = await client.request(method, path, headers=_auth(make_token(roles=[])))

    expected = rejection_response(RejectReason.INVALID_TOKEN)
    for response in (missing, bad):
        assert response.status_code == 401
        assert response.content == expected.body
        assert response.headers["WWW-Authenticate"] == "Bearer"
    assert roleless.status_code == 403
    assert roleless.content == rejection_response(RejectReason.MISSING_ROLE).body


async def test_should_answer_503_when_the_signing_keys_are_unavailable(
    store: InMemoryConversationStore,
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(JWKS_URL).mock(side_effect=httpx.ConnectError("identity down"))
        router.route(host="localhost").pass_through()
        async with serve_app(
            create_app(probes=[], conversation_store=store, checkpointer=InMemorySaver())
        ) as client:
            response = await client.get("/chat/conversations", headers=_auth())

    assert response.status_code == 503
    assert response.content == rejection_response(RejectReason.KEYS_UNAVAILABLE).body


@pytest.mark.parametrize(
    ("method", "path", "failing"),
    [
        ("POST", "/chat/conversations", "create"),
        ("GET", "/chat/conversations", "list"),
        ("GET", f"/chat/conversations/{uuid4()}", "get"),
        ("DELETE", f"/chat/conversations/{uuid4()}", "delete"),
    ],
)
async def test_should_answer_503_unavailable_when_the_database_is_down(
    client: httpx.AsyncClient,
    store: InMemoryConversationStore,
    method: str,
    path: str,
    failing: str,
) -> None:
    store.failures[failing] = deque([OperationalError("SELECT 1", {}, OSError("refused"))])

    response = await client.request(method, path, headers=_auth())

    assert response.status_code == 503
    assert response.json() == UNAVAILABLE


async def test_should_propagate_when_the_store_fails_with_a_non_transient_error(
    store: InMemoryConversationStore, identity: respx.Route
) -> None:
    store.failures["list"] = deque([RuntimeError("bug")])
    app = create_app(probes=[], conversation_store=store, checkpointer=InMemorySaver())
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/chat/conversations", headers=_auth())

    assert response.status_code == 500


async def test_should_keep_fastapi_validation_errors_outside_chat(
    store: InMemoryConversationStore,
) -> None:
    app = create_app(probes=[], conversation_store=store, checkpointer=InMemorySaver())

    @app.get("/probe-validation", openapi_extra={"x-public": True})
    async def probe(number: int) -> int:
        return number

    async with serve_app(app) as client:
        response = await client.get("/probe-validation?number=abc")

    assert response.status_code == 422
    assert "detail" in response.json()


def test_should_declare_authenticated_access_on_every_chat_route(
    store: InMemoryConversationStore,
) -> None:
    app = create_app(probes=[], conversation_store=store, checkpointer=InMemorySaver())

    operations = [
        operation
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/chat/")
        for operation in item.values()
    ]

    assert len(operations) == 5
    assert all(
        operation["x-authenticated"] is AUTHENTICATED["x-authenticated"] for operation in operations
    )
    assert all(operation.get("description") for operation in operations)


def _uuid(value: str) -> UUID:
    return UUID(value)
