"""The ``/chat`` routes: a customer's conversations and streamed answers (contracts/chat-api.md).

I/O only: authentication (``require_caller``, any role), validation, the ``{code, message}``
error bodies and SSE framing; posting and deleting go through ``ChatService``. Every store
call is scoped to the token subject, so an unknown, malformed or
another user's id gives the same 404 (FR-003). A storage outage (``is_unavailable``) is a 503
``UNAVAILABLE``; any other error propagates. Posting is rate limited per user after
authentication and before the body is validated (429 ``RATE_LIMITED`` with ``Retry-After``); a
full conversation or one with an answer in progress is a 409 (research §16).
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from ecommerce_ai.agents.assistant.limits import ConversationFull, StreamActive
from ecommerce_ai.agents.assistant.model import (
    Conversation,
    ConversationHistory,
    FinishReason,
    MessageRole,
    StoredMessage,
    Usage,
)
from ecommerce_ai.agents.assistant.ports import ConversationStore
from ecommerce_ai.agents.assistant.service import ChatService
from ecommerce_ai.api.rate_limit import SlidingWindowLimiter
from ecommerce_ai.api.route_policy import AUTHENTICATED
from ecommerce_ai.api.sse import MEDIA_TYPE, sse_response
from ecommerce_ai.infrastructure.caller import RequestCaller

PREFIX: Final = "/chat"
TITLE_MAX_CHARS: Final = 200
CONTENT_MAX_CHARS: Final = 2000


class PostMessageRequest(BaseModel):
    """Body of `POST /chat/conversations/{id}/messages`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str = Field(
        description="The customer's message, trimmed: 1 to 2000 characters.",
        examples=["Where is my last order?"],
    )

    @field_validator("content", mode="after")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        text = value.strip()
        if not 1 <= len(text) <= CONTENT_MAX_CHARS:
            raise ValueError("content must hold 1 to 2000 characters")
        return text


class ChatHttpCode(StrEnum):
    """Code of an error answered before any stream opens."""

    NOT_FOUND = "NOT_FOUND"
    STREAM_ACTIVE = "STREAM_ACTIVE"
    CONVERSATION_FULL = "CONVERSATION_FULL"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    UNAVAILABLE = "UNAVAILABLE"


HTTP_MESSAGES: Final = {
    ChatHttpCode.NOT_FOUND: "Conversation not found.",
    ChatHttpCode.STREAM_ACTIVE: "An answer is already in progress in this conversation.",
    ChatHttpCode.CONVERSATION_FULL: "This conversation is full. Please start a new one.",
    ChatHttpCode.VALIDATION_FAILED: "The request is not valid.",
    ChatHttpCode.RATE_LIMITED: "You are sending messages too fast. Please wait a moment.",
    ChatHttpCode.UNAVAILABLE: "The assistant is unavailable right now. Please try again later.",
}
"""The fixed message of each code; never request or storage details."""

_STATUS: Final = {
    ChatHttpCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ChatHttpCode.STREAM_ACTIVE: status.HTTP_409_CONFLICT,
    ChatHttpCode.CONVERSATION_FULL: status.HTTP_409_CONFLICT,
    ChatHttpCode.VALIDATION_FAILED: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ChatHttpCode.RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
    ChatHttpCode.UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
}


class _Camel(BaseModel):
    model_config = ConfigDict(frozen=True, alias_generator=to_camel, populate_by_name=True)


class ErrorBody(_Camel):
    """Error answered before the stream opens."""

    code: str = Field(description="Error code, e.g. `NOT_FOUND`.", examples=["NOT_FOUND"])
    message: str = Field(description="Fixed, customer-safe message of the code.")


class CreateConversationRequest(_Camel):
    """Body of `POST /chat/conversations`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str | None = Field(
        default=None,
        max_length=TITLE_MAX_CHARS,
        description="Optional title, trimmed; blank means none. No title is generated.",
        examples=["My last order"],
    )

    @field_validator("title", mode="after")
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None


class ConversationResponse(_Camel):
    """A conversation of the caller."""

    id: UUID = Field(description="Conversation id.")
    title: str | None = Field(description="Title, or `null`.")
    created_at: datetime = Field(description="Creation time, UTC ISO 8601.")
    updated_at: datetime = Field(description="Time of the last message, UTC ISO 8601.")

    @classmethod
    def of(cls, conversation: Conversation) -> "ConversationResponse":
        """The response of ``conversation``."""
        return cls.model_validate(conversation.model_dump())


class UsageResponse(_Camel):
    """Tokens one answer spent."""

    input_tokens: int = Field(description="Input tokens.")
    output_tokens: int = Field(description="Output tokens.")


class MessageResponse(_Camel):
    """A stored message; assistant fields are `null` on user messages."""

    id: UUID = Field(description="Message id (the stream's `messageId` for an answer).")
    role: MessageRole = Field(description="`user` or `assistant`.")
    content: str = Field(description="The text posted, or the answer text actually sent.")
    created_at: datetime = Field(description="Creation time, UTC ISO 8601.")
    finish_reason: FinishReason | None = Field(description="How the answer ended.")
    prompt_version: str | None = Field(description="System prompt version of the answer.")
    model_id: str | None = Field(description="Model that answered, or `null`.")
    usage: UsageResponse | None = Field(description="Tokens the answer spent.")

    @classmethod
    def of(cls, message: StoredMessage) -> "MessageResponse":
        """The response of ``message``."""
        usage: Usage | None = message.usage
        return cls(
            id=message.id,
            role=message.role,
            content=message.content,
            created_at=message.created_at,
            finish_reason=message.finish_reason,
            prompt_version=message.prompt_version,
            model_id=message.model_id,
            usage=None if usage is None else UsageResponse.model_validate(usage.model_dump()),
        )


class ConversationDetailResponse(ConversationResponse):
    """A conversation with its messages, oldest first."""

    messages: tuple[MessageResponse, ...] = Field(description="Messages, oldest first.")

    @classmethod
    def of_history(cls, history: ConversationHistory) -> "ConversationDetailResponse":
        """The response of ``history``."""
        return cls.model_validate(
            history.conversation.model_dump()
            | {"messages": tuple(MessageResponse.of(m) for m in history.messages)}
        )


class ConversationListResponse(_Camel):
    """One page of the caller's conversations, newest activity first."""

    items: tuple[ConversationResponse, ...] = Field(description="The page.")
    total: int = Field(description="Number of the caller's conversations.")
    limit: int = Field(description="Page size asked for.")
    offset: int = Field(description="Conversations skipped.")


def error_response(code: ChatHttpCode, headers: dict[str, str] | None = None) -> JSONResponse:
    """The fixed ``{code, message}`` response of ``code``."""
    body = ErrorBody(code=code.value, message=HTTP_MESSAGES[code])
    return JSONResponse(body.model_dump(by_alias=True), status_code=_STATUS[code], headers=headers)


class ChatUnavailable(Exception):
    """Storage was unreachable; answered 503 ``UNAVAILABLE``."""


class RateLimited(Exception):
    """The caller posts too fast; answered 429 ``RATE_LIMITED`` with ``Retry-After``."""

    def __init__(self, retry_after: int) -> None:
        """Ask the caller to wait ``retry_after`` whole seconds."""
        super().__init__("rate limited")
        self.retry_after = retry_after


def install_chat_error_handlers(app: FastAPI) -> None:
    """Answer ``/chat`` validation errors and outages with ``{code, message}`` bodies."""

    async def on_validation(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, RequestValidationError):  # registered for this class only
            raise exc
        if request.url.path.startswith(PREFIX + "/"):
            return error_response(ChatHttpCode.VALIDATION_FAILED)
        return await request_validation_exception_handler(request, exc)

    async def on_unavailable(_request: Request, _exc: Exception) -> Response:
        return error_response(ChatHttpCode.UNAVAILABLE)

    async def on_rate_limited(_request: Request, exc: Exception) -> Response:
        if not isinstance(exc, RateLimited):  # registered for this class only
            raise exc
        return error_response(ChatHttpCode.RATE_LIMITED, {"Retry-After": str(exc.retry_after)})

    app.add_exception_handler(RequestValidationError, on_validation)
    app.add_exception_handler(ChatUnavailable, on_unavailable)
    app.add_exception_handler(RateLimited, on_rate_limited)


_ERRORS: Final[dict[int | str, dict[str, object]]] = {
    401: {"description": "Missing or invalid token (same body as `/mcp`)."},
    403: {"description": "The token carries no role."},
    503: {"model": ErrorBody, "description": "`UNAVAILABLE`: storage or signing keys down."},
}
_NOT_FOUND: Final[dict[int | str, dict[str, object]]] = {
    404: {"model": ErrorBody, "description": "`NOT_FOUND`: unknown, malformed or not yours."},
}
_INVALID: Final[dict[int | str, dict[str, object]]] = {
    422: {"model": ErrorBody, "description": "`VALIDATION_FAILED`."},
}


_STREAM: Final[dict[int | str, dict[str, object]]] = {
    200: {"content": {MEDIA_TYPE: {}}, "description": "The answer as Server-Sent Events."},
    409: {
        "model": ErrorBody,
        "description": "`STREAM_ACTIVE` (an answer is in progress) or `CONVERSATION_FULL`.",
    },
    429: {
        "model": ErrorBody,
        "description": "`RATE_LIMITED`; `Retry-After` gives the seconds to wait.",
    },
}


def _parse_id(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except ValueError:
        return None


def build_chat_router(
    store: ConversationStore,
    service: ChatService,
    caller_dependency: Callable[[Request], AsyncIterator[RequestCaller]],
    *,
    clock: Callable[[], datetime],
    is_unavailable: Callable[[BaseException], bool],
    keepalive_seconds: float,
    limiter: SlidingWindowLimiter,
) -> APIRouter:
    """Return the ``/chat`` router: reads on ``store``, posts and deletes through ``service``.

    ``clock`` stamps new conversations; a stream sends a keep-alive comment after
    ``keepalive_seconds`` of silence; ``limiter`` bounds each user's posts.
    """
    router = APIRouter(prefix=PREFIX, tags=["chat"])
    Caller = Annotated[RequestCaller, Depends(caller_dependency)]  # a type alias

    async def rate_limited(caller: Caller) -> RequestCaller:
        """The caller, once its post fits in the rate limit (before the body is validated)."""
        retry_after = limiter.hit(caller.identity.subject)
        if retry_after is not None:
            raise RateLimited(retry_after)
        return caller

    LimitedCaller = Annotated[RequestCaller, Depends(rate_limited)]  # a type alias

    async def guarded[T](call: Awaitable[T]) -> T:
        try:
            return await call
        except Exception as exc:
            if is_unavailable(exc):
                raise ChatUnavailable from exc
            raise

    @router.post(
        "/conversations",
        status_code=status.HTTP_201_CREATED,
        summary="Create a conversation",
        description="Create an empty conversation of the caller.",
        responses=_ERRORS | _INVALID,
        openapi_extra=AUTHENTICATED,
    )
    async def create_conversation(
        caller: Caller, body: CreateConversationRequest | None = None
    ) -> ConversationResponse:
        identity = caller.identity
        title = body.title if body is not None else None
        created = await guarded(
            store.create(identity.subject, identity.customer_id, title, clock())
        )
        return ConversationResponse.of(created)

    @router.get(
        "/conversations",
        summary="List my conversations",
        description="The caller's conversations, most recent activity first.",
        responses=_ERRORS | _INVALID,
        openapi_extra=AUTHENTICATED,
    )
    async def list_conversations(
        caller: Caller,
        limit: Annotated[int, Query(ge=1, le=100, description="Page size.")] = 20,
        offset: Annotated[int, Query(ge=0, description="Conversations to skip.")] = 0,
    ) -> ConversationListResponse:
        page = await guarded(store.list(caller.identity.subject, limit, offset))
        return ConversationListResponse(
            items=tuple(ConversationResponse.of(c) for c in page.items),
            total=page.total,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/conversations/{conversation_id}",
        summary="Read a conversation",
        description="One conversation of the caller with its messages, oldest first.",
        responses=_ERRORS | _NOT_FOUND,
        response_model=ConversationDetailResponse,
        openapi_extra=AUTHENTICATED,
    )
    async def get_conversation(caller: Caller, conversation_id: str) -> Response:
        parsed = _parse_id(conversation_id)
        history = (
            None if parsed is None else await guarded(store.get(caller.identity.subject, parsed))
        )
        if history is None:
            return error_response(ChatHttpCode.NOT_FOUND)
        detail = ConversationDetailResponse.of_history(history)
        return JSONResponse(detail.model_dump(mode="json", by_alias=True))

    @router.delete(
        "/conversations/{conversation_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Delete a conversation",
        description="Delete one conversation of the caller, its messages and its memory.",
        responses=_ERRORS | _NOT_FOUND,
        response_class=Response,
        openapi_extra=AUTHENTICATED,
    )
    async def delete_conversation(caller: Caller, conversation_id: str) -> Response:
        parsed = _parse_id(conversation_id)
        deleted = parsed is not None and await guarded(
            service.delete(caller.identity.subject, parsed)
        )
        if not deleted:
            return error_response(ChatHttpCode.NOT_FOUND)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/conversations/{conversation_id}/messages",
        summary="Ask the assistant",
        description=(
            "Store the caller's message and stream the answer as Server-Sent Events: one"
            " `message.start`, then `tool.call`, `tool.result` and `token` events, then exactly"
            " one `message.end` or `error`."
        ),
        responses=_ERRORS | _NOT_FOUND | _INVALID | _STREAM,
        response_class=Response,
        openapi_extra=AUTHENTICATED,
    )
    async def post_message(
        caller: LimitedCaller, conversation_id: str, body: PostMessageRequest
    ) -> Response:
        parsed = _parse_id(conversation_id)
        try:
            run = (
                None
                if parsed is None
                else await guarded(service.start(caller.identity.subject, parsed, body.content))
            )
        except ConversationFull:
            return error_response(ChatHttpCode.CONVERSATION_FULL)
        except StreamActive:
            return error_response(ChatHttpCode.STREAM_ACTIVE)
        if run is None:
            return error_response(ChatHttpCode.NOT_FOUND)
        return sse_response(run.events(), keepalive_seconds, on_close=run.discard)

    return router
