"""``POST /chat/conversations/{id}/approvals/{approvalId}``: access, validation, errors (US-06)."""

import asyncio
import json
from collections import deque
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
import respx
from sqlalchemy.exc import OperationalError

from ecommerce_ai.agents.assistant.model import ApprovalOutcome, FinishReason
from ecommerce_ai.agents.assistant.ports import ApprovalStatus, TextDelta
from ecommerce_ai.api.chat import MAX_BODY_BYTES
from ecommerce_ai.infrastructure.settings import Settings
from tests.support.approvals import (
    OTHER_TOKEN,
    TOKEN,
    ApprovalHarness,
    data,
    names,
    sse,
)
from tests.support.llm import TurnStep, text_turn, turn_end
from tests.support.polling import wait_until
from tests.support.tokens import jwks

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default
NOT_FOUND = {"code": "NOT_FOUND", "message": "Conversation not found."}
EXPIRED = {"code": "APPROVAL_EXPIRED", "message": "This action has expired. Please ask again."}
ALREADY_DECIDED = {
    "code": "APPROVAL_ALREADY_DECIDED",
    "message": "This action has already been decided.",
}
TTL = 600.0


@pytest.fixture(autouse=True)
def identity() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        yield route


@pytest.fixture
def harness() -> ApprovalHarness:
    return ApprovalHarness()


# Access and validation (US1) --------------------------------------------------------------------


async def test_should_answer_401_and_decide_nothing_when_no_token_is_sent(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        response = await client.post(
            f"/chat/conversations/{conversation_id}/approvals/{approval_id}",
            json={"decision": "approve"},
        )

    assert response.status_code == 401
    assert harness.approvals.approvals[0].status is ApprovalStatus.PENDING
    assert harness.tools.approved_calls == []


@pytest.mark.parametrize(
    "body", [{}, {"decision": "yes"}, {"decision": "approve", "orderId": "x"}, {"decision": 1}]
)
async def test_should_answer_422_and_decide_nothing_when_the_body_is_invalid(
    harness: ApprovalHarness, body: dict[str, object]
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        response = await client.post(
            f"/chat/conversations/{conversation_id}/approvals/{approval_id}",
            json=body,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 422
    assert response.json() == {"code": "VALIDATION_FAILED", "message": "The request is not valid."}
    assert harness.approvals.approvals[0].status is ApprovalStatus.PENDING


async def test_should_answer_413_when_the_decision_body_is_too_large(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        response = await client.post(
            f"/chat/conversations/{conversation_id}/approvals/{approval_id}",
            content=json.dumps({"decision": "approve", "pad": "x" * MAX_BODY_BYTES}),
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 413
    assert response.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert harness.tools.approved_calls == []


async def test_should_answer_429_when_decisions_come_too_fast() -> None:
    harness = ApprovalHarness(settings=Settings(chat_rate_limit_messages=1))
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        response = await harness.decide(client, conversation_id, approval_id)

    assert response.status_code == 429
    assert response.json()["code"] == "RATE_LIMITED"
    assert "Retry-After" in response.headers
    assert harness.tools.approved_calls == []


async def test_should_answer_503_and_cancel_nothing_when_the_approval_store_is_down(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.approvals.failures["decide"] = deque([OperationalError("x", {}, Exception())])
        response = await harness.decide(client, conversation_id, approval_id)

    assert response.status_code == 503
    assert response.json()["code"] == "UNAVAILABLE"
    assert harness.tools.approved_calls == []
    assert harness.approvals.approvals[0].status is ApprovalStatus.PENDING


async def test_should_answer_404_identically_for_unknown_malformed_and_foreign_ids(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        responses = [
            await harness.decide(client, conversation_id, str(uuid4())),
            await harness.decide(client, conversation_id, "not-a-uuid"),
            await harness.decide(client, "not-a-uuid", approval_id),
            await harness.decide(client, str(uuid4()), approval_id),
            await harness.decide(client, conversation_id, approval_id, token=OTHER_TOKEN),
        ]

    assert [r.status_code for r in responses] == [404] * 5
    assert all(r.json() == NOT_FOUND for r in responses)
    assert harness.approvals.approvals[0].status is ApprovalStatus.PENDING
    assert harness.tools.approved_calls == []


async def test_should_stream_with_sse_framing_when_deciding(harness: ApprovalHarness) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.script(text_turn("Cancelled."))
        response = await harness.decide(client, conversation_id, approval_id)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    events = sse(response)
    assert [event.id for event in events] == [str(i) for i in range(1, len(events) + 1)]
    assert data(events, "message.start")["conversationId"] == conversation_id


async def test_should_answer_409_stream_active_when_a_stream_runs_in_the_conversation(
    harness: ApprovalHarness,
) -> None:
    gate = asyncio.Event()
    held: list[TurnStep] = [gate, TextDelta(text="Cancelled."), turn_end()]
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.script(held)
        calls = harness.model.calls
        first = asyncio.create_task(harness.decide(client, conversation_id, approval_id))
        await wait_until(lambda: harness.model.calls == calls + 1)
        concurrent = await harness.decide(client, conversation_id, approval_id, "reject")
        gate.set()
        answered = await first

    assert concurrent.status_code == 409
    assert concurrent.json()["code"] == "STREAM_ACTIVE"
    assert data(sse(answered), "approval.resolved")["outcome"] == "cancelled"
    assert len(harness.tools.approved_calls) == 1


# Single use and expiry (US2) --------------------------------------------------------------------


async def test_should_answer_409_already_decided_and_do_nothing_when_a_decision_is_replayed(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.script(text_turn("Cancelled."))
        first = await harness.decide(client, conversation_id, approval_id)
        replay = await harness.decide(client, conversation_id, approval_id)
        flipped = await harness.decide(client, conversation_id, approval_id, "reject")

    assert first.status_code == 200
    assert (replay.status_code, replay.json()) == (409, ALREADY_DECIDED)
    assert (flipped.status_code, flipped.json()) == (409, ALREADY_DECIDED)
    assert len(harness.tools.approved_calls) == 1


async def test_should_cancel_once_when_two_approvals_race(harness: ApprovalHarness) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.script(text_turn("Cancelled."), text_turn("Cancelled."))
        responses = await asyncio.gather(
            harness.decide(client, conversation_id, approval_id),
            harness.decide(client, conversation_id, approval_id),
        )

    assert sorted(r.status_code for r in responses) == [200, 409]
    (refused,) = [r for r in responses if r.status_code == 409]
    assert refused.json()["code"] in {"APPROVAL_ALREADY_DECIDED", "STREAM_ACTIVE"}
    assert len(harness.tools.approved_calls) == 1


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_should_answer_410_and_record_expired_when_deciding_after_the_ttl(
    harness: ApprovalHarness, decision: str
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.clock.advance(TTL)
        response = await harness.decide(client, conversation_id, approval_id, decision)
        again = await harness.decide(client, conversation_id, approval_id, decision)

    assert (response.status_code, response.json()) == (410, EXPIRED)
    assert (again.status_code, again.json()) == (410, EXPIRED)
    assert harness.approvals.approvals[0].status is ApprovalStatus.EXPIRED
    action = harness.action(approval_id)
    assert (action.decision, action.outcome) == (None, ApprovalOutcome.EXPIRED)
    assert harness.tools.approved_calls == []


async def test_should_still_accept_a_decision_just_before_the_ttl(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.clock.advance(TTL - 1)
        harness.script(text_turn("Cancelled."))
        response = await harness.decide(client, conversation_id, approval_id)

    assert response.status_code == 200
    assert len(harness.tools.approved_calls) == 1


async def test_should_expire_the_approval_and_answer_when_a_message_comes_after_the_ttl(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.clock.advance(TTL)
        harness.script(text_turn("Your order was not changed."))
        response = await harness.post(client, conversation_id, "Never mind")

    assert response.status_code == 200
    assert data(sse(response), "message.end")["finishReason"] == "completed"
    assert harness.approvals.approvals[0].status is ApprovalStatus.EXPIRED
    assert harness.action(approval_id).outcome is ApprovalOutcome.EXPIRED
    # The paused call got its "expired, nothing changed" result before the new message.
    transcript = harness.model.requests[-1].transcript
    results = [item for item in transcript if item.kind == "tool_result"]
    assert results[-1].payload["error"] == "expired"
    assert transcript[-1].kind == "user"
    assert harness.tools.approved_calls == []


async def test_should_expire_the_approval_when_the_conversation_is_read_after_the_ttl(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, _ = await harness.propose(client)
        harness.clock.advance(TTL)
        response = await client.get(
            f"/chat/conversations/{conversation_id}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 200
    assert harness.approvals.approvals[0].status is ApprovalStatus.EXPIRED
    assert harness.action(approval_id).outcome is ApprovalOutcome.EXPIRED


async def test_should_keep_the_awaiting_approval_message_when_the_approval_expires(
    harness: ApprovalHarness,
) -> None:
    async with harness.client() as client:
        conversation_id, approval_id, events = await harness.propose(client)
        harness.clock.advance(TTL)
        await harness.decide(client, conversation_id, approval_id)

    assert names(events)[-1] == "message.end"
    messages = harness.store.messages[UUID(conversation_id)]
    assert messages[-1].finish_reason is FinishReason.AWAITING_APPROVAL
