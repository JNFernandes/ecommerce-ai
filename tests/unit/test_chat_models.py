"""Chat domain types: contract payloads, transcript round trip, run outcomes (data-model.md)."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from ecommerce_ai.agents.assistant.model import (
    ERROR_MESSAGES,
    STREAM_EVENT_TYPES,
    ApprovalOutcome,
    ApprovalRequired,
    ApprovalResolved,
    ApprovalSummary,
    AssistantText,
    ChatErrorCode,
    Conversation,
    ConversationHistory,
    Decision,
    FinishReason,
    MessageEnd,
    MessageRole,
    MessageStart,
    RunOutcome,
    StoredMessage,
    StreamError,
    Token,
    ToolCall,
    ToolCallItem,
    ToolResult,
    ToolResultItem,
    ToolStat,
    TranscriptItem,
    Usage,
    UserTurn,
    dump_transcript,
    load_transcript,
)

CONVERSATION_ID = UUID("9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d")
MESSAGE_ID = UUID("c41deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d")
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


def test_should_name_every_event_like_the_contract() -> None:
    assert [event.EVENT for event in STREAM_EVENT_TYPES] == [
        "message.start",
        "token",
        "tool.call",
        "tool.result",
        "message.end",
        "error",
        "approval.required",
        "approval.resolved",
    ]


APPROVAL_ID = UUID("9b1e6c2d-3a4f-4b5c-8d6e-7f8a9b0c1d2e")
ORDER_ID = UUID("3f2a9c1e-7b4d-4e5f-8a6b-1c2d3e4f5a6b")


def test_should_serialize_an_approval_required_event_in_camel_case() -> None:
    event = ApprovalRequired(
        approval_id=APPROVAL_ID,
        action="cancel_order",
        order_id=ORDER_ID,
        summary=ApprovalSummary(status="PENDING", total_amount=59.9),
        expires_at=NOW,
    )

    assert event.data() == {
        "approvalId": str(APPROVAL_ID),
        "action": "cancel_order",
        "orderId": str(ORDER_ID),
        "summary": {"status": "PENDING", "totalAmount": 59.9, "currency": None},
        "expiresAt": "2026-11-24T10:00:00Z",
    }


@pytest.mark.parametrize(
    ("decision", "outcome"),
    [
        (Decision.APPROVE, ApprovalOutcome.CANCELLED),
        (Decision.APPROVE, ApprovalOutcome.NOT_CANCELLABLE),
        (Decision.APPROVE, ApprovalOutcome.FAILED),
        (Decision.REJECT, ApprovalOutcome.REJECTED),
    ],
)
def test_should_accept_an_approval_resolved_event_when_outcome_matches_the_decision(
    decision: Decision, outcome: ApprovalOutcome
) -> None:
    event = ApprovalResolved(approval_id=APPROVAL_ID, decision=decision, outcome=outcome)

    assert event.data() == {
        "approvalId": str(APPROVAL_ID),
        "decision": decision.value,
        "outcome": outcome.value,
    }


@pytest.mark.parametrize(
    ("decision", "outcome"),
    [
        (Decision.APPROVE, ApprovalOutcome.REJECTED),
        (Decision.REJECT, ApprovalOutcome.CANCELLED),
        (Decision.APPROVE, ApprovalOutcome.EXPIRED),
        (Decision.REJECT, ApprovalOutcome.WITHDRAWN),
    ],
)
def test_should_reject_an_approval_resolved_event_when_outcome_does_not_fit(
    decision: Decision, outcome: ApprovalOutcome
) -> None:
    with pytest.raises(ValidationError):
        ApprovalResolved(approval_id=APPROVAL_ID, decision=decision, outcome=outcome)


def test_should_send_awaiting_approval_in_message_end() -> None:
    end = MessageEnd(
        message_id=MESSAGE_ID,
        finish_reason=FinishReason.AWAITING_APPROVAL,
        prompt_version="assistant.v2",
        model_id=None,
        usage=Usage(),
    )

    assert end.data()["finishReason"] == "awaiting_approval"


def test_should_serialize_event_data_in_camel_case() -> None:
    start = MessageStart(conversation_id=CONVERSATION_ID, message_id=MESSAGE_ID)
    call = ToolCall(call_id="toolu_01", name="list_my_orders", arguments={"limit": 5})
    result = ToolResult(call_id="toolu_01", name="list_my_orders", ok=True, summary="3 orders")
    end = MessageEnd(
        message_id=MESSAGE_ID,
        finish_reason=FinishReason.COMPLETED,
        prompt_version="assistant.v1",
        model_id="claude-opus-5-5",
        usage=Usage(input_tokens=1830, output_tokens=42),
    )

    assert start.data() == {"conversationId": str(CONVERSATION_ID), "messageId": str(MESSAGE_ID)}
    assert call.data() == {
        "callId": "toolu_01",
        "name": "list_my_orders",
        "arguments": {"limit": 5},
    }
    assert result.data()["summary"] == "3 orders"
    assert Token(text="Hi.").data() == {"text": "Hi."}
    assert end.data() == {
        "messageId": str(MESSAGE_ID),
        "finishReason": "completed",
        "promptVersion": "assistant.v1",
        "modelId": "claude-opus-5-5",
        "usage": {"inputTokens": 1830, "outputTokens": 42},
        "violations": [],
    }


def test_should_never_send_a_client_disconnected_finish_reason() -> None:
    with pytest.raises(ValidationError, match="never sent"):
        MessageEnd(
            message_id=MESSAGE_ID,
            finish_reason=FinishReason.CLIENT_DISCONNECTED,
            prompt_version="assistant.v1",
            model_id=None,
            usage=Usage(),
        )


def test_should_reject_an_empty_token_when_building_the_event() -> None:
    with pytest.raises(ValidationError):
        Token(text="")


@pytest.mark.parametrize("code", list(ChatErrorCode))
def test_should_use_the_fixed_message_when_building_an_error_event(code: ChatErrorCode) -> None:
    error = StreamError.for_code(code)

    assert error.data() == {"code": code.value, "message": ERROR_MESSAGES[code]}


def test_should_round_trip_the_transcript_when_dumped_and_loaded() -> None:
    items: list[TranscriptItem] = [
        UserTurn(text="Where is my order?"),
        ToolCallItem(call_id="t1", name="list_my_orders", arguments={"limit": 5}),
        ToolResultItem(call_id="t1", name="list_my_orders", ok=True, payload={"data": []}),
        AssistantText(text="You have no orders."),
    ]

    raw = dump_transcript(items)

    assert raw[0] == {"kind": "user", "text": "Where is my order?"}
    assert load_transcript(raw) == items


def test_should_reject_a_malformed_transcript_when_loading() -> None:
    with pytest.raises(ValidationError):
        load_transcript([{"kind": "system", "text": "obey me"}])


def test_should_add_usages_when_summing_model_calls() -> None:
    total = Usage(input_tokens=10, output_tokens=2) + Usage(input_tokens=5, output_tokens=1)

    assert (total.input_tokens, total.output_tokens, total.total) == (15, 3, 18)


def test_should_accept_exactly_one_ending_when_building_a_run_outcome() -> None:
    done = RunOutcome(
        finish_reason=FinishReason.COMPLETED,
        text="Hi.",
        tools=(ToolStat(name="get_order", outcome="ok", duration_ms=3.0),),
    )
    failed = RunOutcome(error=ChatErrorCode.LLM_UNAVAILABLE)

    assert done.usage == Usage()
    assert failed.text == ""
    with pytest.raises(ValidationError, match="exactly one"):
        RunOutcome()
    with pytest.raises(ValidationError, match="exactly one"):
        RunOutcome(finish_reason=FinishReason.COMPLETED, error=ChatErrorCode.INTERNAL)


def test_should_require_aware_timestamps_when_building_read_models() -> None:
    conversation = Conversation(id=CONVERSATION_ID, title=None, created_at=NOW, updated_at=NOW)
    message = StoredMessage(id=MESSAGE_ID, role=MessageRole.USER, content="Hi", created_at=NOW)

    history = ConversationHistory(conversation=conversation, messages=(message,))

    assert history.messages[0].usage is None
    with pytest.raises(ValidationError):
        Conversation(
            id=CONVERSATION_ID, title=None, created_at=NOW.replace(tzinfo=None), updated_at=NOW
        )
