"""Sanity checks for the chat test fakes: scripted model, SSE parser, store and tool runner."""

import asyncio
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import respx

from ecommerce_ai.agents.assistant.model import (
    ApprovalOutcome,
    ApprovalSummary,
    Decision,
    FinishReason,
    MessageRole,
    Usage,
    UserTurn,
)
from ecommerce_ai.agents.assistant.ports import (
    Approval,
    ApprovalStatus,
    ApprovalStore,
    ChatModel,
    ChatModelRequest,
    ConversationStore,
    ModelTurnEnd,
    ModelUnavailable,
    ProposalMessage,
    TextDelta,
    ToolRunner,
    ToolUseRequest,
)
from tests.support.chat import FakeToolError, FakeToolRunner, envelope
from tests.support.llm import ScriptedChatModel, text_turn, tool_turn
from tests.support.orders_stub import ORDERS_BASE_URL, OrdersCancelStub
from tests.support.polling import wait_until
from tests.support.sse import SseComment, SseEvent, events_of, parse_sse, read_sse
from tests.support.stores import InMemoryApprovalStore, InMemoryConversationStore

REQUEST = ChatModelRequest(system="system", transcript=(UserTurn(text="Hi"),), tools=())
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


async def _play(model: ChatModel) -> list[object]:
    return [event async for event in model.stream(REQUEST)]


async def test_should_play_scripted_turns_and_record_requests() -> None:
    model = ScriptedChatModel(
        [tool_turn(("t1", "get_order", {"orderId": "x"})), text_turn("Hel", "lo.")]
    )

    first = await _play(model)
    second = await _play(model)

    assert isinstance(first[0], ToolUseRequest)
    assert isinstance(first[-1], ModelTurnEnd)
    assert first[-1].stop_reason == "tool_use"
    assert [e.text for e in second if isinstance(e, TextDelta)] == ["Hel", "lo."]
    assert model.calls == 2
    assert model.requests == [REQUEST, REQUEST]


async def test_should_raise_before_or_after_the_first_delta_when_scripted() -> None:
    model = ScriptedChatModel(
        [[ModelUnavailable("down")], [TextDelta(text="a"), ModelUnavailable("x")]]
    )
    seen: list[object] = []

    with pytest.raises(ModelUnavailable):
        await _play(model)
    with pytest.raises(ModelUnavailable):
        async for event in model.stream(REQUEST):
            seen.append(event)

    assert seen == [TextDelta(text="a")]
    assert model.closed_early == 0


async def test_should_count_a_turn_closed_early_when_the_reader_stops() -> None:
    model = ScriptedChatModel([text_turn("a", "b")])
    stream = model.stream(REQUEST)

    await anext(stream)
    await stream.aclose()  # type: ignore[attr-defined]  # an async generator underneath

    assert model.closed_early == 1


async def test_should_hold_the_stream_until_the_gate_opens() -> None:
    gate = asyncio.Event()
    model = ScriptedChatModel([[TextDelta(text="a"), gate, *text_turn("b")]])
    task = asyncio.create_task(_play(model))

    await wait_until(lambda: model.calls == 1)
    assert not task.done()
    gate.set()

    assert len(await asyncio.wait_for(task, 1)) == 3


async def test_should_fail_loudly_when_the_script_runs_out() -> None:
    with pytest.raises(AssertionError, match="more often than scripted"):
        await _play(ScriptedChatModel())


def test_should_parse_events_and_comments_when_reading_an_sse_body() -> None:
    body = (
        'id: 1\nevent: message.start\ndata: {"messageId":"m"}\n\n'
        ": keep-alive\n\n"
        'id: 2\r\nevent: token\r\ndata: {"text":"Hi."}\r\n\r\n'
        "data: 3"
    )

    items = parse_sse(body)

    assert items == [
        SseEvent(id="1", event="message.start", data={"messageId": "m"}),
        SseComment("keep-alive"),
        SseEvent(id="2", event="token", data={"text": "Hi."}),
        SseEvent(id=None, event="message", data=3),
    ]
    assert [e.event for e in events_of(items)] == ["message.start", "token", "message"]


async def test_should_stop_reading_a_stream_when_asked() -> None:
    body = 'id: 1\nevent: token\ndata: {"text":"a"}\n\nid: 2\nevent: token\ndata: {"text":"b"}\n\n'

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client,
        client.stream("GET", "http://test/") as response,
    ):
        first = await read_sse(response, stop=lambda event: event.event == "token")
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client,
        client.stream("GET", "http://test/") as response,
    ):
        everything = await read_sse(response)

    assert len(first) == 1
    assert len(everything) == 2


async def test_should_scope_conversations_by_user_in_the_memory_store() -> None:
    store: ConversationStore = InMemoryConversationStore()
    older = await store.create("user-1", None, "Old", NOW)
    newer = await store.create("user-1", None, None, NOW + timedelta(minutes=1))
    await store.create("user-2", None, None, NOW)

    await store.add_user_message("user-1", older.id, uuid4(), "Hi", NOW + timedelta(minutes=2))
    await store.add_assistant_message(
        "user-1",
        older.id,
        uuid4(),
        "Hello.",
        FinishReason.COMPLETED,
        "assistant.v1",
        None,
        Usage(),
        NOW + timedelta(minutes=3),
    )
    page = await store.list("user-1", limit=10, offset=0)
    history = await store.get("user-1", older.id)

    assert [c.id for c in page.items] == [older.id, newer.id]
    assert page.total == 2
    assert history is not None
    assert [m.role for m in history.messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert await store.count_messages("user-1", older.id) == 2
    assert await store.get("user-2", older.id) is None
    assert await store.count_messages("user-2", older.id) is None
    assert not await store.delete("user-2", older.id)
    with pytest.raises(LookupError):
        await store.add_user_message("user-2", older.id, uuid4(), "x", NOW)
    assert await store.delete("user-1", older.id)
    assert await store.get("user-1", older.id) is None


async def test_should_raise_the_queued_failure_once_in_the_memory_store() -> None:
    store = InMemoryConversationStore()
    store.failures["create"] = deque([OSError("db down")])

    with pytest.raises(OSError, match="db down"):
        await store.create("user-1", None, None, NOW)

    assert (await store.create("user-1", None, None, NOW)).title is None


async def test_should_answer_scripted_results_and_refuse_unknown_tools() -> None:
    runner: ToolRunner = FakeToolRunner(
        results={
            "get_order": envelope({"status": "SHIPPED"}),
            "list_my_orders": FakeToolError(upstream_unauthenticated=True),
        }
    )

    found = await runner.run("get_order", {"orderId": "x"})
    failed = await runner.run("list_my_orders", {})
    empty = await runner.run("list_products", {})
    refused = await runner.run("cancel_order", {})

    assert found.ok
    assert found.payload["trust"] == "untrusted_data"
    assert (failed.ok, failed.upstream_unauthenticated) == (False, True)
    assert empty.payload["data"] == []
    assert (refused.refused, refused.outcome) == (True, "unknown_tool")
    assert len(runner.tool_specs()) == 5
    assert isinstance(runner, FakeToolRunner)
    assert [call.name for call in runner.calls] == ["get_order", "list_my_orders", "list_products"]


# Approvals (US-06) ----------------------------------------------------------------------------

TTL = timedelta(seconds=600)


async def _proposed(
    conversations: InMemoryConversationStore, store: InMemoryApprovalStore, user_id: str = "user-1"
) -> Approval:
    conversation = await conversations.create(user_id, None, None, NOW)
    order_id = uuid4()
    approval = Approval(
        id=uuid4(),
        conversation_id=conversation.id,
        user_id=user_id,
        action="cancel_order",
        order_id=order_id,
        arguments={"orderId": str(order_id)},
        summary=ApprovalSummary(status="PENDING", total_amount=10.0),
        proposed_at=NOW,
        expires_at=NOW + TTL,
    )
    message = ProposalMessage(
        message_id=uuid4(),
        content="Shall I cancel it?",
        prompt_version="assistant.v2",
        model_id="m",
        usage=Usage(),
    )
    await store.create_proposal(approval, message, "req-1")
    return approval


@pytest.fixture
def conversations() -> InMemoryConversationStore:
    return InMemoryConversationStore()


@pytest.fixture
def approvals(conversations: InMemoryConversationStore) -> InMemoryApprovalStore:
    return InMemoryApprovalStore(conversations)


async def test_should_store_the_approval_audit_row_and_message_when_proposing(
    conversations: InMemoryConversationStore, approvals: InMemoryApprovalStore
) -> None:
    store: ApprovalStore = approvals
    approval = await _proposed(conversations, approvals)

    assert await store.pending_for("user-1", approval.conversation_id) == approval
    assert approvals.actions[approval.id].request_id == "req-1"
    assert approvals.actions[approval.id].outcome is None
    history = await conversations.get("user-1", approval.conversation_id)
    assert history is not None
    assert history.messages[-1].finish_reason is FinishReason.AWAITING_APPROVAL


async def test_should_refuse_a_second_pending_approval_in_the_same_conversation(
    conversations: InMemoryConversationStore, approvals: InMemoryApprovalStore
) -> None:
    approval = await _proposed(conversations, approvals)
    second = approval.model_copy(update={"id": uuid4()})
    message = ProposalMessage(
        message_id=uuid4(), content="", prompt_version="v", model_id=None, usage=Usage()
    )

    with pytest.raises(ValueError, match="one pending"):
        await approvals.create_proposal(second, message, None)


async def test_should_hide_an_approval_from_another_user_or_conversation(
    conversations: InMemoryConversationStore, approvals: InMemoryApprovalStore
) -> None:
    approval = await _proposed(conversations, approvals)

    assert await approvals.get("user-2", approval.conversation_id, approval.id) is None
    assert await approvals.get("user-1", uuid4(), approval.id) is None
    assert await approvals.pending_for("user-2", approval.conversation_id) is None
    assert (
        await approvals.decide(
            "user-2", approval.conversation_id, approval.id, Decision.APPROVE, NOW
        )
        is None
    )


async def test_should_decide_an_approval_only_once(
    conversations: InMemoryConversationStore, approvals: InMemoryApprovalStore
) -> None:
    approval = await _proposed(conversations, approvals)
    cid = approval.conversation_id

    first = await approvals.decide("user-1", cid, approval.id, Decision.APPROVE, NOW)
    second = await approvals.decide("user-1", cid, approval.id, Decision.REJECT, NOW)

    assert first is not None
    assert first.status is ApprovalStatus.APPROVED
    assert second is None
    assert approvals.actions[approval.id].decision is Decision.APPROVE
    assert await approvals.awaiting_outcome("user-1", cid) == first


async def test_should_not_decide_an_approval_from_its_expiry_on(
    conversations: InMemoryConversationStore, approvals: InMemoryApprovalStore
) -> None:
    approval = await _proposed(conversations, approvals)

    decided = await approvals.decide(
        "user-1", approval.conversation_id, approval.id, Decision.APPROVE, NOW + TTL
    )

    assert decided is None
    assert approvals.actions[approval.id].decision is None


async def test_should_set_the_audit_outcome_once(
    conversations: InMemoryConversationStore, approvals: InMemoryApprovalStore
) -> None:
    approval = await _proposed(conversations, approvals)
    cid = approval.conversation_id
    await approvals.decide("user-1", cid, approval.id, Decision.REJECT, NOW)

    await approvals.record_outcome(approval.id, ApprovalOutcome.REJECTED)
    await approvals.record_outcome(approval.id, ApprovalOutcome.FAILED)

    assert approvals.actions[approval.id].outcome is ApprovalOutcome.REJECTED
    assert await approvals.awaiting_outcome("user-1", cid) is None


async def test_should_close_only_a_pending_approval(
    conversations: InMemoryConversationStore, approvals: InMemoryApprovalStore
) -> None:
    approval = await _proposed(conversations, approvals)

    closed = await approvals.close(
        approval.id, ApprovalStatus.EXPIRED, ApprovalOutcome.EXPIRED, NOW + TTL
    )
    again = await approvals.close(
        approval.id, ApprovalStatus.WITHDRAWN, ApprovalOutcome.WITHDRAWN, NOW + TTL
    )

    assert (closed, again) == (True, False)
    assert approvals.actions[approval.id].outcome is ApprovalOutcome.EXPIRED
    with pytest.raises(ValueError, match="close only"):
        await approvals.close(approval.id, ApprovalStatus.APPROVED, ApprovalOutcome.FAILED, NOW)


async def test_should_drop_approvals_but_keep_audit_rows_when_the_conversation_is_deleted(
    conversations: InMemoryConversationStore, approvals: InMemoryApprovalStore
) -> None:
    approval = await _proposed(conversations, approvals)

    await conversations.delete("user-1", approval.conversation_id)

    assert approvals.approvals == []
    assert approval.id in approvals.actions
    assert await approvals.proposals_since("user-1", NOW) == 1
    assert await approvals.proposals_since("user-1", NOW + timedelta(seconds=1)) == 0
    assert await approvals.proposals_since("user-2", NOW) == 0


async def test_should_raise_the_queued_approval_failure_once_and_change_nothing(
    conversations: InMemoryConversationStore, approvals: InMemoryApprovalStore
) -> None:
    approval = await _proposed(conversations, approvals)
    approvals.failures["decide"] = deque([ConnectionError("down")])
    cid = approval.conversation_id

    with pytest.raises(ConnectionError):
        await approvals.decide("user-1", cid, approval.id, Decision.APPROVE, NOW)

    assert (await approvals.pending_for("user-1", cid)) == approval


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


async def _cancel(order_id: str) -> httpx.Response:
    async with httpx.AsyncClient() as http:
        return await http.post(f"{ORDERS_BASE_URL}/{order_id}/cancel")


async def test_should_cancel_and_count_each_order_in_the_cancel_stub(
    router: respx.MockRouter,
) -> None:
    stub = OrdersCancelStub(router)

    response = await _cancel("o-1")

    assert response.json() == {"orderId": "o-1", "status": "CANCELLED"}
    assert (stub.call_count, stub.order_ids) == (1, ["o-1"])


@pytest.mark.parametrize("status", [404, 409])
async def test_should_answer_the_configured_status_in_the_cancel_stub(
    router: respx.MockRouter, status: int
) -> None:
    stub = OrdersCancelStub(router)
    stub.respond_status(status)

    assert (await _cancel("o-1")).status_code == status


async def test_should_answer_a_configured_body_or_hang_in_the_cancel_stub(
    router: respx.MockRouter,
) -> None:
    stub = OrdersCancelStub(router)
    stub.respond_json({"unexpected": True})
    assert (await _cancel("o-1")).json() == {"unexpected": True}

    stub.respond_hang()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await _cancel("o-1")
