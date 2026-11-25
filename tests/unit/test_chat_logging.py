"""One ``chat_run`` log line per answer and one ``chat_approval`` line per proposal and decision,
without content, arguments, results, totals or the token (FR-026, research §17; US-06 FR-017)."""

import hashlib
import io
import json
from collections import deque
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from langgraph.checkpoint.memory import InMemorySaver

from ecommerce_ai.agents.assistant.graph import (
    ChatGraph,
    ChatState,
    StateUpdate,
    load_assistant_prompt,
    record_guard_events,
)
from ecommerce_ai.agents.assistant.model import FinishReason, MessageEnd, Token
from ecommerce_ai.agents.assistant.service import ChatRun, ChatService, user_hash
from ecommerce_ai.agents.guardrails.events import (
    GuardAction,
    GuardEvent,
    GuardSeverity,
    GuardStage,
)
from ecommerce_ai.agents.ports import SystemClock
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
from tests.support.llm import MODEL_ID, ScriptedChatModel, TurnStep, text_turn, tool_turn
from tests.support.stores import InMemoryConversationStore, InMemoryGuardrailEventStore
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


# Guard events (US-07) -------------------------------------------------------------------------

BLOCKED = GuardEvent(
    stage=GuardStage.INPUT,
    rule="instruction_override",
    action=GuardAction.BLOCKED,
    severity=GuardSeverity.HIGH,
)
GREETING = "Hello, how can I help you today?"
SANITIZED = GuardEvent(
    stage=GuardStage.TOOL,
    rule="tool_result_cap",
    action=GuardAction.SANITIZED,
    severity=GuardSeverity.LOW,
)


class _GuardingGraph(ChatGraph):
    """A chat graph whose input node records two guard events on every message."""

    async def _guard_input(self, state: ChatState) -> StateUpdate:
        await super()._guard_input(state)
        return record_guard_events(state, [BLOCKED, SANITIZED])


async def _guarded_run(
    guard_store: InMemoryGuardrailEventStore, *turns: Sequence[TurnStep]
) -> tuple[ChatRun, UUID]:
    store = InMemoryConversationStore()
    graph = _GuardingGraph(
        model=ScriptedChatModel(turns),
        tools=FakeToolRunner(),
        prompt=load_assistant_prompt(),
        checkpointer=InMemorySaver(),
    )
    service = ChatService(store, graph, SystemClock(), guard_store=guard_store)
    conversation = await store.create(SUBJECT, None, None, datetime.now(UTC))
    run = await service.start(SUBJECT, conversation.id, QUESTION, request_id="req-guard")
    assert run is not None
    return run, conversation.id


def _log_lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


async def test_should_audit_and_log_each_guard_event_once_when_the_run_ends() -> None:
    guard_store = InMemoryGuardrailEventStore()
    run, conversation_id = await _guarded_run(guard_store, text_turn(GREETING))
    stream = io.StringIO()
    configure_logging("INFO", stream)

    events = [event async for event in run.events()]

    assert isinstance(events[-1], MessageEnd)
    assert not any(isinstance(event, GuardEvent) for event in events)
    assert [
        (row.user_id, row.conversation_id, row.request_id, row.event) for row in guard_store.rows
    ] == [
        (SUBJECT, conversation_id, "req-guard", BLOCKED),
        (SUBJECT, conversation_id, "req-guard", SANITIZED),
    ]
    lines = _log_lines(stream)
    guards = [line for line in lines if line["event"] == "guardrail_event"]
    assert [
        {key: line[key] for key in ("rule", "stage", "action", "severity", "user_hash")}
        for line in guards
    ] == [
        {
            "rule": "instruction_override",
            "stage": "input",
            "action": "blocked",
            "severity": "high",
            "user_hash": user_hash(SUBJECT),
        },
        {
            "rule": "tool_result_cap",
            "stage": "tool",
            "action": "sanitized",
            "severity": "low",
            "user_hash": user_hash(SUBJECT),
        },
    ]
    (chat_run,) = [line for line in lines if line["event"] == "chat_run"]
    assert chat_run["guards"] == {"input": {"blocked": 1}, "tool": {"sanitized": 1}}
    for secret in (QUESTION, GREETING, SUBJECT):
        assert secret not in stream.getvalue()


async def test_should_report_no_guards_on_chat_run_when_no_guard_acted() -> None:
    app = chat_app(ScriptedChatModel([text_turn(ANSWER)]), store=InMemoryConversationStore())
    stream = io.StringIO()
    configure_logging("INFO", stream)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        created = await client.post("/chat/conversations", json={}, headers=headers)
        await client.post(
            f"/chat/conversations/{created.json()['id']}/messages",
            json={"content": QUESTION},
            headers=headers,
        )

    lines = _log_lines(stream)
    (chat_run,) = [line for line in lines if line["event"] == "chat_run"]
    assert chat_run["guards"] == {}
    assert not [line for line in lines if line["event"] == "guardrail_event"]


async def test_should_audit_the_guard_events_when_the_client_goes_away() -> None:
    guard_store = InMemoryGuardrailEventStore()
    run, _ = await _guarded_run(guard_store, text_turn("Hello there. ", GREETING))
    stream = io.StringIO()
    configure_logging("INFO", stream)

    events = run.events()
    async for event in events:
        if isinstance(event, Token):
            break
    await events.aclose()

    assert guard_store.events == [BLOCKED, SANITIZED]
    (chat_run,) = [line for line in _log_lines(stream) if line["event"] == "chat_run"]
    assert chat_run["finish_reason"] == "client_disconnected"
    assert chat_run["guards"] == {"input": {"blocked": 1}, "tool": {"sanitized": 1}}


async def test_should_log_and_still_answer_when_the_guard_audit_cannot_be_written() -> None:
    guard_store = InMemoryGuardrailEventStore()
    guard_store.failures["append"] = deque([RuntimeError("audit down")])
    run, _ = await _guarded_run(guard_store, text_turn(GREETING))
    stream = io.StringIO()
    configure_logging("INFO", stream)

    events = [event async for event in run.events()]

    end = events[-1]
    assert isinstance(end, MessageEnd)
    assert end.finish_reason is FinishReason.COMPLETED
    assert guard_store.rows == []
    lines = _log_lines(stream)
    (failure,) = [line for line in lines if line["event"] == "guardrail_audit_failed"]
    assert failure["error"] == "RuntimeError"
    assert len([line for line in lines if line["event"] == "guardrail_event"]) == 2
