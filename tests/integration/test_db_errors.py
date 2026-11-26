"""Regression: a wrong database login is reported by its real cause, and migrate is re-runnable.

The consumer used to report SQLAlchemy's generic ``DBAPIError`` (and log nothing more) when the
service ran without its ``.env`` and connected as the OS user; ``migrate`` failed the same way.
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai import main
from ecommerce_ai.agents.triage_intake.model import TriageCause
from ecommerce_ai.agents.triage_intake.service import TriageIntakeService
from ecommerce_ai.infrastructure.db.dead_letter_repository import DeadLetterRepository
from ecommerce_ai.infrastructure.db.engine import build_engine
from ecommerce_ai.infrastructure.db.triage_repository import TriageRepository
from ecommerce_ai.infrastructure.errors import error_message, error_name
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.messaging.consumer_state import ConsumerState
from ecommerce_ai.messaging.contracts import PAYMENT_FAILED_TOPIC
from ecommerce_ai.messaging.triage_consumer import TriageConsumer
from tests.support.kafka import FakeConsumer, make_record, payment_failed_payload
from tests.support.polling import wait_until

REJECTED_LOGIN = "not-the-password-42"


def _wrong_login(database_url: str) -> Settings:
    url = make_url(database_url).set(password=REJECTED_LOGIN)
    return Settings(
        database_url=SecretStr(url.render_as_string(hide_password=False)),
        db_retry_max_attempts=1,
        kafka_reconnect_backoff_base_seconds=0.01,
        kafka_reconnect_backoff_max_seconds=0.01,
    )


async def test_should_name_the_driver_error_when_the_database_login_is_wrong(
    database_url: str,
) -> None:
    engine = build_engine(_wrong_login(database_url))
    try:
        with pytest.raises(DBAPIError) as raised:
            async with TriageRepository(engine).transaction() as work:
                await work.get_or_create_locked_case(
                    uuid4(), TriageCause.UNKNOWN, datetime.now(UTC)
                )
    finally:
        await engine.dispose()

    assert error_name(raised.value) == "InvalidPasswordError"
    assert "password authentication failed" in error_message(raised.value)
    assert REJECTED_LOGIN not in error_message(raised.value)


async def test_should_report_the_real_cause_in_readiness_state_when_saving_fails(
    database_url: str,
) -> None:
    settings = _wrong_login(database_url)
    engine = build_engine(settings)
    state = ConsumerState()
    fake = FakeConsumer([make_record(PAYMENT_FAILED_TOPIC, payment_failed_payload(uuid4()))])
    consumer = TriageConsumer(
        lambda: fake,
        TriageIntakeService(TriageRepository(engine)),
        DeadLetterRepository(engine),
        state,
        settings,
        poll_timeout_ms=5,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    try:
        await wait_until(lambda: state.last_error is not None, timeout=10)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        await engine.dispose()

    assert state.last_error == "InvalidPasswordError"
    assert fake.commits == []


def _asyncpg_url(database_url: str) -> str:
    """The URL as the developer's ``.env`` writes it: ``postgresql+asyncpg://``."""
    url = make_url(database_url).set(drivername="postgresql+asyncpg")
    return url.render_as_string(hide_password=False)


def test_should_succeed_and_change_nothing_when_migrate_runs_at_head(
    database_url: str, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", _asyncpg_url(database_url))

    assert main.cli(["migrate"]) == 0
    assert main.cli(["migrate"]) == 0

    assert asyncio.run(_alembic_versions(database_url)) == ["0006"]


async def _alembic_versions(database_url: str) -> list[str]:
    engine = build_engine(Settings(database_url=SecretStr(database_url)))
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT version_num FROM alembic_version"))
            return [str(row[0]) for row in rows]
    finally:
        await engine.dispose()
