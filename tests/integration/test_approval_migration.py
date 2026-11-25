"""Migration 0004: approval tables, the append-only audit trigger and ``awaiting_approval``."""

from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.infrastructure.db.migrate import alembic_config

APPROVAL_TABLES = ("chat_approvals", "chat_actions")


def _schema(connection: Connection) -> dict[str, Any]:
    inspector = inspect(connection)
    return {
        "tables": set(inspector.get_table_names()),
        "checks": {
            table: {ck["name"] for ck in inspector.get_check_constraints(table)}
            for table in APPROVAL_TABLES
        },
        "indexes": {
            index["name"]: (tuple(index["column_names"]), bool(index["unique"]))
            for table in APPROVAL_TABLES
            for index in inspector.get_indexes(table)
            if "duplicates_constraint" not in index  # the unique constraint's own index
        },
        "unique": {uq["name"] for uq in inspector.get_unique_constraints("chat_actions")},
        "foreign_keys": {
            table: [
                (fk["referred_table"], fk["options"].get("ondelete"))
                for fk in inspector.get_foreign_keys(table)
            ]
            for table in APPROVAL_TABLES
        },
    }


async def test_should_create_the_approval_tables_with_their_constraints(
    db_engine: AsyncEngine,
) -> None:
    async with db_engine.connect() as connection:
        schema = await connection.run_sync(_schema)

    assert schema["tables"] >= set(APPROVAL_TABLES)
    assert schema["checks"] == {
        "chat_approvals": {"ck_chat_approvals_status", "ck_chat_approvals_expires_after_proposal"},
        "chat_actions": {"ck_chat_actions_decision", "ck_chat_actions_outcome"},
    }
    assert schema["indexes"] == {
        "ux_chat_approvals_conversation_id_pending": (("conversation_id",), True),
        "ix_chat_actions_user_id_proposed_at": (("user_id", "proposed_at"), False),
    }
    assert schema["unique"] == {"uq_chat_actions_approval_id"}
    assert schema["foreign_keys"] == {
        "chat_approvals": [("chat_conversations", "CASCADE")],
        "chat_actions": [],
    }


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


async def _approval(db_engine: AsyncEngine, conversation_id: UUID, status: str = "pending") -> None:
    async with db_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO chat_approvals (id, conversation_id, user_id, action, arguments,"
                " summary, status, proposed_at, expires_at) VALUES (:id, :cid, 'user-1',"
                " 'cancel_order', '{}', '{}', :status, now(), now() + interval '10 minutes')"
            ),
            {"id": uuid4(), "cid": conversation_id, "status": status},
        )


async def _action(db_engine: AsyncEngine, conversation_id: UUID) -> UUID:
    action_id = uuid4()
    async with db_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO chat_actions (id, approval_id, conversation_id, user_id, action,"
                " order_id, request_id, proposed_at) VALUES (:id, :aid, :cid, 'user-1',"
                " 'cancel_order', :oid, 'req-1', now())"
            ),
            {"id": action_id, "aid": uuid4(), "cid": conversation_id, "oid": uuid4()},
        )
    return action_id


async def _execute(db_engine: AsyncEngine, sql: str, params: dict[str, object]) -> None:
    async with db_engine.begin() as connection:
        await connection.execute(text(sql), params)


async def test_should_allow_one_pending_approval_per_conversation(
    db_engine: AsyncEngine,
) -> None:
    conversation_id = await _conversation(db_engine)
    await _approval(db_engine, conversation_id)
    await _approval(db_engine, conversation_id, status="rejected")

    with pytest.raises(IntegrityError):
        await _approval(db_engine, conversation_id)


@pytest.mark.parametrize("change", ["status = 'done'", "expires_at = proposed_at"])
async def test_should_reject_an_approval_when_a_value_breaks_a_constraint(
    db_engine: AsyncEngine, change: str
) -> None:
    conversation_id = await _conversation(db_engine)
    await _approval(db_engine, conversation_id)

    with pytest.raises(IntegrityError):
        await _execute(
            db_engine,
            f"UPDATE chat_approvals SET {change}",  # noqa: S608  # fixed fragments
            {},
        )


async def test_should_delete_approvals_but_keep_audit_rows_when_the_conversation_is_deleted(
    db_engine: AsyncEngine,
) -> None:
    conversation_id = await _conversation(db_engine)
    await _approval(db_engine, conversation_id)
    await _action(db_engine, conversation_id)

    async with db_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM chat_conversations WHERE id = :id"), {"id": conversation_id}
        )
        approvals = await connection.scalar(text("SELECT count(*) FROM chat_approvals"))
        actions = await connection.scalar(text("SELECT count(*) FROM chat_actions"))

    assert (approvals, actions) == (0, 1)


async def test_should_complete_an_audit_row_once_with_its_decision_and_outcome(
    db_engine: AsyncEngine,
) -> None:
    action_id = await _action(db_engine, await _conversation(db_engine))

    await _execute(
        db_engine,
        "UPDATE chat_actions SET decision = 'approve', decided_at = now() WHERE id = :id",
        {"id": action_id},
    )
    await _execute(
        db_engine, "UPDATE chat_actions SET outcome = 'cancelled' WHERE id = :id", {"id": action_id}
    )

    async with db_engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT decision, outcome FROM chat_actions WHERE id = :id"),
                {"id": action_id},
            )
        ).one()
    assert tuple(row) == ("approve", "cancelled")


@pytest.mark.parametrize(
    ("setup", "change"),
    [
        (None, "DELETE FROM chat_actions WHERE id = :id"),
        ("outcome = 'rejected'", "UPDATE chat_actions SET decided_at = now() WHERE id = :id"),
        (None, "UPDATE chat_actions SET order_id = gen_random_uuid() WHERE id = :id"),
        (None, "UPDATE chat_actions SET user_id = 'user-2' WHERE id = :id"),
        ("decision = 'reject'", "UPDATE chat_actions SET decision = 'approve' WHERE id = :id"),
    ],
    ids=["delete", "after-outcome", "order-id", "user-id", "second-decision"],
)
async def test_should_reject_a_change_to_the_audit_trail_when_the_trigger_forbids_it(
    db_engine: AsyncEngine, setup: str | None, change: str
) -> None:
    action_id = await _action(db_engine, await _conversation(db_engine))
    if setup is not None:
        await _execute(
            db_engine,
            f"UPDATE chat_actions SET {setup} WHERE id = :id",  # noqa: S608  # fixed fragments
            {"id": action_id},
        )

    with pytest.raises(DBAPIError, match="chat_actions"):
        await _execute(db_engine, change, {"id": action_id})


@pytest.mark.parametrize("column", ["decision", "outcome"])
async def test_should_reject_an_audit_row_when_a_value_is_unknown(
    db_engine: AsyncEngine, column: str
) -> None:
    action_id = await _action(db_engine, await _conversation(db_engine))

    with pytest.raises(IntegrityError):
        await _execute(
            db_engine,
            f"UPDATE chat_actions SET {column} = 'bogus' WHERE id = :id",  # noqa: S608  # fixed
            {"id": action_id},
        )


async def test_should_store_an_awaiting_approval_message(db_engine: AsyncEngine) -> None:
    conversation_id = await _conversation(db_engine)

    await _execute(
        db_engine,
        "INSERT INTO chat_messages (id, conversation_id, role, content, finish_reason,"
        " prompt_version, input_tokens, output_tokens, created_at) VALUES (:id, :cid,"
        " 'assistant', 'Shall I cancel it?', 'awaiting_approval', 'assistant.v2', 1, 1, now())",
        {"id": uuid4(), "cid": conversation_id},
    )


async def test_should_drop_and_recreate_the_approval_tables_when_downgraded_and_upgraded(
    db_engine: AsyncEngine,
) -> None:
    conversation_id = await _conversation(db_engine)
    await _execute(
        db_engine,
        "INSERT INTO chat_messages (id, conversation_id, role, content, finish_reason,"
        " prompt_version, input_tokens, output_tokens, created_at) VALUES (:id, :cid,"
        " 'assistant', 'Shall I cancel it?', 'awaiting_approval', 'assistant.v2', 1, 1, now())",
        {"id": uuid4(), "cid": conversation_id},
    )

    def cycle(connection: Connection) -> tuple[set[str], object, set[str]]:
        command.downgrade(alembic_config(connection), "0003")
        downgraded = set(inspect(connection).get_table_names())
        reason = connection.scalar(text("SELECT finish_reason FROM chat_messages"))
        command.upgrade(alembic_config(connection), "head")
        return downgraded, reason, set(inspect(connection).get_table_names())

    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            downgraded, reason, upgraded = await connection.run_sync(cycle)
        finally:
            await transaction.rollback()

    assert downgraded.isdisjoint(APPROVAL_TABLES)
    assert reason == "completed"
    assert upgraded >= set(APPROVAL_TABLES)
