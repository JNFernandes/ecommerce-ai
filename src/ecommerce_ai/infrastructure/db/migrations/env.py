"""Alembic environment: run the revisions on the connection ``migrate.py`` passes in.

Offline (SQL script) mode is not supported; run ``python -m ecommerce_ai migrate`` instead.
"""

from alembic import context
from sqlalchemy import Connection

from ecommerce_ai.infrastructure.db.tables import metadata


def _run_online(connection: Connection) -> None:
    """Run the pending revisions inside the caller's transaction."""
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


_connection = context.config.attributes.get("connection")
if context.is_offline_mode() or not isinstance(_connection, Connection):
    raise RuntimeError("Run migrations with `python -m ecommerce_ai migrate`.")
_run_online(_connection)
