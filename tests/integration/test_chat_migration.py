"""Migration 0003 creates the chat tables with their constraints and can be downgraded."""

from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.infrastructure.db.migrate import alembic_config

CHAT_TABLES = ("chat_conversations", "chat_messages", "chat_checkpoints", "chat_checkpoint_writes")


def _schema(connection: Connection) -> dict[str, Any]:
    inspector = inspect(connection)
    return {
        "tables": set(inspector.get_table_names()),
        "checks": {ck["name"] for ck in inspector.get_check_constraints("chat_messages")},
        "indexes": {
            index["name"]: tuple(index["column_names"])
            for table in ("chat_conversations", "chat_messages")
            for index in inspector.get_indexes(table)
        },
        "primary_keys": {
            table: inspector.get_pk_constraint(table)["constrained_columns"]
            for table in ("chat_checkpoints", "chat_checkpoint_writes")
        },
        "foreign_keys": [
            (fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("chat_messages")
        ],
    }


async def test_should_create_the_chat_tables_with_their_constraints(
    db_engine: AsyncEngine,
) -> None:
    async with db_engine.connect() as connection:
        schema = await connection.run_sync(_schema)

    assert schema["tables"] >= set(CHAT_TABLES)
    assert schema["checks"] == {
        "ck_chat_messages_role",
        "ck_chat_messages_finish_reason",
        "ck_chat_messages_user_content_length",
        "ck_chat_messages_assistant_fields",
        "ck_chat_messages_tokens_not_negative",
    }
    assert schema["indexes"] == {
        "ix_chat_conversations_user_id_updated_at": ("user_id", "updated_at", "id"),
        "ix_chat_messages_conversation_id_created_at": ("conversation_id", "created_at", "id"),
    }
    assert schema["primary_keys"] == {
        "chat_checkpoints": ["thread_id", "checkpoint_ns", "checkpoint_id"],
        "chat_checkpoint_writes": ["thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"],
    }
    assert schema["foreign_keys"] == [("chat_conversations", "CASCADE")]


async def _conversation(db_engine: AsyncEngine) -> UUID:
    conversation_id = uuid4()
    async with db_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO chat_conversations (id, user_id, created_at, updated_at)"
                " VALUES (:id, 'user-1', now(), now())"
            ),
            {"id": conversation_id},
        )
    return conversation_id


_ASSISTANT: dict[str, object] = {
    "role": "assistant",
    "content": "On its way.",
    "finish_reason": "completed",
    "prompt_version": "assistant.v1",
    "input_tokens": 10,
    "output_tokens": 2,
}
_USER: dict[str, object] = {
    "role": "user",
    "content": "Where is it?",
    "finish_reason": None,
    "prompt_version": None,
    "input_tokens": None,
    "output_tokens": None,
}


async def _insert(db_engine: AsyncEngine, conversation_id: UUID, row: dict[str, object]) -> None:
    async with db_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO chat_messages (id, conversation_id, role, content, finish_reason,"
                " prompt_version, input_tokens, output_tokens, created_at) VALUES (:id, :cid,"
                " :role, :content, :finish_reason, :prompt_version, :input_tokens,"
                " :output_tokens, now())"
            ),
            {"id": uuid4(), "cid": conversation_id, **row},
        )


@pytest.mark.parametrize(
    "row",
    [
        {**_ASSISTANT, "role": "system"},
        {**_ASSISTANT, "finish_reason": "done"},
        {**_ASSISTANT, "finish_reason": None},
        {**_ASSISTANT, "input_tokens": -1},
        {**_USER, "content": ""},
        {**_USER, "content": "x" * 2001},
        {**_USER, "finish_reason": "completed"},
    ],
)
async def test_should_reject_a_message_when_a_value_breaks_a_constraint(
    db_engine: AsyncEngine, row: dict[str, object]
) -> None:
    conversation_id = await _conversation(db_engine)

    with pytest.raises(IntegrityError):
        await _insert(db_engine, conversation_id, row)


async def test_should_cascade_messages_when_the_conversation_is_deleted(
    db_engine: AsyncEngine,
) -> None:
    conversation_id = await _conversation(db_engine)
    await _insert(db_engine, conversation_id, _USER)
    await _insert(db_engine, conversation_id, _ASSISTANT)

    async with db_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM chat_conversations WHERE id = :id"), {"id": conversation_id}
        )
        remaining = await connection.scalar(text("SELECT count(*) FROM chat_messages"))
    assert remaining == 0


async def test_should_drop_and_recreate_the_chat_tables_when_downgraded_and_upgraded(
    db_engine: AsyncEngine,
) -> None:
    def cycle(connection: Connection) -> tuple[set[str], set[str]]:
        command.downgrade(alembic_config(connection), "0002")
        downgraded = set(inspect(connection).get_table_names())
        command.upgrade(alembic_config(connection), "head")
        return downgraded, set(inspect(connection).get_table_names())

    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            downgraded, upgraded = await connection.run_sync(cycle)
        finally:
            await transaction.rollback()

    assert downgraded.isdisjoint(CHAT_TABLES)
    assert upgraded >= set(CHAT_TABLES)
