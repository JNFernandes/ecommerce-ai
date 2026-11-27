"""Privacy (US-08 US2): secrets and personal data in a chat never reach a span or a log line.

The customer's message, the model's text and its tool arguments (a known and an unknown tool)
carry a fake API key, a bearer token, an email and a secret; the spans exported through the
scrubber and every captured log line are searched for each of them and for the system prompt.
"""

import io
import json
from collections.abc import Iterator

import httpx
import pytest
import respx
from langgraph.checkpoint.memory import InMemorySaver

from ecommerce_ai.agents.assistant.graph import load_assistant_prompt
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.main import create_app
from tests.support.chat import UNLIMITED_BUDGET
from tests.support.llm import ScriptedChatModel, text_turn, tool_turn
from tests.support.orders_stub import ORDER_ID, ORDERS_GRAPHQL_URL, OrdersStub, sample_order
from tests.support.stores import (
    InMemoryApprovalStore,
    InMemoryConversationStore,
    InMemoryGuardrailEventStore,
    InMemoryLlmCallRecorder,
    InMemoryTokenBudget,
    InMemoryUserUsageStore,
)
from tests.support.tokens import jwks, make_token
from tests.support.tracing import SpanRecorder

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default
TOKEN = make_token(sub="jane-subject-42")
FAKE_KEY = "sk-ant-FAKE0123456789abcdef"
FAKE_BEARER = "bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJqYW5lIn0.c2lnbmF0dXJl"
EMAIL = "jane@example.com"
SECRET = "secret-XYZ"  # noqa: S105  # fake, test-only
SENSITIVE = (FAKE_KEY, FAKE_BEARER, "eyJhbGciOiJSUzI1NiJ9", EMAIL, SECRET, "jane-subject-42")
MESSAGE = f"My key is {FAKE_KEY}, token {FAKE_BEARER}, mail {EMAIL}, password {SECRET}."


@pytest.fixture
def orders() -> Iterator[OrdersStub]:
    with respx.mock(assert_all_called=False) as router:
        stub = OrdersStub(router, ORDERS_GRAPHQL_URL)
        router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        yield stub


def _client(spans: SpanRecorder, model: ScriptedChatModel) -> httpx.AsyncClient:
    store = InMemoryConversationStore()
    app = create_app(
        probes=[],
        conversation_store=store,
        chat_model=model,
        checkpointer=InMemorySaver(),
        token_budget=InMemoryTokenBudget(UNLIMITED_BUDGET),
        approval_store=InMemoryApprovalStore(store),
        guard_store=InMemoryGuardrailEventStore(),
        user_usage_store=InMemoryUserUsageStore(),
        tracer_provider=spans.provider,
        llm_call_recorder=InMemoryLlmCallRecorder(),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")


async def test_should_keep_secrets_and_personal_data_out_of_spans_and_logs(
    spans: SpanRecorder, orders: OrdersStub
) -> None:
    orders.respond_order(sample_order(cancellationReason=f"{EMAIL} {SECRET}"))
    model = ScriptedChatModel(
        [
            tool_turn(
                ("call-1", "get_order", {"orderId": EMAIL}),
                ("call-2", "list_my_orders", {"status": SECRET}),
                ("call-3", f"tool_{SECRET}", {"q": FAKE_KEY}),
            ),
            tool_turn(("call-4", "get_order", {"orderId": ORDER_ID})),
            text_turn(f"Noted {EMAIL}; I will not repeat {SECRET}."),
        ]
    )
    logs = io.StringIO()
    async with _client(spans, model) as client:
        configure_logging("INFO", logs)
        headers = {"Authorization": f"Bearer {TOKEN}"}
        created = await client.post("/chat/conversations", json={}, headers=headers)
        response = await client.post(
            f"/chat/conversations/{created.json()['id']}/messages",
            json={"content": MESSAGE},
            headers=headers,
        )

    assert response.status_code == 200
    assert {"chat.run", "tool.call", "upstream.http", "llm.call"} <= set(spans.names())
    exported = "\n".join(span.to_json() for span in spans.spans)
    logged = logs.getvalue()
    assert [json.loads(line) for line in logged.splitlines()]  # the run did log
    prompt = load_assistant_prompt().system
    for text in (*SENSITIVE, TOKEN, prompt[:80], MESSAGE):
        assert text not in exported, f"{text!r} leaked into a span"
        assert text not in logged, f"{text!r} leaked into a log line"
    for span in spans.spans:
        assert span.events == ()
        assert span.status.description is None
