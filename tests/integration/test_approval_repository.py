"""``ApprovalRepository`` on PostgreSQL: atomic proposals, single-use decisions, audit, limits."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.assistant.model import (
    ApprovalOutcome,
    ApprovalSummary,
    Decision,
    FinishReason,
    Usage,
)
from ecommerce_ai.agents.assistant.ports import (
    Approval,
    ApprovalStatus,
    ApprovalStore,
    ProposalMessage,
)
from ecommerce_ai.infrastructure.db.approval_repository import ApprovalRepository
from ecommerce_ai.infrastructure.db.conversation_repository import ConversationRepository
from ecommerce_ai.infrastructure.db.tables import chat_actions

NOW = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
TTL = timedelta(seconds=600)
OWNER = "user-1"
OTHER = "user-2"


@pytest.fixture
def conversations(db_engine: AsyncEngine) -> ConversationRepository:
    return ConversationRepository(db_engine)


@pytest.fixture
def store(db_engine: AsyncEngine) -> ApprovalRepository:
    return ApprovalRepository(db_engine)


def _approval(conversation_id: UUID, *, proposed_at: datetime = NOW) -> Approval:
    order_id = uuid4()
    return Approval(
        id=uuid4(),
        conversation_id=conversation_id,
        user_id=OWNER,
        action="cancel_order",
        order_id=order_id,
        arguments={"orderId": str(order_id)},
        summary=ApprovalSummary(status="PENDING", total_amount=59.9, currency="EUR"),
        proposed_at=proposed_at,
        expires_at=proposed_at + TTL,
    )


def _message() -> ProposalMessage:
    return ProposalMessage(
        message_id=uuid4(),
        content="Shall I cancel it?",
        prompt_version="assistant.v2",
        model_id="claude-opus-5-5",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


async def _proposed(
    conversations: ConversationRepository, store: ApprovalStore, *, proposed_at: datetime = NOW
) -> Approval:
    conversation = await conversations.create(OWNER, None, None, proposed_at)
    approval = _approval(conversation.id, proposed_at=proposed_at)
    await store.create_proposal(approval, _message(), "req-1")
    return approval


async def _audit(engine: AsyncEngine, approval_id: UUID) -> dict[str, object]:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(
                    chat_actions.c.order_id,
                    chat_actions.c.decision,
                    chat_actions.c.outcome,
                    chat_actions.c.request_id,
                    chat_actions.c.decided_at,
                ).where(chat_actions.c.approval_id == approval_id)
            )
        ).one()
    return dict(row._mapping)


async def test_should_store_the_approval_audit_row_and_message_when_proposing(
    db_engine: AsyncEngine, conversations: ConversationRepository, store: ApprovalRepository
) -> None:
    approval = await _proposed(conversations, store)

    assert await store.pending_for(OWNER, approval.conversation_id) == approval
    audit = await _audit(db_engine, approval.id)
    assert list(audit.values()) == [approval.order_id, None, None, "req-1", None]
    history = await conversations.get(OWNER, approval.conversation_id)
    assert history is not None
    assert history.messages[-1].finish_reason is FinishReason.AWAITING_APPROVAL
    assert history.messages[-1].content == "Shall I cancel it?"


async def test_should_store_nothing_when_the_conversation_is_not_the_users(
    conversations: ConversationRepository, store: ApprovalRepository
) -> None:
    conversation = await conversations.create(OTHER, None, None, NOW)
    approval = _approval(conversation.id)

    with pytest.raises(LookupError):
        await store.create_proposal(approval, _message(), None)

    assert await store.proposals_since(OWNER, NOW) == 0
    history = await conversations.get(OTHER, conversation.id)
    assert history is not None
    assert history.messages == ()


async def test_should_refuse_a_second_pending_approval_and_roll_back_its_message(
    conversations: ConversationRepository, store: ApprovalRepository
) -> None:
    first = await _proposed(conversations, store)

    with pytest.raises(IntegrityError):
        await store.create_proposal(_approval(first.conversation_id), _message(), None)

    history = await conversations.get(OWNER, first.conversation_id)
    assert history is not None
    assert len(history.messages) == 1
    assert await store.proposals_since(OWNER, NOW) == 1


async def test_should_hide_an_approval_from_another_user_or_conversation(
    conversations: ConversationRepository, store: ApprovalRepository
) -> None:
    approval = await _proposed(conversations, store)
    cid = approval.conversation_id

    assert await store.get(OWNER, cid, approval.id) == approval
    assert await store.get(OTHER, cid, approval.id) is None
    assert await store.get(OWNER, uuid4(), approval.id) is None
    assert await store.pending_for(OTHER, cid) is None
    assert await store.decide(OTHER, cid, approval.id, Decision.APPROVE, NOW) is None


async def test_should_decide_once_and_audit_the_decision(
    db_engine: AsyncEngine, conversations: ConversationRepository, store: ApprovalRepository
) -> None:
    approval = await _proposed(conversations, store)
    cid = approval.conversation_id
    later = NOW + timedelta(seconds=30)

    decided = await store.decide(OWNER, cid, approval.id, Decision.APPROVE, later)
    again = await store.decide(OWNER, cid, approval.id, Decision.REJECT, later)

    assert decided == approval.model_copy(
        update={"status": ApprovalStatus.APPROVED, "decided_at": later}
    )
    assert again is None
    audit = await _audit(db_engine, approval.id)
    assert (audit["decision"], audit["outcome"], audit["decided_at"]) == ("approve", None, later)
    assert await store.awaiting_outcome(OWNER, cid) == decided
    assert await store.pending_for(OWNER, cid) is None


async def test_should_let_exactly_one_of_concurrent_decisions_win(
    conversations: ConversationRepository, store: ApprovalRepository
) -> None:
    approval = await _proposed(conversations, store)
    cid = approval.conversation_id

    results = await asyncio.gather(
        *(store.decide(OWNER, cid, approval.id, Decision.APPROVE, NOW) for _ in range(5))
    )

    assert sum(result is not None for result in results) == 1


async def test_should_not_decide_an_approval_from_its_expiry_on(
    db_engine: AsyncEngine, conversations: ConversationRepository, store: ApprovalRepository
) -> None:
    approval = await _proposed(conversations, store)

    decided = await store.decide(
        OWNER, approval.conversation_id, approval.id, Decision.APPROVE, NOW + TTL
    )

    assert decided is None
    assert (await _audit(db_engine, approval.id))["decision"] is None


async def test_should_set_the_audit_outcome_once(
    db_engine: AsyncEngine, conversations: ConversationRepository, store: ApprovalRepository
) -> None:
    approval = await _proposed(conversations, store)
    cid = approval.conversation_id
    await store.decide(OWNER, cid, approval.id, Decision.REJECT, NOW)

    await store.record_outcome(approval.id, ApprovalOutcome.REJECTED)
    await store.record_outcome(approval.id, ApprovalOutcome.FAILED)

    assert (await _audit(db_engine, approval.id))["outcome"] == "rejected"
    assert await store.awaiting_outcome(OWNER, cid) is None


async def test_should_close_only_a_pending_approval(
    db_engine: AsyncEngine, conversations: ConversationRepository, store: ApprovalRepository
) -> None:
    approval = await _proposed(conversations, store)
    expired_at = NOW + TTL

    closed = await store.close(
        approval.id, ApprovalStatus.EXPIRED, ApprovalOutcome.EXPIRED, expired_at
    )
    again = await store.close(
        approval.id, ApprovalStatus.WITHDRAWN, ApprovalOutcome.WITHDRAWN, expired_at
    )

    assert (closed, again) == (True, False)
    stored = await store.get(OWNER, approval.conversation_id, approval.id)
    assert stored is not None
    assert stored.status is ApprovalStatus.EXPIRED
    audit = await _audit(db_engine, approval.id)
    assert (audit["outcome"], audit["decided_at"]) == ("expired", expired_at)
    with pytest.raises(ValueError, match="close only"):
        await store.close(approval.id, ApprovalStatus.APPROVED, ApprovalOutcome.FAILED, NOW)


async def test_should_keep_the_audit_row_and_count_it_when_the_conversation_is_deleted(
    db_engine: AsyncEngine, conversations: ConversationRepository, store: ApprovalRepository
) -> None:
    approval = await _proposed(conversations, store)
    await store.close(approval.id, ApprovalStatus.WITHDRAWN, ApprovalOutcome.WITHDRAWN, NOW)

    assert await conversations.delete(OWNER, approval.conversation_id)

    assert await store.get(OWNER, approval.conversation_id, approval.id) is None
    assert (await _audit(db_engine, approval.id))["outcome"] == "withdrawn"
    assert await store.proposals_since(OWNER, NOW) == 1


async def test_should_count_only_the_users_proposals_inside_the_window(
    conversations: ConversationRepository, store: ApprovalRepository
) -> None:
    await _proposed(conversations, store, proposed_at=NOW - timedelta(hours=2))
    await _proposed(conversations, store, proposed_at=NOW - timedelta(minutes=30))
    await _proposed(conversations, store, proposed_at=NOW)

    assert await store.proposals_since(OWNER, NOW - timedelta(hours=1)) == 2
    assert await store.proposals_since(OTHER, NOW - timedelta(hours=1)) == 0
