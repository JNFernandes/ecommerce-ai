"""Add triage results, the outbox, daily token usage and the COMPLETED case status (US-04).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24

Hand-written and immutable once released: the allowed enum values are copied here on purpose,
so a later change to the Python enums needs a new revision.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CAUSES = (
    "'PAYMENT_FAILED', 'INSUFFICIENT_STOCK', 'UNKNOWN_PRODUCT', 'CANCELLATION_REQUESTED', 'UNKNOWN'"
)
_OLD_STATUSES = "'PENDING_TRIAGE'"
_STATUSES = "'PENDING_TRIAGE', 'COMPLETED'"
_ACTIONS = "'CONTACT_CUSTOMER', 'RESTOCK', 'FIX_CATALOG', 'REVIEW', 'NONE'"
_SOURCES = "'LLM', 'FALLBACK_TEMPLATE'"
_OUTBOX_STATUSES = "'PENDING', 'RESOLVED', 'EXHAUSTED'"


def _replace_case_status_check(statuses: str) -> None:
    op.drop_constraint(op.f("ck_triage_cases_status"), "triage_cases", type_="check")
    op.create_check_constraint(
        op.f("ck_triage_cases_status"), "triage_cases", f"status IN ({statuses})"
    )


def upgrade() -> None:
    """Allow COMPLETED cases, index the pick query and create the three new tables."""
    _replace_case_status_check(_STATUSES)
    op.create_index("ix_triage_cases_status_updated_at", "triage_cases", ["status", "updated_at"])
    op.create_table(
        "triage_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("cause", sa.String(32), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("suggested_action", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("revisions", sa.SmallInteger(), nullable=False),
        sa.Column("violations", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_triage_results")),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["triage_cases.id"],
            name=op.f("fk_triage_results_case_id_triage_cases"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("case_id", name=op.f("uq_triage_results_case_id")),
        sa.UniqueConstraint("event_id", name=op.f("uq_triage_results_event_id")),
        sa.CheckConstraint(f"cause IN ({_CAUSES})", name=op.f("ck_triage_results_cause")),
        sa.CheckConstraint(
            "char_length(explanation) BETWEEN 1 AND 500",
            name=op.f("ck_triage_results_explanation_length"),
        ),
        sa.CheckConstraint(
            f"suggested_action IN ({_ACTIONS})", name=op.f("ck_triage_results_suggested_action")
        ),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 1", name=op.f("ck_triage_results_confidence_range")
        ),
        sa.CheckConstraint(f"source IN ({_SOURCES})", name=op.f("ck_triage_results_source")),
        sa.CheckConstraint(
            "(model_id IS NULL) = (source = 'FALLBACK_TEMPLATE')",
            name=op.f("ck_triage_results_model_id_iff_llm"),
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0",
            name=op.f("ck_triage_results_tokens_not_negative"),
        ),
        sa.CheckConstraint("latency_ms >= 0", name=op.f("ck_triage_results_latency_not_negative")),
        sa.CheckConstraint(
            "revisions BETWEEN 0 AND 2", name=op.f("ck_triage_results_revisions_range")
        ),
    )
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.String(255), nullable=False),
        sa.Column("message_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
        sa.UniqueConstraint("event_id", name=op.f("uq_outbox_events_event_id")),
        sa.CheckConstraint(f"status IN ({_OUTBOX_STATUSES})", name=op.f("ck_outbox_events_status")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_outbox_events_attempts_not_negative")),
    )
    op.create_index(
        "ix_outbox_events_status_next_attempt_at", "outbox_events", ["status", "next_attempt_at"]
    )
    op.create_table(
        "llm_token_usage",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("day", name=op.f("pk_llm_token_usage")),
        sa.CheckConstraint("tokens >= 0", name=op.f("ck_llm_token_usage_tokens_not_negative")),
    )


def downgrade() -> None:
    """Drop the three tables and the index; fails while COMPLETED cases exist."""
    op.drop_table("llm_token_usage")
    op.drop_index("ix_outbox_events_status_next_attempt_at", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_table("triage_results")
    op.drop_index("ix_triage_cases_status_updated_at", table_name="triage_cases")
    _replace_case_status_check(_OLD_STATUSES)
