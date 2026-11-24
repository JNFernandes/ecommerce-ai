"""SQLAlchemy Core tables of the triage intake (``data-model.md``).

The schema itself is created only by Alembic revisions (``migrations/versions``); these
definitions are what the repositories query, and must match the latest revision.
"""

from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

from ecommerce_ai.agents.triage_intake.model import TriageCause, TriageStatus

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _one_of(column: str, values: type[StrEnum]) -> str:
    """SQL condition limiting ``column`` to the values of an enum."""
    allowed = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({allowed})"


triage_cases = Table(
    "triage_cases",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("order_id", Uuid, nullable=False, unique=True),
    Column("cause", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(_one_of("cause", TriageCause), name="cause"),
    CheckConstraint(_one_of("status", TriageStatus), name="status"),
)

triage_evidence = Table(
    "triage_evidence",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "case_id",
        Uuid,
        ForeignKey("triage_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("source_topic", String(64), nullable=False),
    Column("event_id", Uuid, nullable=False, unique=True),
    Column("reason", Text, nullable=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSONB, nullable=False),
)

inbound_dead_letters = Table(
    "inbound_dead_letters",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("topic", String(255), nullable=False),
    Column("partition", Integer, nullable=False),
    Column("offset", BigInteger, nullable=False),
    Column("message_key", LargeBinary, nullable=True),
    Column("raw_payload", LargeBinary, nullable=False),
    Column("failure_reason", String(500), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
