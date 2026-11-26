"""Add the guardrail audit, per-user usage, conversation tool allowlists and finish reasons (US-07).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-25

Hand-written and immutable once released: the allowed enum values and the chat tools are copied
here on purpose. ``guardrail_events`` has no foreign key, so the audit outlives a deleted
conversation, and a trigger rejects every ``UPDATE`` and ``DELETE`` (``TRUNCATE`` is not a row
event, so the test fixtures can still empty it). It holds no message text, tool data or token
(SC-007). ``chat_user_usage`` keeps one row per user and UTC day. Existing conversations get the
chat tools of this release as their allowlist; a row inserted without one gets an empty
allowlist (no tool), so a missing value fails closed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STAGES = "'input', 'tool', 'output'"
_ACTIONS = "'blocked', 'replaced', 'sanitized', 'cooldown'"
_SEVERITIES = "'low', 'medium', 'high'"
_OLD_FINISH_REASONS = (
    "'completed', 'replaced', 'tool_limit', 'client_disconnected', 'awaiting_approval'"
)
_FINISH_REASONS = f"{_OLD_FINISH_REASONS}, 'blocked', 'tool_loop'"
_CHAT_TOOLS = (
    "ARRAY['get_order', 'list_my_orders', 'list_products', 'get_product',"
    " 'list_my_notifications', 'cancel_order']::text[]"
)
_EMPTY_ALLOWLIST = "'{}'::text[]"

_APPEND_ONLY_FUNCTION = """
CREATE FUNCTION guardrail_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'guardrail_events is append-only';
END;
$$
"""


def upgrade() -> None:
    """Create both tables and the trigger, add the allowlist, allow the new finish reasons."""
    op.create_table(
        "guardrail_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("stage", sa.String(8), nullable=False),
        sa.Column("rule", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guardrail_events")),
        sa.CheckConstraint(f"stage IN ({_STAGES})", name=op.f("ck_guardrail_events_stage")),
        sa.CheckConstraint(f"action IN ({_ACTIONS})", name=op.f("ck_guardrail_events_action")),
        sa.CheckConstraint(
            f"severity IN ({_SEVERITIES})", name=op.f("ck_guardrail_events_severity")
        ),
    )
    op.create_index(
        "ix_guardrail_events_user_id_created_at", "guardrail_events", ["user_id", "created_at"]
    )
    op.execute(_APPEND_ONLY_FUNCTION)
    op.execute(
        "CREATE TRIGGER guardrail_events_append_only BEFORE UPDATE OR DELETE ON guardrail_events"
        " FOR EACH ROW EXECUTE FUNCTION guardrail_events_append_only()"
    )
    op.create_table(
        "chat_user_usage",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("messages", sa.Integer(), nullable=False),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "day", name=op.f("pk_chat_user_usage")),
        sa.CheckConstraint("messages >= 0", name=op.f("ck_chat_user_usage_messages_not_negative")),
        sa.CheckConstraint("tokens >= 0", name=op.f("ck_chat_user_usage_tokens_not_negative")),
    )
    op.add_column(
        "chat_conversations",
        sa.Column(
            "tool_allowlist",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text(_CHAT_TOOLS),  # backfills the existing rows
        ),
    )
    # Fail closed: a conversation stored without an allowlist may call no tool.
    op.alter_column(
        "chat_conversations", "tool_allowlist", server_default=sa.text(_EMPTY_ALLOWLIST)
    )
    op.drop_constraint(op.f("ck_chat_messages_finish_reason"), "chat_messages", type_="check")
    op.create_check_constraint(
        op.f("ck_chat_messages_finish_reason"),
        "chat_messages",
        f"finish_reason IN ({_FINISH_REASONS})",
    )


def downgrade() -> None:
    """Drop both tables, the trigger and the allowlist (the guard audit is lost).

    Stored ``blocked`` and ``tool_loop`` messages become ``replaced`` so the 0004 check holds.
    """
    op.drop_constraint(op.f("ck_chat_messages_finish_reason"), "chat_messages", type_="check")
    op.execute(
        "UPDATE chat_messages SET finish_reason = 'replaced'"
        " WHERE finish_reason IN ('blocked', 'tool_loop')"
    )
    op.create_check_constraint(
        op.f("ck_chat_messages_finish_reason"),
        "chat_messages",
        f"finish_reason IN ({_OLD_FINISH_REASONS})",
    )
    op.drop_column("chat_conversations", "tool_allowlist")
    op.drop_table("chat_user_usage")
    op.execute("DROP TRIGGER guardrail_events_append_only ON guardrail_events")
    op.execute("DROP FUNCTION guardrail_events_append_only()")
    op.drop_index("ix_guardrail_events_user_id_created_at", table_name="guardrail_events")
    op.drop_table("guardrail_events")
