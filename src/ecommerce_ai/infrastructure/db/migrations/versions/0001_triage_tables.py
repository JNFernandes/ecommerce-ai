"""Create the triage case, evidence and inbound dead-letter tables (US-03).

Revision ID: 0001
Revises:
Create Date: 2026-11-24

Hand-written and immutable once released: the allowed enum values are copied here on purpose,
so a later change to the Python enums needs a new revision.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CAUSES = (
    "'PAYMENT_FAILED', 'INSUFFICIENT_STOCK', 'UNKNOWN_PRODUCT', 'CANCELLATION_REQUESTED', 'UNKNOWN'"
)
_STATUSES = "'PENDING_TRIAGE'"


def upgrade() -> None:
    """Create the three tables with their keys, uniqueness and CHECK constraints."""
    op.create_table(
        "triage_cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("cause", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_triage_cases"),
        sa.UniqueConstraint("order_id", name="uq_triage_cases_order_id"),
        sa.CheckConstraint(f"cause IN ({_CAUSES})", name="ck_triage_cases_cause"),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="ck_triage_cases_status"),
    )
    op.create_table(
        "triage_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("source_topic", sa.String(64), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_triage_evidence"),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["triage_cases.id"],
            name="fk_triage_evidence_case_id_triage_cases",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("event_id", name="uq_triage_evidence_event_id"),
    )
    op.create_index("ix_triage_evidence_case_id", "triage_evidence", ["case_id"])
    op.create_table(
        "inbound_dead_letters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.String(255), nullable=False),
        sa.Column("partition", sa.Integer(), nullable=False),
        sa.Column("offset", sa.BigInteger(), nullable=False),
        sa.Column("message_key", sa.LargeBinary(), nullable=True),
        sa.Column("raw_payload", sa.LargeBinary(), nullable=False),
        sa.Column("failure_reason", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_inbound_dead_letters"),
    )


def downgrade() -> None:
    """Drop the three tables."""
    op.drop_table("inbound_dead_letters")
    op.drop_index("ix_triage_evidence_case_id", table_name="triage_evidence")
    op.drop_table("triage_evidence")
    op.drop_table("triage_cases")
