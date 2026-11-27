"""Add ``llm_calls``: one usage record per model call (US-08).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-26

Hand-written and immutable once released: the allowed kinds and outcomes are copied here on
purpose. The table holds ids, counts, durations and outcomes only, never prompt or message text.
It has no foreign key, so records outlive deleted conversations and cases; a chat record names
its conversation and a triage record its case, never both.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = "'chat', 'triage'"
_OUTCOMES = "'ok', 'timeout', 'error', 'budget', 'fallback'"


def upgrade() -> None:
    """Create ``llm_calls`` and its time and model indexes."""
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
        sa.CheckConstraint(f"kind IN ({_KINDS})", name=op.f("ck_llm_calls_kind")),
        sa.CheckConstraint(f"outcome IN ({_OUTCOMES})", name=op.f("ck_llm_calls_outcome")),
        sa.CheckConstraint(
            "(conversation_id IS NULL) <> (case_id IS NULL)", name=op.f("ck_llm_calls_one_subject")
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0",
            name=op.f("ck_llm_calls_tokens_not_negative"),
        ),
        sa.CheckConstraint("latency_ms >= 0", name=op.f("ck_llm_calls_latency_not_negative")),
        sa.CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name=op.f("ck_llm_calls_cost_not_negative"),
        ),
    )
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])
    op.create_index("ix_llm_calls_model_id_created_at", "llm_calls", ["model_id", "created_at"])


def downgrade() -> None:
    """Drop ``llm_calls`` (the usage records are lost)."""
    op.drop_index("ix_llm_calls_model_id_created_at", table_name="llm_calls")
    op.drop_index("ix_llm_calls_created_at", table_name="llm_calls")
    op.drop_table("llm_calls")
