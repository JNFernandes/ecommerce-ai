"""PostgreSQL implementation of the ``ConversationStore`` port (FR-003, FR-004, data-model.md).

Every query filters on ``user_id``, so another user's conversation behaves exactly like an
unknown one. Adding a message bumps the conversation's ``updated_at`` in the same transaction;
deleting a conversation also deletes its LangGraph checkpoint rows (thread id = conversation id).
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ecommerce_ai.agents.assistant.model import (
    Conversation,
    ConversationHistory,
    FinishReason,
    MessageRole,
    StoredMessage,
    Usage,
)
from ecommerce_ai.agents.assistant.ports import ConversationPage
from ecommerce_ai.infrastructure.db.tables import (
    chat_checkpoint_writes,
    chat_checkpoints,
    chat_conversations,
    chat_messages,
)

_conversations = chat_conversations.c
_messages = chat_messages.c
_CONVERSATION_COLUMNS = (
    _conversations.id,
    _conversations.title,
    _conversations.created_at,
    _conversations.updated_at,
    _conversations.tool_allowlist,
)


def _conversation(row: RowMapping) -> Conversation:
    return Conversation.model_validate(dict(row) | {"tool_allowlist": tuple(row["tool_allowlist"])})


def _message(row: RowMapping) -> StoredMessage:
    usage = None
    if row["role"] == MessageRole.ASSISTANT:
        usage = Usage.model_validate(
            {"input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"]}
        )
    return StoredMessage.model_validate(
        {
            key: row[key]
            for key in (
                "id",
                "role",
                "content",
                "created_at",
                "finish_reason",
                "prompt_version",
                "model_id",
            )
        }
        | {"usage": usage}
    )


class ConversationRepository:
    """``ConversationStore`` over the async engine; one transaction per call."""

    def __init__(self, engine: AsyncEngine, tool_allowlist: Sequence[str] = ()) -> None:
        """Read and write on ``engine``; new conversations get ``tool_allowlist`` (FR-010).

        The default allows no tool at all, so a missing allowlist fails closed.
        """
        self._engine = engine
        self._tool_allowlist = list(dict.fromkeys(tool_allowlist))

    async def create(
        self, user_id: str, customer_id: UUID | None, title: str | None, now: datetime
    ) -> Conversation:
        """Create an empty conversation."""
        stamp = now.astimezone(UTC)
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    insert(chat_conversations)
                    .values(
                        id=uuid4(),
                        user_id=user_id,
                        customer_id=customer_id,
                        title=title,
                        created_at=stamp,
                        updated_at=stamp,
                        tool_allowlist=self._tool_allowlist,
                    )
                    .returning(*_CONVERSATION_COLUMNS)
                )
            ).one()
        return _conversation(row._mapping)

    async def list(self, user_id: str, limit: int, offset: int) -> ConversationPage:
        """One page of the user's conversations, newest ``updated_at`` first, and their total."""
        async with self._engine.connect() as connection:
            total = (
                await connection.execute(
                    select(func.count())
                    .select_from(chat_conversations)
                    .where(_conversations.user_id == user_id)
                )
            ).scalar_one()
            rows = await connection.execute(
                select(*_CONVERSATION_COLUMNS)
                .where(_conversations.user_id == user_id)
                .order_by(_conversations.updated_at.desc(), _conversations.id.desc())
                .limit(limit)
                .offset(offset)
            )
            items = tuple(_conversation(row._mapping) for row in rows)
        return ConversationPage(items=items, total=total)

    async def get(self, user_id: str, conversation_id: UUID) -> ConversationHistory | None:
        """The conversation with its messages oldest first, or ``None``."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(*_CONVERSATION_COLUMNS).where(
                        _conversations.id == conversation_id, _conversations.user_id == user_id
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            messages = await connection.execute(
                select(chat_messages)
                .where(_messages.conversation_id == conversation_id)
                .order_by(_messages.created_at, _messages.id)
            )
            history = tuple(_message(message._mapping) for message in messages)
        return ConversationHistory(conversation=_conversation(row._mapping), messages=history)

    async def delete(self, user_id: str, conversation_id: UUID) -> bool:
        """Delete the conversation, its messages (cascade) and its checkpoint rows."""
        async with self._engine.begin() as connection:
            deleted = await connection.execute(
                delete(chat_conversations)
                .where(_conversations.id == conversation_id, _conversations.user_id == user_id)
                .returning(_conversations.id)
            )
            if deleted.one_or_none() is None:
                return False
            thread_id = str(conversation_id)
            for table in (chat_checkpoint_writes, chat_checkpoints):
                await connection.execute(delete(table).where(table.c.thread_id == thread_id))
        return True

    async def find(self, user_id: str, conversation_id: UUID) -> Conversation | None:
        """The conversation without its messages, or ``None``."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(*_CONVERSATION_COLUMNS).where(
                        _conversations.id == conversation_id, _conversations.user_id == user_id
                    )
                )
            ).one_or_none()
        return None if row is None else _conversation(row._mapping)

    async def count_messages(self, user_id: str, conversation_id: UUID) -> int | None:
        """Number of stored messages, or ``None`` when the conversation is unknown."""
        async with self._engine.connect() as connection:
            owned = (
                await connection.execute(
                    select(_conversations.id).where(
                        _conversations.id == conversation_id, _conversations.user_id == user_id
                    )
                )
            ).one_or_none()
            if owned is None:
                return None
            count: int = (
                await connection.execute(
                    select(func.count())
                    .select_from(chat_messages)
                    .where(_messages.conversation_id == conversation_id)
                )
            ).scalar_one()
        return count

    async def add_user_message(
        self, user_id: str, conversation_id: UUID, message_id: UUID, content: str, now: datetime
    ) -> StoredMessage:
        """Store a user message; ``LookupError`` when the conversation is unknown."""
        values: dict[str, object] = {"role": MessageRole.USER.value, "content": content}
        return await self._add(user_id, conversation_id, message_id, values, now)

    async def add_assistant_message(
        self,
        user_id: str,
        conversation_id: UUID,
        message_id: UUID,
        content: str,
        finish_reason: FinishReason,
        prompt_version: str,
        model_id: str | None,
        usage: Usage,
        now: datetime,
    ) -> StoredMessage:
        """Store the assistant message of a run (``LookupError`` for an unknown conversation)."""
        values = assistant_values(content, finish_reason, prompt_version, model_id, usage)
        return await self._add(user_id, conversation_id, message_id, values, now)

    async def _add(
        self,
        user_id: str,
        conversation_id: UUID,
        message_id: UUID,
        values: dict[str, object],
        now: datetime,
    ) -> StoredMessage:
        stamp = now.astimezone(UTC)
        async with self._engine.begin() as connection:
            return await insert_message(
                connection, user_id, conversation_id, message_id, values, stamp
            )


def assistant_values(
    content: str,
    finish_reason: FinishReason,
    prompt_version: str,
    model_id: str | None,
    usage: Usage,
) -> dict[str, object]:
    """Column values of an assistant message."""
    return {
        "role": MessageRole.ASSISTANT.value,
        "content": content,
        "finish_reason": finish_reason.value,
        "prompt_version": prompt_version,
        "model_id": model_id,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }


async def insert_message(
    connection: AsyncConnection,
    user_id: str,
    conversation_id: UUID,
    message_id: UUID,
    values: dict[str, object],
    now: datetime,
) -> StoredMessage:
    """Insert a message in the caller's transaction and bump the conversation's ``updated_at``.

    ``LookupError`` when the user has no such conversation.
    """
    await _touch(connection, user_id, conversation_id, now)
    row = (
        await connection.execute(
            insert(chat_messages)
            .values(id=message_id, conversation_id=conversation_id, created_at=now, **values)
            .returning(chat_messages)
        )
    ).one()
    return _message(row._mapping)


async def _touch(
    connection: AsyncConnection, user_id: str, conversation_id: UUID, now: datetime
) -> None:
    """Bump ``updated_at`` of the user's conversation; ``LookupError`` when it is unknown."""
    touched = await connection.execute(
        update(chat_conversations)
        .where(_conversations.id == conversation_id, _conversations.user_id == user_id)
        .values(updated_at=now)
        .returning(_conversations.id)
    )
    if touched.one_or_none() is None:
        raise LookupError("unknown conversation")
