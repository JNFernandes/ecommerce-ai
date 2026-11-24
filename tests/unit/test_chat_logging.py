"""One ``chat_run`` log line per answer, without content, arguments, results or the token
(FR-026, research §17)."""

import hashlib
import io
import json
from collections.abc import Iterator

import httpx
import pytest
import respx

from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.settings import Settings
from tests.support.chat import FakeToolRunner, chat_app, envelope
from tests.support.llm import MODEL_ID, ScriptedChatModel, text_turn, tool_turn
from tests.support.stores import InMemoryConversationStore
from tests.support.tokens import jwks, make_token

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default
SUBJECT = "subject-7f3a9c"
TOKEN = make_token(sub=SUBJECT)
QUESTION = "Where is my parcel for the blue teapot?"
ANSWER = "Your blue teapot order is SHIPPED."
ARGUMENT = "order-9d1e77"
RESULT_VALUE = "warehouse-lisbon-secret"


@pytest.fixture(autouse=True)
def identity() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        yield route


async def test_should_log_one_chat_run_line_with_counts_and_no_content() -> None:
    model = ScriptedChatModel(
        [tool_turn(("toolu_01", "get_order", {"orderId": ARGUMENT})), text_turn(ANSWER)]
    )
    tools = FakeToolRunner(results={"get_order": envelope({"id": ARGUMENT, "note": RESULT_VALUE})})
    app = chat_app(model, tools, InMemoryConversationStore())
    stream = io.StringIO()
    configure_logging("INFO", stream)
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Request-Id": "req-chat-1"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        created = await client.post("/chat/conversations", json={}, headers=headers)
        conversation_id = created.json()["id"]
        await client.post(
            f"/chat/conversations/{conversation_id}/messages",
            json={"content": QUESTION},
            headers=headers,
        )

    output = stream.getvalue()
    lines = [json.loads(line) for line in output.splitlines()]
    (run,) = [line for line in lines if line["event"] == "chat_run"]
    assert run["request_id"] == "req-chat-1"
    assert run["conversation_id"] == conversation_id
    assert run["user_hash"] == hashlib.sha256(SUBJECT.encode()).hexdigest()[:16]
    assert run["latency_ms"] >= 0
    assert run["usage"] == {"input": 200, "output": 30}
    assert run["tools"] == [{"name": "get_order", "outcome": "ok", "duration_ms": 1.0}]
    assert run["finish_reason"] == "completed"
    assert run["error_code"] is None
    assert run["prompt_version"] == "assistant.v1"
    assert run["model_id"] == MODEL_ID
    for secret in (QUESTION, ANSWER, "blue teapot", ARGUMENT, RESULT_VALUE, TOKEN, SUBJECT):
        assert secret not in output
