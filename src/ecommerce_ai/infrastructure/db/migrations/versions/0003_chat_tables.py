"""Add chat conversations, messages and the LangGraph checkpoint tables (US-05).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-24

Hand-written and immutable once released: the allowed enum values are copied here on purpose,
so a later change to the Python enums needs a new revision. The checkpoint tables hold the graph
state of each conversation (``thread_id`` = conversation id as text); the conversation repository
deletes their rows together with the conversation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLES = "'user', 'assistant'"
_FINISH_REASONS = "'completed', 'replaced', 'tool_limit', 'client_disconnected'"


def upgrade() -> None:
    """Create the four chat tables and the two listing indexes."""
    op.create_table(
        "chat_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_conversations")),
        sa.CheckConstraint(
            "title IS NULL OR char_length(title) >= 1",
            name=op.f("ck_chat_conversations_title_not_empty"),
        ),
    )
    op.create_index(
        "ix_chat_conversations_user_id_updated_at",
        "chat_conversations",
        ["user_id", "updated_at", "id"],
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("finish_reason", sa.String(32), nullable=True),
        sa.Column("prompt_version", sa.String(32), nullable=True),
        sa.Column("model_id", sa.String(128), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["chat_conversations.id"],
            name=op.f("fk_chat_messages_conversation_id_chat_conversations"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(f"role IN ({_ROLES})", name=op.f("ck_chat_messages_role")),
        sa.CheckConstraint(
            f"finish_reason IN ({_FINISH_REASONS})", name=op.f("ck_chat_messages_finish_reason")
        ),
        sa.CheckConstraint(
            "role = 'assistant' OR char_length(content) BETWEEN 1 AND 2000",
            name=op.f("ck_chat_messages_user_content_length"),
        ),
        sa.CheckConstraint(
            "(role = 'user' AND finish_reason IS NULL AND prompt_version IS NULL"
            " AND model_id IS NULL AND input_tokens IS NULL AND output_tokens IS NULL)"
            " OR (role = 'assistant' AND finish_reason IS NOT NULL"
            " AND prompt_version IS NOT NULL AND input_tokens IS NOT NULL"
            " AND output_tokens IS NOT NULL)",
            name=op.f("ck_chat_messages_assistant_fields"),
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0",
            name=op.f("ck_chat_messages_tokens_not_negative"),
        ),
    )
    op.create_index(
        "ix_chat_messages_conversation_id_created_at",
        "chat_messages",
        ["conversation_id", "created_at", "id"],
    )
    op.create_table(
        "chat_checkpoints",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("parent_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("checkpoint", sa.LargeBinary(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "thread_id", "checkpoint_ns", "checkpoint_id", name=op.f("pk_chat_checkpoints")
        ),
    )
    op.create_table(
        "chat_checkpoint_writes",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("value", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "task_id",
            "idx",
            name=op.f("pk_chat_checkpoint_writes"),
        ),
    )


def downgrade() -> None:
    """Drop the chat tables and their indexes (all chat data is lost)."""
    op.drop_table("chat_checkpoint_writes")
    op.drop_table("chat_checkpoints")
    op.drop_index("ix_chat_messages_conversation_id_created_at", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_conversations_user_id_updated_at", table_name="chat_conversations")
    op.drop_table("chat_conversations")
