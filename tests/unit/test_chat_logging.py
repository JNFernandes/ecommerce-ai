"""One ``chat_run`` log line per answer and one ``chat_approval`` line per proposal and decision,
without content, arguments, results, totals or the token (FR-026, research §17; US-06 FR-017)."""

import hashlib
import io
import json
from collections.abc import Iterator

import httpx
import pytest
import respx

from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.settings import Settings
from tests.support.approvals import (
    ORDER_ID,
    OWNER,
    ApprovalHarness,
    cancel_turn,
    data,
    headers,
    sse,
)
from tests.support.approvals import TOKEN as APPROVAL_TOKEN
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
    tools = FakeToolRunner(
        results={"get_order": envelope({"id": ARGUMENT, "status": "SHIPPED", "note": RESULT_VALUE})}
    )
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
    assert run["prompt_version"] == "assistant.v3"
    assert run["model_id"] == MODEL_ID
    for secret in (QUESTION, ANSWER, "blue teapot", ARGUMENT, RESULT_VALUE, TOKEN, SUBJECT):
        assert secret not in output


async def test_should_log_one_chat_approval_line_per_proposal_and_per_decision() -> None:
    harness = ApprovalHarness()
    harness.tools.results["get_order"] = envelope(
        {"orderId": ORDER_ID, "status": "PENDING", "totalAmount": 873.45, "note": RESULT_VALUE}
    )
    harness.script(cancel_turn(), text_turn(ANSWER))
    stream = io.StringIO()
    async with harness.client() as client:
        configure_logging("INFO", stream)
        created = await client.post("/chat/conversations", json={}, headers=headers())
        conversation_id = created.json()["id"]
        proposed = await client.post(
            f"/chat/conversations/{conversation_id}/messages",
            json={"content": QUESTION},
            headers=headers() | {"X-Request-Id": "req-propose"},
        )
        approval_id = data(sse(proposed), "approval.required")["approvalId"]
        await client.post(
            f"/chat/conversations/{conversation_id}/approvals/{approval_id}",
            json={"decision": "approve"},
            headers=headers() | {"X-Request-Id": "req-decide"},
        )

    output = stream.getvalue()
    lines = [json.loads(line) for line in output.splitlines()]
    approvals = [line for line in lines if line["event"] == "chat_approval"]
    common = {
        "conversation_id": conversation_id,
        "approval_id": approval_id,
        "action": "cancel_order",
        "user_hash": hashlib.sha256(OWNER.encode()).hexdigest()[:16],
    }
    assert [
        {key: line[key] for key in (*common, "request_id", "decision", "outcome")}
        for line in approvals
    ] == [
        common | {"request_id": "req-propose", "decision": None, "outcome": None},
        common | {"request_id": "req-decide", "decision": "approve", "outcome": "cancelled"},
    ]
    for secret in (QUESTION, ANSWER, "873.45", RESULT_VALUE, APPROVAL_TOKEN, OWNER):
        assert secret not in output
