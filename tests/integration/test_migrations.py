"""The packaged migrations create the triage schema and can be re-applied safely."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.infrastructure.db.migrate import run_migrations
from ecommerce_ai.infrastructure.db.tables import metadata
from ecommerce_ai.infrastructure.settings import Settings

TABLES = {"triage_cases", "triage_evidence", "inbound_dead_letters"}


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
    assert version == "0001"


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
