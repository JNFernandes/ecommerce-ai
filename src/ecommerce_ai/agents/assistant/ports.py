"""Ports of the chat assistant, implemented outside ``agents`` (constitution I).

- ``ChatModel``: one streamed model turn (``infrastructure/llm/anthropic_chat_model.py``); it
  raises the shared ``ModelUnavailable`` when the model cannot answer (research §6).
- ``ToolRunner``: the read-only tools, run in-process with the caller's token (``ToolInvoker``).
- ``ConversationStore``: conversations and messages, every call scoped to one user
  (``infrastructure/db/conversation_repository.py``).
- ``Checkpointer``: LangGraph's saver of the graph state per conversation (research §5).
- ``ApprovalStore``: proposed mutating calls and their audit trail (US-06,
  ``infrastructure/db/approval_repository.py``).
"""

from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, Self
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from ecommerce_ai.agents.assistant.model import (
    ApprovalOutcome,
    ApprovalRequired,
    ApprovalSummary,
    Conversation,
    ConversationHistory,
    Decision,
    FinishReason,
    StoredMessage,
    TranscriptItem,
    Usage,
)
from ecommerce_ai.agents.ports import ModelUnavailable

__all__ = [
    "Approval",
    "ApprovalOutcome",
    "ApprovalStatus",
    "ApprovalStore",
    "ChatModel",
    "ChatModelEvent",
    "ChatModelRequest",
    "Checkpointer",
    "ConversationPage",
    "ConversationStore",
    "Decision",
    "ModelTurnEnd",
    "ModelUnavailable",
    "ProposalMessage",
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
    content: tuple[dict[str, JsonValue], ...] = Field(
        default=(),
        description=(
            "The turn's content blocks in order (`thinking`, `text`, `tool_use`), so it can be"
            " replayed unchanged; empty when the model does not report them."
        ),
    )


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
    """The chat's tools, run with the caller bound to the current context."""

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """Every tool the model may call (a mutating one is offered, then intercepted)."""
        ...

    async def run(self, name: str, arguments: Mapping[str, JsonValue]) -> ToolOutcome:
        """Run read-only tool ``name``; never raises for a tool failure (it becomes the outcome).

        A mutating tool is refused (``unknown_tool``).
        """
        ...

    async def run_approved(self, name: str, arguments: Mapping[str, JsonValue]) -> ToolOutcome:
        """Run mutating tool ``name`` once, with the approved arguments; never retried (US-06)."""
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


# Approvals (US-06) ----------------------------------------------------------------------------


class ApprovalStatus(StrEnum):
    """Life of an approval: it leaves ``pending`` once and only once (data-model.md)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"


class Approval(_Frozen):
    """One proposed mutating call, bound to its conversation, user, action and exact arguments."""

    id: UUID
    conversation_id: UUID
    user_id: str = Field(min_length=1, description="Token subject of the proposing caller.")
    action: str = Field(min_length=1, description="The mutating tool, e.g. `cancel_order`.")
    order_id: UUID = Field(description="Target of the action (audit `order_id`).")
    arguments: dict[str, JsonValue] = Field(description="Exact arguments; never changed.")
    summary: ApprovalSummary
    status: ApprovalStatus = ApprovalStatus.PENDING
    proposed_at: AwareDatetime
    expires_at: AwareDatetime
    decided_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _expires_after_proposal(self) -> Self:
        if self.expires_at <= self.proposed_at:
            raise ValueError("expires_at must be after proposed_at")
        return self

    def is_expired(self, now: datetime) -> bool:
        """True from ``expires_at`` on: the approval can never be executed any more."""
        return now >= self.expires_at

    def required_event(self) -> ApprovalRequired:
        """The ``approval.required`` event (and ``pendingApproval`` object) of this approval."""
        return ApprovalRequired(
            approval_id=self.id,
            action=self.action,
            order_id=self.order_id,
            summary=self.summary,
            expires_at=self.expires_at,
        )


class ProposalMessage(_Frozen):
    """The assistant message stored with a proposal (finish reason ``awaiting_approval``)."""

    message_id: UUID
    content: str = Field(description="Text actually sent before the pause.")
    prompt_version: str = Field(min_length=1)
    model_id: str | None
    usage: Usage


class ApprovalStore(Protocol):
    """Approvals and their append-only audit rows (``chat_actions``); every read filters user.

    An approval of another user, or of another conversation, behaves like an unknown one.
    Transitions happen only from ``pending``, by a conditional update, so concurrent decisions
    yield exactly one winner (FR-007).
    """

    async def create_proposal(
        self, approval: Approval, message: ProposalMessage, request_id: str | None
    ) -> None:
        """Store the approval, its audit row and the assistant message in one transaction."""
        ...

    async def get(self, user_id: str, conversation_id: UUID, approval_id: UUID) -> Approval | None:
        """The approval in any status, or ``None``."""
        ...

    async def pending_for(self, user_id: str, conversation_id: UUID) -> Approval | None:
        """The conversation's pending approval (expired or not), or ``None``."""
        ...

    async def awaiting_outcome(self, user_id: str, conversation_id: UUID) -> Approval | None:
        """The conversation's decided approval whose audit outcome is still unset, or ``None``."""
        ...

    async def decide(
        self,
        user_id: str,
        conversation_id: UUID,
        approval_id: UUID,
        decision: Decision,
        now: datetime,
    ) -> Approval | None:
        """Atomically move a pending, unexpired approval to approved/rejected and audit it.

        Returns the decided approval, or ``None`` when nothing changed (unknown, not pending or
        expired); the caller then reloads it to choose the answer.
        """
        ...

    async def close(
        self, approval_id: UUID, status: ApprovalStatus, outcome: ApprovalOutcome, now: datetime
    ) -> bool:
        """Move a pending approval to ``expired``/``withdrawn`` with that audit outcome.

        ``False`` when it was no longer pending.
        """
        ...

    async def record_outcome(self, approval_id: UUID, outcome: ApprovalOutcome) -> None:
        """Set the audit outcome of a decided approval once; a second call changes nothing."""
        ...

    async def proposals_since(self, user_id: str, since: datetime) -> int:
        """Proposals (audit rows) of ``user_id`` at or after ``since``, for the hourly limit."""
        ...


type Checkpointer = BaseCheckpointSaver[str]
"""The graph-state saver: ``InMemorySaver`` in tests, ``PostgresCheckpointSaver`` in service."""
