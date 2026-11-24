"""Async SQLAlchemy engine on asyncpg, built lazily from ``DATABASE_URL``.

``DATABASE_URL`` stays a plain ``postgresql://`` URL in the environment; the driver is swapped to
``postgresql+asyncpg`` here. Building the engine opens no connection. ``is_transient_db_error``
tells the consumer which failures deserve a bounded retry (research 4).
"""

from sqlalchemy import URL, make_url
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ecommerce_ai.infrastructure.settings import Settings

ASYNC_DRIVER = "postgresql+asyncpg"


def async_database_url(settings: Settings) -> URL:
    """Return ``DATABASE_URL`` with the asyncpg driver, credentials kept as given."""
    return make_url(settings.database_url.get_secret_value()).set(drivername=ASYNC_DRIVER)


def build_engine(settings: Settings) -> AsyncEngine:
    """Build the engine without connecting; connections are checked before reuse."""
    timeout = settings.database_timeout_seconds
    return create_async_engine(
        async_database_url(settings),
        pool_pre_ping=True,
        connect_args={"timeout": timeout, "command_timeout": timeout},
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a factory of sessions whose objects stay usable after commit."""
    return async_sessionmaker(engine, expire_on_commit=False)


def is_transient_db_error(exc: BaseException) -> bool:
    """Whether ``exc`` is a connection-level failure that a later attempt may not hit."""
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    return isinstance(
        exc, OperationalError | InterfaceError | PoolTimeoutError | OSError | TimeoutError
    )
