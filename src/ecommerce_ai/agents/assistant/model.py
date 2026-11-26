"""Domain types of the chat assistant (data-model.md, contracts/chat-api.md).

- ``FinishReason`` / ``ChatErrorCode`` (+ the fixed customer-safe message of each code, FR-009).
- Approvals (US-06): ``Decision``, ``ApprovalOutcome``, ``ApprovalSummary`` and the
  ``approval.required`` / ``approval.resolved`` events.
- Transcript items (``UserTurn``, ``AssistantText``, ``ThinkingItem``, ``ToolCallItem``,
  ``ToolResultItem``): the
  graph state, stored as plain JSON by the checkpointer and re-validated on load (research §4).
- ``StreamEvent``: the SSE contract events, camelCase on the wire; the API framer adds the ids.
- ``RunOutcome``: how one run ended, for storage and the ``chat_run`` log line.
- ``Conversation`` / ``StoredMessage`` / ``ConversationHistory``: read models of the store.
"""

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, ClassVar, Final, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    model_validator,
)
from pydantic.alias_generators import to_camel

from ecommerce_ai.agents.guardrails.events import GuardEvent
from ecommerce_ai.agents.guardrails.quotas import QuotaScope


class FinishReason(StrEnum):
    """How an answer ended; ``client_disconnected`` is stored, never sent."""

    COMPLETED = "completed"
    REPLACED = "replaced"
    TOOL_LIMIT = "tool_limit"
    CLIENT_DISCONNECTED = "client_disconnected"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"
    TOOL_LOOP = "tool_loop"


SENT_FINISH_REASONS: Final = frozenset(
    {
        FinishReason.COMPLETED,
        FinishReason.REPLACED,
        FinishReason.TOOL_LIMIT,
        FinishReason.AWAITING_APPROVAL,
        FinishReason.BLOCKED,
        FinishReason.TOOL_LOOP,
    }
)
"""Finish reasons a ``message.end`` event may carry."""


class Decision(StrEnum):
    """The customer's answer to a proposed action (US-06); only the decision endpoint sets it."""

    APPROVE = "approve"
    REJECT = "reject"


class ApprovalOutcome(StrEnum):
    """What finally happened to a proposed action (audit ``chat_actions.outcome``)."""

    CANCELLED = "cancelled"
    REJECTED = "rejected"
    NOT_CANCELLABLE = "not_cancellable"
    FAILED = "failed"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"


RESOLVED_OUTCOMES: Final = frozenset(
    {
        ApprovalOutcome.CANCELLED,
        ApprovalOutcome.REJECTED,
        ApprovalOutcome.NOT_CANCELLABLE,
        ApprovalOutcome.FAILED,
    }
)
"""Outcomes of a decision, as an ``approval.resolved`` event may carry them."""


class ChatErrorCode(StrEnum):
    """Code of a terminal ``error`` event."""

    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    RATE_LIMITED = "RATE_LIMITED"
    TOOL_FAILED = "TOOL_FAILED"
    INTERNAL = "INTERNAL"


ERROR_MESSAGES: Final[Mapping[ChatErrorCode, str]] = MappingProxyType(
    {
        ChatErrorCode.LLM_UNAVAILABLE: (
            "The assistant is unavailable right now. Please try again later."
        ),
        ChatErrorCode.BUDGET_EXCEEDED: (
            "The assistant has reached its usage limit. Please try again later."
        ),
        ChatErrorCode.RATE_LIMITED: "You are sending messages too fast. Please wait a moment.",
        ChatErrorCode.TOOL_FAILED: "Your session has expired. Please sign in again.",
        ChatErrorCode.INTERNAL: "Something went wrong. Please try again.",
    }
)
"""The fixed, customer-safe message of each error code (contract table)."""


class MessageRole(StrEnum):
    """Author of a stored message."""

    USER = "user"
    ASSISTANT = "assistant"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Camel(BaseModel):
    """camelCase on the wire, snake_case in Python."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", alias_generator=to_camel, populate_by_name=True
    )


# Transcript items ---------------------------------------------------------------------------


class UserTurn(_Frozen):
    """A message the customer posted (trimmed)."""

    kind: Literal["user"] = "user"
    text: str = Field(min_length=1, description="The customer's text.")


class AssistantText(_Frozen):
    """Text the assistant sent to the customer (gatekept)."""

    kind: Literal["assistant"] = "assistant"
    text: str = Field(description="The text actually sent.")


class ToolCallItem(_Frozen):
    """A tool call the model asked for."""

    kind: Literal["tool_call"] = "tool_call"
    call_id: str = Field(min_length=1, description="Model-assigned id of the call.")
    name: str = Field(min_length=1, description="Requested tool name (may be unknown).")
    arguments: dict[str, JsonValue] = Field(description="Arguments as the model sent them.")


class ToolResultItem(_Frozen):
    """The result of a tool call: an untrusted-data envelope or ``{error, message}``."""

    kind: Literal["tool_result"] = "tool_result"
    call_id: str = Field(min_length=1, description="Id of the call this answers.")
    name: str = Field(min_length=1, description="Tool name of the call.")
    ok: bool = Field(description="False for a failed or refused call.")
    payload: dict[str, JsonValue] = Field(description="Envelope or error body; never the token.")


class ThinkingItem(_Frozen):
    """A provider thinking block, replayed to the model unchanged (never sent to the customer)."""

    kind: Literal["thinking"] = "thinking"
    block: dict[str, JsonValue] = Field(description="The block exactly as the model returned it.")


type TranscriptItem = Annotated[
    UserTurn | AssistantText | ThinkingItem | ToolCallItem | ToolResultItem,
    Field(discriminator="kind"),
]

_TRANSCRIPT: Final = TypeAdapter(list[TranscriptItem])


def dump_transcript(items: Iterable[TranscriptItem]) -> list[dict[str, JsonValue]]:
    """Plain JSON dicts of ``items``, as kept in the graph state."""
    return [item.model_dump(mode="json") for item in items]


def load_transcript(raw: object) -> list[TranscriptItem]:
    """Re-validate a stored transcript; raises ``ValidationError`` when it is malformed."""
    return _TRANSCRIPT.validate_python(raw)


# Stream events ------------------------------------------------------------------------------


class Usage(_Camel):
    """Tokens one answer spent."""

    input_tokens: int = Field(default=0, ge=0, description="Input tokens of every model call.")
    output_tokens: int = Field(default=0, ge=0, description="Output tokens of every model call.")

    def __add__(self, other: "Usage") -> "Usage":
        """Sum of two usages."""
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    @property
    def total(self) -> int:
        """Input plus output tokens."""
        return self.input_tokens + self.output_tokens


class _Event(_Camel):
    EVENT: ClassVar[str]
    """The SSE ``event:`` name."""

    def data(self) -> dict[str, JsonValue]:
        """The JSON ``data:`` payload, camelCase."""
        return self.model_dump(mode="json", by_alias=True)


class MessageStart(_Event):
    """First event of every stream."""

    EVENT: ClassVar[str] = "message.start"
    conversation_id: UUID = Field(description="The conversation answered.")
    message_id: UUID = Field(description="Id of the assistant message being written.")


class Token(_Event):
    """A gatekept segment of the answer."""

    EVENT: ClassVar[str] = "token"
    text: str = Field(min_length=1, description="Text that has passed the gatekeeper.")


class ToolCall(_Event):
    """The assistant calls a tool."""

    EVENT: ClassVar[str] = "tool.call"
    call_id: str = Field(min_length=1, description="Id of the call.")
    name: str = Field(min_length=1, description="Tool name.")
    arguments: dict[str, JsonValue] = Field(description="Arguments; never the token.")


class ToolResult(_Event):
    """A tool call ended; ``summary`` is a fixed phrase, never upstream data."""

    EVENT: ClassVar[str] = "tool.result"
    call_id: str = Field(min_length=1, description="Id of the call.")
    name: str = Field(min_length=1, description="Tool name.")
    ok: bool = Field(description="False when the call failed or was refused.")
    summary: str = Field(description="Fixed phrase per tool and outcome.")


class MessageEnd(_Event):
    """Last event of a stream that answered."""

    EVENT: ClassVar[str] = "message.end"
    message_id: UUID = Field(description="Id of the assistant message.")
    finish_reason: FinishReason = Field(
        description="`completed`, `replaced`, `tool_limit` or `awaiting_approval`."
    )
    prompt_version: str = Field(min_length=1, description="System prompt version, e.g. v1.")
    model_id: str | None = Field(description="Model that answered; null when none did.")
    usage: Usage = Field(description="Tokens spent on the answer.")
    violations: tuple[str, ...] = Field(
        default=(),
        description=(
            "Gatekeeper rules the answer broke (rule names only, e.g. `ungrounded_fact`); empty"
            " unless `finishReason` is `replaced`."
        ),
    )

    @model_validator(mode="after")
    def _sent_reason(self) -> Self:
        if self.finish_reason not in SENT_FINISH_REASONS:
            raise ValueError("finish_reason client_disconnected is never sent")
        return self


class StreamError(_Event):
    """Last event of a stream that failed; the message is the code's fixed text."""

    EVENT: ClassVar[str] = "error"
    code: ChatErrorCode = Field(description="Error code.")
    message: str = Field(description="Fixed, customer-safe message of the code.")
    scope: QuotaScope | None = Field(
        default=None,
        description="`user` or `global`: whose limit `BUDGET_EXCEEDED`/`RATE_LIMITED` hit.",
    )

    @classmethod
    def for_code(cls, code: ChatErrorCode, scope: QuotaScope | None = None) -> "StreamError":
        """The error event of ``code`` with its fixed message (and ``scope`` for limit errors)."""
        limited = code in {ChatErrorCode.BUDGET_EXCEEDED, ChatErrorCode.RATE_LIMITED}
        return cls(code=code, message=ERROR_MESSAGES[code], scope=scope if limited else None)

    def data(self) -> dict[str, JsonValue]:
        """The payload; ``scope`` only on the limit errors."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class ApprovalSummary(_Camel):
    """What the customer approves, read from Orders at proposal time (never from the model)."""

    status: str = Field(min_length=1, description="Order status read just now, e.g. PENDING.")
    total_amount: float | None = Field(description="Order total read just now; null if unknown.")
    currency: str | None = Field(default=None, description="Currency, when Orders reports one.")


class ApprovalRequired(_Event):
    """The run paused: the customer must approve or reject this exact action (US-06)."""

    EVENT: ClassVar[str] = "approval.required"
    approval_id: UUID = Field(description="Id to post the decision for.")
    action: str = Field(min_length=1, description="The proposed tool, e.g. `cancel_order`.")
    order_id: UUID = Field(description="The order the action targets.")
    summary: ApprovalSummary = Field(description="Status and total read from Orders just now.")
    expires_at: AwareDatetime = Field(description="After this instant the approval is void.")


class ApprovalResolved(_Event):
    """First event after ``message.start`` on a decision stream: what the decision did."""

    EVENT: ClassVar[str] = "approval.resolved"
    approval_id: UUID = Field(description="The decided approval.")
    decision: Decision = Field(description="`approve` or `reject`.")
    outcome: ApprovalOutcome = Field(
        description="`cancelled`, `rejected`, `not_cancellable` or `failed`."
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome not in RESOLVED_OUTCOMES:
            raise ValueError("outcome expired or withdrawn is never streamed")
        if (self.decision is Decision.REJECT) != (self.outcome is ApprovalOutcome.REJECTED):
            raise ValueError("outcome rejected goes with decision reject, and only with it")
        return self


class MessageReplaced(_Event):
    """The answer already shown failed the whole-answer check: show ``text`` instead of it."""

    EVENT: ClassVar[str] = "message.replaced"
    message_id: UUID = Field(description="Id of the assistant message to replace.")
    text: str = Field(description="The full text the message now holds (a fixed message).")


class AnswerReplaced(_Frozen):
    """Graph-internal: the streamed answer became ``text`` (sent as ``message.replaced``)."""

    text: str


type StreamEvent = (
    MessageStart
    | Token
    | ToolCall
    | ToolResult
    | MessageReplaced
    | MessageEnd
    | StreamError
    | ApprovalRequired
    | ApprovalResolved
)

STREAM_EVENT_TYPES: Final = (
    MessageStart,
    Token,
    ToolCall,
    ToolResult,
    MessageReplaced,
    MessageEnd,
    StreamError,
    ApprovalRequired,
    ApprovalResolved,
)


# Run outcome --------------------------------------------------------------------------------


class ToolStat(_Frozen):
    """One tool call of a run, for the ``chat_run`` log line (no arguments or results)."""

    name: str
    outcome: str
    duration_ms: float = Field(ge=0)


class PendingApproval(_Frozen):
    """A checked ``cancel_order`` call the run paused on (graph state ``pending_approval``)."""

    approval_id: UUID = Field(description="New id, sent in `approval.required`.")
    action: str = Field(min_length=1, description="The intercepted mutating tool.")
    order_id: UUID = Field(description="The target, parsed from the arguments.")
    arguments: dict[str, JsonValue] = Field(description="Exact arguments; the only ones run.")
    summary: ApprovalSummary = Field(description="Read from Orders when the call was checked.")
    call_id: str = Field(min_length=1, description="The paused `tool_use` id.")
    proposed_at: AwareDatetime
    expires_at: AwareDatetime

    def required_event(self) -> ApprovalRequired:
        """The ``approval.required`` event of this proposal."""
        return ApprovalRequired(
            approval_id=self.approval_id,
            action=self.action,
            order_id=self.order_id,
            summary=self.summary,
            expires_at=self.expires_at,
        )


class ApprovalExecution(_Frozen):
    """What the ``approval`` node did with a decision; never streamed as such."""

    approval_id: UUID
    order_id: UUID
    decision: Decision
    outcome: ApprovalOutcome
    result: ToolResultItem = Field(description="The paused call's result, as the model sees it.")


class RunOutcome(_Frozen):
    """How one run ended: exactly one of ``finish_reason`` and ``error``.

    ``proposal`` is set when the run paused for an approval (``awaiting_approval``);
    ``execution`` when it resumed from a decision.
    """

    finish_reason: FinishReason | None = None
    error: ChatErrorCode | None = None
    text: str = Field(default="", description="Text actually sent to the customer.")
    usage: Usage = Field(default_factory=Usage)
    model_id: str | None = None
    tools: tuple[ToolStat, ...] = ()
    proposal: PendingApproval | None = None
    execution: ApprovalExecution | None = None
    violations: tuple[str, ...] = ()
    """Names of the gatekeeper rules the answer broke, once each, in order."""
    guard_events: tuple[GuardEvent, ...] = ()
    """What the guards did during the run (US-07), in order; content-free."""

    @model_validator(mode="after")
    def _one_ending(self) -> Self:
        if (self.finish_reason is None) == (self.error is None):
            raise ValueError("exactly one of finish_reason and error must be set")
        if (self.proposal is not None) != (self.finish_reason is FinishReason.AWAITING_APPROVAL):
            raise ValueError("a proposal goes with finish_reason awaiting_approval, and only it")
        return self


# Read models --------------------------------------------------------------------------------


class Conversation(_Frozen):
    """A conversation of one user."""

    id: UUID
    title: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    tool_allowlist: tuple[str, ...] | None = None
    """Tools the conversation may call, set on create (US-07 FR-010); ``None``: no allowlist
    (only stores without one, in tests). Never part of the HTTP responses."""


class StoredMessage(_Frozen):
    """A stored message; the assistant fields are ``None`` on user messages."""

    id: UUID
    role: MessageRole
    content: str
    created_at: AwareDatetime
    finish_reason: FinishReason | None = None
    prompt_version: str | None = None
    model_id: str | None = None
    usage: Usage | None = None


class ConversationHistory(_Frozen):
    """A conversation with its messages, oldest first."""

    conversation: Conversation
    messages: tuple[StoredMessage, ...]
