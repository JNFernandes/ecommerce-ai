"""Chat span trees (plan D7): a run with a real tool call, and an approval pause and resume."""

from collections.abc import Iterator

import httpx
import pytest
import respx
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.trace import ReadableSpan

from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.main import create_app
from ecommerce_ai.telemetry import attributes
from tests.support.approvals import ApprovalHarness, sse
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
from tests.support.tracing import SpanRecorder, assert_parent

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default
TOKEN = make_token(sub="user-1")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Request-Id": "req-chat-1"}


@pytest.fixture
def orders() -> Iterator[OrdersStub]:
    with respx.mock(assert_all_called=False) as router:
        stub = OrdersStub(router, ORDERS_GRAPHQL_URL)  # before the localhost pass-through
        router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        yield stub


def _client(spans: SpanRecorder, model: ScriptedChatModel) -> httpx.AsyncClient:
    """The app with the real tool registry (``ToolInvoker`` → ``UpstreamTransport``)."""
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


def _in_trace(spans: SpanRecorder, root: ReadableSpan) -> list[ReadableSpan]:
    assert root.context is not None
    return [s for s in spans.spans if s.context and s.context.trace_id == root.context.trace_id]


def _message_request(spans: SpanRecorder) -> ReadableSpan:
    (request,) = [
        s
        for s in spans.spans_by_name("http.request")
        if (s.attributes or {}).get(attributes.HTTP_ROUTE)
        == "/chat/conversations/{conversation_id}/messages"
    ]
    return request


async def test_should_trace_a_chat_run_with_a_tool_call_as_one_tree(
    spans: SpanRecorder, orders: OrdersStub
) -> None:
    orders.respond_order(sample_order())
    model = ScriptedChatModel(
        [
            tool_turn(("call-1", "get_order", {"orderId": ORDER_ID})),
            text_turn("Your order is confirmed."),
        ]
    )
    async with _client(spans, model) as client:
        created = await client.post("/chat/conversations", json={}, headers=HEADERS)
        conversation_id = created.json()["id"]
        response = await client.post(
            f"/chat/conversations/{conversation_id}/messages",
            json={"content": "Where is my order?"},
            headers=HEADERS,
        )

    assert response.status_code == 200
    request = _message_request(spans)
    run = spans.one("chat.run")
    assert_parent(run, request)
    assert dict(run.attributes or {}) == {
        attributes.CONVERSATION_ID: conversation_id,
        attributes.USER_HASH: (run.attributes or {})[attributes.USER_HASH],
        attributes.REQUEST_ID: "req-chat-1",
        attributes.PROMPT_VERSION: "assistant.v4",
    }
    assert (run.attributes or {})[attributes.USER_HASH] != "user-1"
    nodes = [s.name for s in spans.spans if s.name.startswith("node.")]
    assert nodes == [
        "node.guard_input",
        "node.agent",
        "node.tools",
        "node.agent",
        "node.gatekeeper",
        "node.finalize",
    ]
    for node in spans.spans_by_name("node.agent") + spans.spans_by_name("node.tools"):
        assert_parent(node, run)
    first_agent, second_agent = spans.spans_by_name("node.agent")
    first_call, second_call = spans.spans_by_name("llm.call")
    assert_parent(first_call, first_agent)
    assert_parent(second_call, second_agent)
    assert dict(second_call.attributes or {}) == {
        attributes.PROMPT_VERSION: "assistant.v4",
        attributes.LLM_OUTCOME: "ok",
        attributes.LATENCY_MS: (second_call.attributes or {})[attributes.LATENCY_MS],
        attributes.MODEL: "claude-test-model",
        attributes.INPUT_TOKENS: 100,
        attributes.OUTPUT_TOKENS: 20,
        attributes.FINISH_REASON: "end_turn",
    }
    tool = spans.one("tool.call")
    assert_parent(tool, spans.one("node.tools"))
    assert dict(tool.attributes or {}) == {
        attributes.TOOL_NAME: "get_order",
        attributes.TOOL_OUTCOME: "ok",
    }
    upstream = spans.one("upstream.http")
    assert_parent(upstream, tool)
    assert dict(upstream.attributes or {}) == {attributes.HTTP_STATUS_CODE: 200}
    assert {s.name for s in _in_trace(spans, request)} == {
        "http.request",
        "chat.run",
        "node.guard_input",
        "node.agent",
        "llm.call",
        "node.tools",
        "tool.call",
        "upstream.http",
        "node.gatekeeper",
        "node.finalize",
    }


async def test_should_record_the_upstream_failure_by_class_name_only(
    spans: SpanRecorder, orders: OrdersStub
) -> None:
    orders.route.respond(503, text="jane@example.com is down")
    model = ScriptedChatModel(
        [tool_turn(("call-1", "get_order", {"orderId": ORDER_ID})), text_turn("Sorry.")]
    )
    async with _client(spans, model) as client:
        created = await client.post("/chat/conversations", json={}, headers=HEADERS)
        await client.post(
            f"/chat/conversations/{created.json()['id']}/messages",
            json={"content": "Where is my order?"},
            headers=HEADERS,
        )

    attempts = spans.spans_by_name("upstream.http")
    assert attempts
    for attempt in attempts:
        assert (attempt.attributes or {})[attributes.HTTP_STATUS_CODE] == 503
        assert (attempt.attributes or {})[attributes.ERROR_TYPE] == "UpstreamError"
        assert attempt.status.description is None
        assert "jane@example.com" not in attempt.to_json()
    assert (spans.one("tool.call").attributes or {})[attributes.TOOL_OUTCOME] == "upstream_error"


async def test_should_trace_the_approval_pause_and_its_resume(spans: SpanRecorder) -> None:
    harness = ApprovalHarness(tracer_provider=spans.provider)
    with respx.mock(assert_all_called=False) as router:
        router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        async with harness.client() as client:
            conversation_id, approval_id, _ = await harness.propose(client)
            pause = spans.one("approval.pause")
            run = spans.one("chat.run")
            spans.clear()
            harness.script(text_turn("Your order is cancelled."))
            decided = await harness.decide(client, conversation_id, approval_id)

    assert decided.status_code == 200
    assert "approval.resolved" in [event.event for event in sse(decided)]
    assert pause.parent is not None
    assert run.context is not None
    assert pause.parent.trace_id == run.context.trace_id
    assert dict(pause.attributes or {}) == {attributes.TOOL_NAME: "cancel_order"}
    resume = spans.one("chat.resume")
    approval_node = spans.one("node.approval")
    assert_parent(approval_node, resume)
    resumed = spans.one("approval.resume")
    assert_parent(resumed, approval_node)
    assert approval_node.status.is_ok
    assert spans.spans_by_name("approval.pause") == []
    assert (resume.attributes or {})[attributes.CONVERSATION_ID] == conversation_id
