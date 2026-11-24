"""SQLAlchemy Core tables of the triage intake, results, outbox and token budget.

The schema itself is created only by Alembic revisions (``migrations/versions``); these
definitions are what the repositories query, and must match the latest revision.
"""

from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

from ecommerce_ai.agents.triage.model import (
    EXPLANATION_MAX_CHARS,
    MAX_REVISIONS,
    ResultSource,
    SuggestedAction,
)
from ecommerce_ai.agents.triage_intake.model import TriageCause, TriageStatus
from ecommerce_ai.infrastructure.outbox import OutboxStatus

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
    Index("ix_triage_cases_status_updated_at", "status", "updated_at"),
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

triage_results = Table(
    "triage_results",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "case_id",
        Uuid,
        ForeignKey("triage_cases.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column("event_id", Uuid, nullable=False, unique=True),
    Column("cause", String(32), nullable=False),
    Column("explanation", Text, nullable=False),
    Column("suggested_action", String(32), nullable=False),
    Column("confidence", Numeric(4, 3), nullable=False),
    Column("source", String(32), nullable=False),
    Column("prompt_version", String(32), nullable=False),
    Column("model_id", String(128), nullable=True),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("latency_ms", Integer, nullable=False),
    Column("revisions", SmallInteger, nullable=False),
    Column("violations", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(_one_of("cause", TriageCause), name="cause"),
    CheckConstraint(
        f"char_length(explanation) BETWEEN 1 AND {EXPLANATION_MAX_CHARS}",
        name="explanation_length",
    ),
    CheckConstraint(_one_of("suggested_action", SuggestedAction), name="suggested_action"),
    CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_range"),
    CheckConstraint(_one_of("source", ResultSource), name="source"),
    CheckConstraint(
        f"(model_id IS NULL) = (source = '{ResultSource.FALLBACK_TEMPLATE.value}')",
        name="model_id_iff_llm",
    ),
    CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="tokens_not_negative"),
    CheckConstraint("latency_ms >= 0", name="latency_not_negative"),
    CheckConstraint(f"revisions BETWEEN 0 AND {MAX_REVISIONS}", name="revisions_range"),
)

outbox_events = Table(
    "outbox_events",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("event_id", Uuid, nullable=False, unique=True),
    Column("topic", String(255), nullable=False),
    Column("message_key", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False),
    Column("last_error", String(200), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(_one_of("status", OutboxStatus), name="status"),
    CheckConstraint("attempts >= 0", name="attempts_not_negative"),
    Index("ix_outbox_events_status_next_attempt_at", "status", "next_attempt_at"),
)

llm_token_usage = Table(
    "llm_token_usage",
    metadata,
    Column("day", Date, primary_key=True),
    Column("tokens", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("tokens >= 0", name="tokens_not_negative"),
)
