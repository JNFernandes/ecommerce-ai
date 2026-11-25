"""Add chat approvals, the append-only ``chat_actions`` audit and ``awaiting_approval`` (US-06).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25

Hand-written and immutable once released: the allowed enum values are copied here on purpose.
``chat_approvals`` dies with its conversation; ``chat_actions`` has no foreign key and a trigger
rejects every ``DELETE`` and any ``UPDATE`` of a row whose outcome is set, or that changes an
identity column or an already set decision (data-model.md). ``TRUNCATE`` is not a row event, so
the test fixtures can still empty the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = "'pending', 'approved', 'rejected', 'expired', 'withdrawn'"
_DECISIONS = "'approve', 'reject'"
_OUTCOMES = "'cancelled', 'rejected', 'not_cancellable', 'failed', 'expired', 'withdrawn'"
_OLD_FINISH_REASONS = "'completed', 'replaced', 'tool_limit', 'client_disconnected'"
_FINISH_REASONS = f"{_OLD_FINISH_REASONS}, 'awaiting_approval'"

_APPEND_ONLY_FUNCTION = """
CREATE FUNCTION chat_actions_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'chat_actions is append-only';
    END IF;
    IF OLD.outcome IS NOT NULL THEN
        RAISE EXCEPTION 'chat_actions row % is final', OLD.id;
    END IF;
    IF (NEW.id, NEW.approval_id, NEW.conversation_id, NEW.user_id, NEW.action, NEW.order_id,
        NEW.request_id, NEW.proposed_at)
       IS DISTINCT FROM
       (OLD.id, OLD.approval_id, OLD.conversation_id, OLD.user_id, OLD.action, OLD.order_id,
        OLD.request_id, OLD.proposed_at) THEN
        RAISE EXCEPTION 'chat_actions identity columns are immutable';
    END IF;
    IF OLD.decision IS NOT NULL AND NEW.decision IS DISTINCT FROM OLD.decision THEN
        RAISE EXCEPTION 'chat_actions decision is set once';
    END IF;
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    """Create both approval tables, the audit trigger, and allow ``awaiting_approval``."""
    op.create_table(
        "chat_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_approvals")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["chat_conversations.id"],
            name=op.f("fk_chat_approvals_conversation_id_chat_conversations"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name=op.f("ck_chat_approvals_status")),
        sa.CheckConstraint(
            "expires_at > proposed_at", name=op.f("ck_chat_approvals_expires_after_proposal")
        ),
    )
    op.create_index(
        "ux_chat_approvals_conversation_id_pending",
        "chat_approvals",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "chat_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_actions")),
        sa.UniqueConstraint("approval_id", name=op.f("uq_chat_actions_approval_id")),
        sa.CheckConstraint(f"decision IN ({_DECISIONS})", name=op.f("ck_chat_actions_decision")),
        sa.CheckConstraint(f"outcome IN ({_OUTCOMES})", name=op.f("ck_chat_actions_outcome")),
    )
    op.create_index(
        "ix_chat_actions_user_id_proposed_at", "chat_actions", ["user_id", "proposed_at"]
    )
    op.execute(_APPEND_ONLY_FUNCTION)
    op.execute(
        "CREATE TRIGGER chat_actions_append_only BEFORE UPDATE OR DELETE ON chat_actions"
        " FOR EACH ROW EXECUTE FUNCTION chat_actions_append_only()"
    )
    op.drop_constraint(op.f("ck_chat_messages_finish_reason"), "chat_messages", type_="check")
    op.create_check_constraint(
        op.f("ck_chat_messages_finish_reason"),
        "chat_messages",
        f"finish_reason IN ({_FINISH_REASONS})",
    )


def downgrade() -> None:
    """Drop the approval tables and the trigger (the audit trail is lost).

    Stored ``awaiting_approval`` messages become ``completed`` so the 0003 check holds again.
    """
    op.drop_constraint(op.f("ck_chat_messages_finish_reason"), "chat_messages", type_="check")
    op.execute(
        "UPDATE chat_messages SET finish_reason = 'completed'"
        " WHERE finish_reason = 'awaiting_approval'"
    )
    op.create_check_constraint(
        op.f("ck_chat_messages_finish_reason"),
        "chat_messages",
        f"finish_reason IN ({_OLD_FINISH_REASONS})",
    )
    op.execute("DROP TRIGGER chat_actions_append_only ON chat_actions")
    op.execute("DROP FUNCTION chat_actions_append_only()")
    op.drop_index("ix_chat_actions_user_id_proposed_at", table_name="chat_actions")
    op.drop_table("chat_actions")
    op.drop_index("ux_chat_approvals_conversation_id_pending", table_name="chat_approvals")
    op.drop_table("chat_approvals")
