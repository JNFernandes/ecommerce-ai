"""The verified caller of the current request, held in a ContextVar for that request only.

The auth middleware binds a ``RequestCaller`` with ``bind_caller`` after verifying the token;
tools read it with ``current_caller`` to forward the caller's own token upstream (ADR 0001).
The raw token is a ``SecretStr``: it never shows in ``repr``, logs or tool results, and it is
dropped when the request ends.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class CallerIdentity(BaseModel):
    """Claims of a verified access token."""

    model_config = ConfigDict(frozen=True)

    subject: str = Field(description="`sub` claim: the user id.")
    roles: frozenset[str] = Field(description="`roles` claim.")
    customer_id: UUID | None = Field(description="`customerId` claim, if the token has one.")


@dataclass(slots=True)
class RequestCaller:
    """The caller of one request: identity, raw token and request id.

    ``upstream_unauthenticated`` is the only mutable field: a tool sets it when the platform API
    rejects the token as unauthenticated, and the auth middleware then answers HTTP 401.
    """

    identity: CallerIdentity
    token: SecretStr
    request_id: str
    upstream_unauthenticated: bool = False


class NoCallerError(RuntimeError):
    """Raised when code reads the caller outside an authenticated request."""


_caller: ContextVar[RequestCaller] = ContextVar("request_caller")


def current_caller() -> RequestCaller:
    """Return the caller bound to the current request."""
    try:
        return _caller.get()
    except LookupError as error:
        raise NoCallerError("no authenticated caller is bound to this request") from error


@contextmanager
def bind_caller(caller: RequestCaller) -> Iterator[RequestCaller]:
    """Bind ``caller`` for the duration of the block, then reset it (also on errors)."""
    reset_token = _caller.set(caller)
    try:
        yield caller
    finally:
        _caller.reset(reset_token)
