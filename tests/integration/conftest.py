"""Database fixtures shared by the integration tests.

The database comes from ``TEST_DATABASE_URL`` when it is set (for example the compose
``postgres-ai`` on port 5436; its triage tables are truncated before every test, so never point
it at data you want to keep), otherwise from a Testcontainers ``postgres:16-alpine``. Without
either, every integration test is skipped, unless ``INTEGRATION_REQUIRED=true`` (set in
CI), which turns the skip into a failure so a broken Docker setup cannot pass silently. The
schema is created once per session by the real migrations.
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from ecommerce_ai.infrastructure.db.engine import build_engine
from ecommerce_ai.infrastructure.db.migrate import run_migrations
from ecommerce_ai.infrastructure.settings import Settings

POSTGRES_IMAGE = "postgres:16-alpine"
INTEGRATION_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test of this directory ``integration``."""
    for item in items:
        if INTEGRATION_DIR in item.path.parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """URL of a PostgreSQL 16 database reserved for the test session."""
    configured = os.environ.get("TEST_DATABASE_URL")
    if configured:
        yield configured
        return
    container = PostgresContainer(POSTGRES_IMAGE, driver=None)
    try:
        container.start()
    except Exception as exc:  # any Docker failure means "no database here": skip, never pass
        message = f"no PostgreSQL for integration tests ({type(exc).__name__})"
        if os.environ.get("INTEGRATION_REQUIRED") == "true":
            pytest.fail(message)
        pytest.skip(message)
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture(scope="session")
async def db_settings(database_url: str) -> Settings:
    """Settings bound to the test database, with the schema migrated to head."""
    settings = Settings(database_url=SecretStr(database_url))
    await run_migrations(settings)
    return settings


@pytest.fixture(scope="session")
async def db_engine(db_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Engine on the migrated test database, disposed at the end of the session."""
    engine = build_engine(db_settings)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _empty_tables(db_engine: AsyncEngine) -> None:
    """Start every integration test from empty triage tables."""
    async with db_engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE triage_evidence, triage_cases, inbound_dead_letters")
        )
