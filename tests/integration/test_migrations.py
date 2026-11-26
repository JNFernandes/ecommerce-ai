"""The packaged migrations create the triage and chat schema and can be re-applied safely."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.infrastructure.db.migrate import alembic_config, run_migrations
from ecommerce_ai.infrastructure.db.tables import metadata
from ecommerce_ai.infrastructure.settings import Settings

TABLES = {
    "triage_cases",
    "triage_evidence",
    "inbound_dead_letters",
    "triage_results",
    "outbox_events",
    "llm_token_usage",
    "chat_conversations",
    "chat_messages",
    "chat_checkpoints",
    "chat_checkpoint_writes",
    "guardrail_events",
    "chat_user_usage",
    "llm_calls",
}


def _schema(connection: Connection) -> dict[str, object]:
    inspector = inspect(connection)
    return {
        "tables": set(inspector.get_table_names()),
        "unique": {
            table: {tuple(uq["column_names"]) for uq in inspector.get_unique_constraints(table)}
            for table in ("triage_cases", "triage_evidence")
        },
        "checks": {ck["name"] for ck in inspector.get_check_constraints("triage_cases")},
        "foreign_keys": [
            (fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("triage_evidence")
        ],
    }


async def test_should_create_the_triage_tables_with_their_constraints(
    db_engine: AsyncEngine,
) -> None:
    async with db_engine.connect() as connection:
        schema = await connection.run_sync(_schema)

    assert schema["tables"] >= TABLES  # type: ignore[operator]  # a set, see _schema
    assert schema["unique"] == {
        "triage_cases": {("order_id",)},
        "triage_evidence": {("event_id",)},
    }
    assert schema["checks"] == {"ck_triage_cases_cause", "ck_triage_cases_status"}
    assert schema["foreign_keys"] == [("triage_cases", "CASCADE")]


async def test_should_match_the_table_definitions_when_migrated_to_head(
    db_engine: AsyncEngine,
) -> None:
    def differences(connection: Connection) -> list[object]:
        return list(compare_metadata(MigrationContext.configure(connection), metadata))

    async with db_engine.connect() as connection:
        assert await connection.run_sync(differences) == []


async def test_should_change_nothing_when_the_migrations_run_again(
    db_settings: Settings, db_engine: AsyncEngine
) -> None:
    await run_migrations(db_settings)

    async with db_engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == "0006"


@pytest.mark.parametrize(("cause", "status"), [("BOGUS", "PENDING_TRIAGE"), ("UNKNOWN", "DONE")])
async def test_should_reject_a_case_when_its_cause_or_status_is_unknown(
    db_engine: AsyncEngine, cause: str, status: str
) -> None:
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        async with db_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO triage_cases (id, order_id, cause, status, created_at, updated_at)"
                    " VALUES (:id, :order_id, :cause, :status, :now, :now)"
                ),
                {"id": uuid4(), "order_id": uuid4(), "cause": cause, "status": status, "now": now},
            )


def _new_tables(connection: Connection) -> dict[str, Any]:
    inspector = inspect(connection)
    tables = ("triage_results", "outbox_events", "llm_token_usage")
    return {
        "unique": {
            table: {tuple(uq["column_names"]) for uq in inspector.get_unique_constraints(table)}
            for table in tables
        },
        "checks": {
            table: {ck["name"] for ck in inspector.get_check_constraints(table)} for table in tables
        },
        "indexes": {
            index["name"]: tuple(index["column_names"])
            for table in ("triage_cases", "outbox_events")
            for index in inspector.get_indexes(table)
            if "duplicates_constraint" not in index  # the index behind a UNIQUE constraint
        },
        "primary_key": inspector.get_pk_constraint("llm_token_usage")["constrained_columns"],
        "foreign_keys": [
            (fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("triage_results")
        ],
    }


async def test_should_create_the_result_outbox_and_budget_tables_with_their_constraints(
    db_engine: AsyncEngine,
) -> None:
    async with db_engine.connect() as connection:
        schema = await connection.run_sync(_new_tables)

    assert schema["unique"] == {
        "triage_results": {("case_id",), ("event_id",)},
        "outbox_events": {("event_id",)},
        "llm_token_usage": set(),
    }
    assert schema["checks"] == {
        "triage_results": {
            "ck_triage_results_cause",
            "ck_triage_results_explanation_length",
            "ck_triage_results_suggested_action",
            "ck_triage_results_confidence_range",
            "ck_triage_results_source",
            "ck_triage_results_model_id_iff_llm",
            "ck_triage_results_tokens_not_negative",
            "ck_triage_results_latency_not_negative",
            "ck_triage_results_revisions_range",
        },
        "outbox_events": {"ck_outbox_events_status", "ck_outbox_events_attempts_not_negative"},
        "llm_token_usage": {"ck_llm_token_usage_tokens_not_negative"},
    }
    assert schema["indexes"] == {
        "ix_triage_cases_status_updated_at": ("status", "updated_at"),
        "ix_outbox_events_status_next_attempt_at": ("status", "next_attempt_at"),
    }
    assert schema["primary_key"] == ["day"]
    assert schema["foreign_keys"] == [("triage_cases", "CASCADE")]


async def _insert_case(db_engine: AsyncEngine, status: str = "PENDING_TRIAGE") -> UUID:
    case_id = uuid4()
    now = datetime.now(UTC)
    async with db_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO triage_cases (id, order_id, cause, status, created_at, updated_at)"
                " VALUES (:id, :order_id, 'UNKNOWN', :status, :now, :now)"
            ),
            {"id": case_id, "order_id": uuid4(), "status": status, "now": now},
        )
    return case_id


async def test_should_accept_a_completed_case_when_migrated_to_head(
    db_engine: AsyncEngine,
) -> None:
    case_id = await _insert_case(db_engine, status="COMPLETED")

    async with db_engine.connect() as connection:
        status = await connection.scalar(
            text("SELECT status FROM triage_cases WHERE id = :id"), {"id": case_id}
        )
    assert status == "COMPLETED"


def _result_row(case_id: UUID, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": uuid4(),
        "case_id": case_id,
        "event_id": uuid4(),
        "cause": "UNKNOWN",
        "explanation": "Fine.",
        "suggested_action": "REVIEW",
        "confidence": 0.3,
        "source": "FALLBACK_TEMPLATE",
        "prompt_version": "triage.v1",
        "model_id": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "latency_ms": 0,
        "revisions": 0,
        "violations": json.dumps([]),
        "created_at": datetime.now(UTC),
    }
    row.update(overrides)
    return row


async def _insert_result(db_engine: AsyncEngine, row: dict[str, object]) -> None:
    async with db_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO triage_results (id, case_id, event_id, cause, explanation,"
                " suggested_action, confidence, source, prompt_version, model_id, input_tokens,"
                " output_tokens, latency_ms, revisions, violations, created_at) VALUES (:id,"
                " :case_id, :event_id, :cause, :explanation, :suggested_action, :confidence,"
                " :source, :prompt_version, :model_id, :input_tokens, :output_tokens,"
                " :latency_ms, :revisions, CAST(:violations AS jsonb), :created_at)"
            ),
            row,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "FALLBACK_TEMPLATE", "model_id": None},
        {"source": "LLM", "model_id": "claude-opus-5-5"},
    ],
)
async def test_should_store_a_result_when_model_id_matches_the_source(
    db_engine: AsyncEngine, overrides: dict[str, object]
) -> None:
    case_id = await _insert_case(db_engine)

    await _insert_result(db_engine, _result_row(case_id, **overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "LLM", "model_id": None},
        {"source": "FALLBACK_TEMPLATE", "model_id": "claude-opus-5-5"},
        {"source": "BOGUS"},
        {"cause": "BOGUS"},
        {"suggested_action": "REFUND"},
        {"explanation": ""},
        {"explanation": "x" * 501},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"input_tokens": -1},
        {"output_tokens": -1},
        {"latency_ms": -1},
        {"revisions": 3},
    ],
)
async def test_should_reject_a_result_when_a_value_breaks_a_constraint(
    db_engine: AsyncEngine, overrides: dict[str, object]
) -> None:
    case_id = await _insert_case(db_engine)

    with pytest.raises(IntegrityError):
        await _insert_result(db_engine, _result_row(case_id, **overrides))


async def test_should_reject_a_second_result_when_the_case_already_has_one(
    db_engine: AsyncEngine,
) -> None:
    case_id = await _insert_case(db_engine)
    await _insert_result(db_engine, _result_row(case_id))

    with pytest.raises(IntegrityError):
        await _insert_result(db_engine, _result_row(case_id))


@pytest.mark.parametrize(("status", "attempts"), [("SENT", 0), ("PENDING", -1)])
async def test_should_reject_an_outbox_event_when_its_status_or_attempts_are_invalid(
    db_engine: AsyncEngine, status: str, attempts: int
) -> None:
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        async with db_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO outbox_events (id, event_id, topic, message_key, payload, status,"
                    " attempts, next_attempt_at, created_at, updated_at) VALUES (:id, :event_id,"
                    " 'ai.order-triage-completed', 'k', '{}', :status, :attempts, :now, :now,"
                    " :now)"
                ),
                {
                    "id": uuid4(),
                    "event_id": uuid4(),
                    "status": status,
                    "attempts": attempts,
                    "now": now,
                },
            )


async def test_should_reject_negative_token_usage_when_stored(db_engine: AsyncEngine) -> None:
    with pytest.raises(IntegrityError):
        async with db_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO llm_token_usage (day, tokens, updated_at)"
                    " VALUES (CURRENT_DATE, -1, now())"
                )
            )


GUARD_TABLES = ("guardrail_events", "chat_user_usage")
CHAT_TOOLS = [
    "get_order",
    "list_my_orders",
    "list_products",
    "get_product",
    "list_my_notifications",
    "cancel_order",
]
GUARD_EVENT_SQL = (
    "INSERT INTO guardrail_events (id, user_id, stage, rule, action, severity, created_at)"
    " VALUES (gen_random_uuid(), 'user-1', :stage, 'rule', :action, :severity, now())"
)


def _guard_schema(connection: Connection) -> dict[str, Any]:
    inspector = inspect(connection)
    return {
        "checks": {
            table: {ck["name"] for ck in inspector.get_check_constraints(table)}
            for table in GUARD_TABLES
        },
        "indexes": {
            index["name"]: tuple(index["column_names"])
            for index in inspector.get_indexes("guardrail_events")
        },
        "primary_keys": {
            table: tuple(inspector.get_pk_constraint(table)["constrained_columns"])
            for table in GUARD_TABLES
        },
        "foreign_keys": inspector.get_foreign_keys("guardrail_events"),
    }


async def _guard_event(db_engine: AsyncEngine) -> UUID:
    event_id = uuid4()
    async with db_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO guardrail_events (id, user_id, conversation_id, request_id, stage,"
                " rule, action, severity, created_at) VALUES (:id, 'user-1', :cid, 'req-1',"
                " 'input', 'instruction_override', 'blocked', 'high', now())"
            ),
            {"id": event_id, "cid": uuid4()},
        )
    return event_id


async def _insert_conversation(db_engine: AsyncEngine) -> UUID:
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


async def _insert_reply(db_engine: AsyncEngine, conversation_id: UUID, reason: str) -> None:
    async with db_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO chat_messages (id, conversation_id, role, content, finish_reason,"
                " prompt_version, input_tokens, output_tokens, created_at) VALUES (:id, :cid,"
                " 'assistant', 'Sorry.', :reason, 'assistant.v3', 0, 0, now())"
            ),
            {"id": uuid4(), "cid": conversation_id, "reason": reason},
        )


async def test_should_create_the_guard_tables_with_their_constraints(
    db_engine: AsyncEngine,
) -> None:
    async with db_engine.connect() as connection:
        schema = await connection.run_sync(_guard_schema)

    assert schema["checks"] == {
        "guardrail_events": {
            "ck_guardrail_events_stage",
            "ck_guardrail_events_action",
            "ck_guardrail_events_severity",
        },
        "chat_user_usage": {
            "ck_chat_user_usage_messages_not_negative",
            "ck_chat_user_usage_tokens_not_negative",
        },
    }
    assert schema["indexes"] == {
        "ix_guardrail_events_user_id_created_at": ("user_id", "created_at")
    }
    assert schema["primary_keys"] == {
        "guardrail_events": ("id",),
        "chat_user_usage": ("user_id", "day"),
    }
    assert schema["foreign_keys"] == []


@pytest.mark.parametrize("column", ["stage", "action", "severity"])
async def test_should_reject_a_guard_event_when_a_value_is_unknown(
    db_engine: AsyncEngine, column: str
) -> None:
    values = {"stage": "input", "action": "blocked", "severity": "high", column: "bogus"}

    with pytest.raises(IntegrityError):
        async with db_engine.begin() as connection:
            await connection.execute(text(GUARD_EVENT_SQL), values)


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE guardrail_events SET severity = 'low' WHERE id = :id",
        "UPDATE guardrail_events SET user_id = 'user-2' WHERE id = :id",
        "DELETE FROM guardrail_events WHERE id = :id",
    ],
    ids=["update-severity", "update-user", "delete"],
)
async def test_should_reject_a_change_to_the_guard_audit_when_the_trigger_forbids_it(
    db_engine: AsyncEngine, change: str
) -> None:
    event_id = await _guard_event(db_engine)

    with pytest.raises(DBAPIError, match="guardrail_events is append-only"):
        async with db_engine.begin() as connection:
            await connection.execute(text(change), {"id": event_id})


@pytest.mark.parametrize(("messages", "tokens"), [(-1, 0), (0, -1)])
async def test_should_reject_negative_user_usage_when_stored(
    db_engine: AsyncEngine, messages: int, tokens: int
) -> None:
    with pytest.raises(IntegrityError):
        async with db_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO chat_user_usage (user_id, day, messages, tokens, updated_at)"
                    " VALUES ('user-1', current_date, :messages, :tokens, now())"
                ),
                {"messages": messages, "tokens": tokens},
            )


async def test_should_give_a_conversation_no_tool_when_it_is_stored_without_an_allowlist(
    db_engine: AsyncEngine,
) -> None:
    conversation_id = await _insert_conversation(db_engine)

    async with db_engine.connect() as connection:
        allowlist = await connection.scalar(
            text("SELECT tool_allowlist FROM chat_conversations WHERE id = :id"),
            {"id": conversation_id},
        )
    assert allowlist == []


@pytest.mark.parametrize("reason", ["blocked", "tool_loop"])
async def test_should_store_a_guarded_reply_when_its_finish_reason_is_new(
    db_engine: AsyncEngine, reason: str
) -> None:
    await _insert_reply(db_engine, await _insert_conversation(db_engine), reason)


async def test_should_backfill_and_map_back_when_0005_is_downgraded_and_upgraded(
    db_engine: AsyncEngine,
) -> None:
    conversation_id = await _insert_conversation(db_engine)
    await _insert_reply(db_engine, conversation_id, "blocked")
    await _insert_reply(db_engine, conversation_id, "tool_loop")

    def cycle(connection: Connection) -> tuple[set[str], set[str], set[object], object]:
        command.downgrade(alembic_config(connection), "0004")
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        columns = {c["name"] for c in inspector.get_columns("chat_conversations")}
        reasons = set(connection.scalars(text("SELECT finish_reason FROM chat_messages")))
        command.upgrade(alembic_config(connection), "head")
        allowlist = connection.scalar(text("SELECT tool_allowlist FROM chat_conversations"))
        return tables, columns, reasons, allowlist

    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            tables, columns, reasons, allowlist = await connection.run_sync(cycle)
        finally:
            await transaction.rollback()

    assert tables.isdisjoint(GUARD_TABLES)
    assert "tool_allowlist" not in columns
    assert reasons == {"replaced"}
    assert allowlist == CHAT_TOOLS
