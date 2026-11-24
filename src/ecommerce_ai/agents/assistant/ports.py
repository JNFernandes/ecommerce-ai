"""Ports of the chat assistant, implemented outside ``agents`` (constitution I).

- ``ChatModel``: one streamed model turn (``infrastructure/llm/anthropic_chat_model.py``); it
  raises the shared ``ModelUnavailable`` when the model cannot answer (research §6).
- ``ToolRunner``: the read-only tools, run in-process with the caller's token (``ToolInvoker``).
- ``ConversationStore``: conversations and messages, every call scoped to one user
  (``infrastructure/db/conversation_repository.py``).
- ``Checkpointer``: LangGraph's saver of the graph state per conversation (research §5).
"""

from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ecommerce_ai.agents.assistant.model import (
    Conversation,
    ConversationHistory,
    FinishReason,
    StoredMessage,
    TranscriptItem,
    Usage,
)
from ecommerce_ai.agents.ports import ModelUnavailable

__all__ = [
    "ChatModel",
    "ChatModelEvent",
    "ChatModelRequest",
    "Checkpointer",
    "ConversationPage",
    "ConversationStore",
    "ModelTurnEnd",
    "ModelUnavailable",
    "TextDelta",
    "ToolOutcome",
    "ToolRunner",
    "ToolSpec",
    "ToolUseRequest",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# Model ----------------------------------------------------------------------------------------


class ToolSpec(_Frozen):
    """A tool offered to the model: only registry tools ever are (FR-011)."""

    name: str = Field(min_length=1)
    description: str
    input_schema: dict[str, JsonValue] = Field(description="JSON schema of the input model.")


class ChatModelRequest(_Frozen):
    """One model turn: the system prompt, the conversation so far and the tools on offer."""

    system: str = Field(min_length=1)
    transcript: tuple[TranscriptItem, ...]
    tools: tuple[ToolSpec, ...]


class TextDelta(_Frozen):
    """A piece of answer text, not yet gatekept."""

    kind: Literal["text"] = "text"
    text: str


class ToolUseRequest(_Frozen):
    """The model asks for a tool call (name and input unchecked until the runner validates)."""

    kind: Literal["tool_use"] = "tool_use"
    id: str = Field(min_length=1)
    name: str
    input: dict[str, JsonValue]


class ModelTurnEnd(_Frozen):
    """Last event of a turn: why it stopped, what it cost, which model answered."""

    kind: Literal["turn_end"] = "turn_end"
    stop_reason: str
    usage: Usage
    model_id: str = Field(min_length=1)


type ChatModelEvent = TextDelta | ToolUseRequest | ModelTurnEnd


class ChatModel(Protocol):
    """The LLM behind the agent node."""

    def stream(self, request: ChatModelRequest) -> AsyncIterator[ChatModelEvent]:
        """Stream one turn, ending with ``ModelTurnEnd``; raise ``ModelUnavailable`` on failure.

        Closing the iterator early stops the model call (a gatekeeper violation stops reading).
        """
        ...


# Tools ----------------------------------------------------------------------------------------


class ToolOutcome(_Frozen):
    """The result of one tool call; never carries the caller's token."""

    ok: bool
    payload: dict[str, JsonValue] = Field(description="Envelope or `{error, message}`.")
    outcome: str = Field(description="`ok`, a tool error code or `unknown_tool`.")
    refused: bool = Field(description="True when the call never ran (unknown or invalid).")
    upstream_unauthenticated: bool = Field(description="The platform rejected the token.")
    duration_ms: float = Field(ge=0)


class ToolRunner(Protocol):
    """The read-only tools, run with the caller bound to the current context."""

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """Every tool the model may call."""
        ...

    async def run(self, name: str, arguments: Mapping[str, JsonValue]) -> ToolOutcome:
        """Run tool ``name``; never raises for a tool failure (it becomes the outcome)."""
        ...


# Store ----------------------------------------------------------------------------------------


class ConversationPage(_Frozen):
    """One page of a user's conversations, newest ``updated_at`` first."""

    items: tuple[Conversation, ...]
    total: int = Field(ge=0)


class ConversationStore(Protocol):
    """Conversations and messages; every call is scoped to ``user_id`` (FR-003).

    A conversation of another user behaves exactly like an unknown one (``None`` / ``False``).
    Adding a message bumps the conversation's ``updated_at``.
    """

    async def create(
        self, user_id: str, customer_id: UUID | None, title: str | None, now: datetime
    ) -> Conversation:
        """Create an empty conversation."""
        ...

    async def list(self, user_id: str, limit: int, offset: int) -> ConversationPage:
        """One page of the user's conversations and their total."""
        ...

    async def get(self, user_id: str, conversation_id: UUID) -> ConversationHistory | None:
        """The conversation with its messages oldest first, or ``None``."""
        ...

    async def delete(self, user_id: str, conversation_id: UUID) -> bool:
        """Delete the conversation, its messages and its graph state; ``False`` when unknown."""
        ...

    async def count_messages(self, user_id: str, conversation_id: UUID) -> int | None:
        """Number of stored messages, or ``None`` when the conversation is unknown."""
        ...

    async def add_user_message(
        self, user_id: str, conversation_id: UUID, message_id: UUID, content: str, now: datetime
    ) -> StoredMessage:
        """Store a user message."""
        ...

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
        """Store the assistant message of a run (the text actually sent)."""
        ...


type Checkpointer = BaseCheckpointSaver[str]
"""The graph-state saver: ``InMemorySaver`` in tests, ``PostgresCheckpointSaver`` in service."""
