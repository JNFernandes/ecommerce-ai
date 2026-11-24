"""Apply the Alembic migrations shipped inside the package (``upgrade head``).

There is no ``alembic.ini``: the configuration is built here, and the migration environment
receives an already open connection, so the same code serves the ``migrate`` command, the
container entrypoint and the integration tests (research 12).
"""

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection

from ecommerce_ai.infrastructure.db.engine import build_engine
from ecommerce_ai.infrastructure.settings import Settings

SCRIPT_LOCATION = "ecommerce_ai.infrastructure.db:migrations"


def alembic_config(connection: Connection | None = None) -> Config:
    """Alembic configuration pointing at the packaged migrations, bound to ``connection``."""
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    config.attributes["connection"] = connection
    return config


def _upgrade_head(connection: Connection) -> None:
    command.upgrade(alembic_config(connection), "head")


async def run_migrations(settings: Settings) -> None:
    """Upgrade the database at ``DATABASE_URL`` to the latest revision, in one transaction."""
    engine = build_engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_upgrade_head)
    finally:
        await engine.dispose()
