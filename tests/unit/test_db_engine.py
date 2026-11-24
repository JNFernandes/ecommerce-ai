"""The database engine is built lazily and classifies failures for retries."""

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from ecommerce_ai.infrastructure.db.engine import (
    async_database_url,
    build_engine,
    build_session_factory,
    is_transient_db_error,
)
from ecommerce_ai.infrastructure.settings import Settings


def _settings(url: str) -> Settings:
    return Settings(database_url=SecretStr(url))


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://ai:s3cret@db.internal:5436/ecommerce_ai",
        "postgres://ai:s3cret@db.internal:5436/ecommerce_ai",
    ],
)
def test_should_switch_to_the_asyncpg_driver_when_reading_database_url(url: str) -> None:
    result = async_database_url(_settings(url))

    assert result.drivername == "postgresql+asyncpg"
    assert (result.username, result.password) == ("ai", "s3cret")
    assert (result.host, result.port, result.database) == ("db.internal", 5436, "ecommerce_ai")


async def test_should_not_connect_when_the_engine_is_built() -> None:
    # Nothing listens on this port: building the engine and its session factory must not care.
    engine = build_engine(_settings("postgresql://ai:pw@127.0.0.1:1/ecommerce_ai"))
    try:
        factory = build_session_factory(engine)

        assert engine.url.drivername == "postgresql+asyncpg"
        assert engine.pool.status().count("Current Checked out connections: 0") == 1
        assert factory.kw["expire_on_commit"] is False
    finally:
        await engine.dispose()


def _dbapi_error(cls: type[DBAPIError], *, invalidated: bool = False) -> DBAPIError:
    return cls("SELECT 1", None, Exception("boom"), connection_invalidated=invalidated)


@pytest.mark.parametrize(
    "exc",
    [
        _dbapi_error(OperationalError),
        _dbapi_error(InterfaceError),
        _dbapi_error(DBAPIError, invalidated=True),
        PoolTimeoutError("pool exhausted"),
        ConnectionRefusedError("refused"),
        OSError("network unreachable"),
        TimeoutError(),
    ],
    ids=[
        "operational",
        "interface",
        "connection-invalidated",
        "pool-timeout",
        "connection-refused",
        "os-error",
        "timeout",
    ],
)
def test_should_retry_when_the_failure_is_transient(exc: BaseException) -> None:
    assert is_transient_db_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [_dbapi_error(IntegrityError), _dbapi_error(DBAPIError), ValueError("bad"), KeyError("x")],
    ids=["integrity", "plain-dbapi", "value-error", "key-error"],
)
def test_should_not_retry_when_the_failure_is_permanent(exc: BaseException) -> None:
    assert is_transient_db_error(exc) is False
