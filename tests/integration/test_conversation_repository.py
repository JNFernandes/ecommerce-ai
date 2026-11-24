"""``ConversationRepository`` on PostgreSQL: ownership, ordering, paging, cascade and counts."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Table, func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.assistant.model import FinishReason, MessageRole, Usage
from ecommerce_ai.agents.assistant.ports import ConversationStore
from ecommerce_ai.infrastructure.db.conversation_repository import ConversationRepository
from ecommerce_ai.infrastructure.db.tables import (
    chat_checkpoint_writes,
    chat_checkpoints,
    chat_messages,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
CUSTOMER_ID = UUID("00000000-0000-4000-8000-0000000000c1")
OWNER = "user-1"
OTHER = "user-2"


@pytest.fixture
def store(db_engine: AsyncEngine) -> ConversationStore:
    return ConversationRepository(db_engine)


async def _count(engine: AsyncEngine, table: Table) -> int:
    async with engine.connect() as connection:
        count: int = (
            await connection.execute(select(func.count()).select_from(table))
        ).scalar_one()
    return count


async def _add_checkpoint_rows(engine: AsyncEngine, thread_id: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            insert(chat_checkpoints).values(
                thread_id=thread_id,
                checkpoint_ns="",
                checkpoint_id="1",
                type="json",
                checkpoint=b"{}",
                metadata={},
                created_at=NOW,
            )
        )
        await connection.execute(
            insert(chat_checkpoint_writes).values(
                thread_id=thread_id,
                checkpoint_ns="",
                checkpoint_id="1",
                task_id="t",
                idx=0,
                channel="transcript",
                type="json",
                value=b"[]",
            )
        )


async def test_should_create_an_empty_conversation_when_given_a_title(
    store: ConversationStore,
) -> None:
    created = await store.create(OWNER, CUSTOMER_ID, "My order", NOW)

    assert created.title == "My order"
    assert created.created_at == created.updated_at == NOW
    history = await store.get(OWNER, created.id)
    assert history is not None
    assert history.conversation == created
    assert history.messages == ()


async def test_should_list_newest_updated_first_with_total_limit_and_offset(
    store: ConversationStore,
) -> None:
    first = await store.create(OWNER, None, "first", NOW)
    second = await store.create(OWNER, None, "second", NOW + timedelta(minutes=1))
    third = await store.create(OWNER, None, None, NOW + timedelta(minutes=2))
    await store.create(OTHER, None, "foreign", NOW + timedelta(minutes=3))
    await store.add_user_message(OWNER, first.id, uuid4(), "hi", NOW + timedelta(minutes=5))

    page = await store.list(OWNER, limit=2, offset=0)
    rest = await store.list(OWNER, limit=2, offset=2)

    assert page.total == rest.total == 3
    assert [c.id for c in page.items] == [first.id, third.id]
    assert [c.id for c in rest.items] == [second.id]
    assert page.items[0].updated_at == NOW + timedelta(minutes=5)


async def test_should_return_history_oldest_first_with_assistant_fields(
    store: ConversationStore,
) -> None:
    conversation = await store.create(OWNER, CUSTOMER_ID, None, NOW)
    question = await store.add_user_message(OWNER, conversation.id, uuid4(), "Where is it?", NOW)
    answer = await store.add_assistant_message(
        OWNER,
        conversation.id,
        uuid4(),
        "It is SHIPPED.",
        FinishReason.COMPLETED,
        "assistant.v1",
        "claude-opus-5-5",
        Usage(input_tokens=120, output_tokens=8),
        NOW + timedelta(seconds=3),
    )

    history = await store.get(OWNER, conversation.id)

    assert history is not None
    assert history.messages == (question, answer)
    assert question.role is MessageRole.USER
    assert question.usage is None
    assert question.finish_reason is None
    assert answer.role is MessageRole.ASSISTANT
    assert answer.finish_reason is FinishReason.COMPLETED
    assert answer.usage == Usage(input_tokens=120, output_tokens=8)
    assert history.conversation.updated_at == NOW + timedelta(seconds=3)


async def test_should_treat_another_users_conversation_as_unknown_on_every_call(
    store: ConversationStore,
) -> None:
    foreign = await store.create(OTHER, None, "theirs", NOW)

    assert await store.get(OWNER, foreign.id) is None
    assert await store.count_messages(OWNER, foreign.id) is None
    assert await store.delete(OWNER, foreign.id) is False
    assert (await store.list(OWNER, 20, 0)).total == 0
    with pytest.raises(LookupError):
        await store.add_user_message(OWNER, foreign.id, uuid4(), "hi", NOW)
    with pytest.raises(LookupError):
        await store.add_assistant_message(
            OWNER,
            foreign.id,
            uuid4(),
            "x",
            FinishReason.COMPLETED,
            "assistant.v1",
            None,
            Usage(),
            NOW,
        )
    assert await store.count_messages(OTHER, foreign.id) == 0


async def test_should_return_none_or_false_when_the_conversation_is_unknown(
    store: ConversationStore,
) -> None:
    unknown = uuid4()

    assert await store.get(OWNER, unknown) is None
    assert await store.count_messages(OWNER, unknown) is None
    assert await store.delete(OWNER, unknown) is False


async def test_should_count_messages_when_some_are_stored(store: ConversationStore) -> None:
    conversation = await store.create(OWNER, None, None, NOW)
    for index in range(3):
        await store.add_user_message(OWNER, conversation.id, uuid4(), f"q{index}", NOW)

    assert await store.count_messages(OWNER, conversation.id) == 3


async def test_should_delete_messages_and_checkpoint_rows_when_deleting_a_conversation(
    db_engine: AsyncEngine, store: ConversationStore
) -> None:
    conversation = await store.create(OWNER, None, None, NOW)
    kept = await store.create(OWNER, None, None, NOW)
    await store.add_user_message(OWNER, conversation.id, uuid4(), "hi", NOW)
    await store.add_user_message(OWNER, kept.id, uuid4(), "hello", NOW)
    await _add_checkpoint_rows(db_engine, str(conversation.id))
    await _add_checkpoint_rows(db_engine, str(kept.id))

    assert await store.delete(OWNER, conversation.id) is True

    assert await store.get(OWNER, conversation.id) is None
    assert await _count(db_engine, chat_messages) == 1
    assert await _count(db_engine, chat_checkpoints) == 1
    assert await _count(db_engine, chat_checkpoint_writes) == 1
    assert await store.delete(OWNER, conversation.id) is False
