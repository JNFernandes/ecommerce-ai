"""Bearer authentication shared by ``/mcp`` and ``/chat`` (constitution II, research §14).

``authenticate`` reads ``Authorization: Bearer <token>``, verifies it locally with
``TokenVerifier`` and checks the role; every failure is an ``AuthRejected`` whose ``reason`` maps
to one fixed response: the same 401 (``WWW-Authenticate: Bearer``) for every missing or bad token,
503 when no signing key can be obtained, 403 without the required role. ``/mcp`` uses it from its
ASGI middleware; ``/chat`` routes use the ``require_caller`` FastAPI dependency, which accepts any
token with at least one role (``/usage`` asks for ``ADMIN``) and binds the ``RequestCaller`` for
the request (ADR 0001).
"""

from collections.abc import AsyncIterator, Callable
from enum import StrEnum
from typing import Final

import structlog
from fastapi import HTTPException, Request
from pydantic import SecretStr
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from ecommerce_ai.api.middleware import REQUEST_ID_HEADER, accept_request_id
from ecommerce_ai.infrastructure.auth import InvalidToken, TokenVerifier
from ecommerce_ai.infrastructure.caller import CallerIdentity, RequestCaller, bind_caller
from ecommerce_ai.infrastructure.jwks import KeysUnavailable

_logger = structlog.get_logger(__name__)


class RejectReason(StrEnum):
    """Why a request was not authenticated (logged; never the token)."""

    MISSING_TOKEN = "missing_token"  # noqa: S105  # a log reason, not a secret
    INVALID_TOKEN = "invalid_token"  # noqa: S105  # a log reason, not a secret
    KEYS_UNAVAILABLE = "keys_unavailable"
    MISSING_ROLE = "missing_role"
    UPSTREAM_UNAUTHENTICATED = "upstream_unauthenticated"


_STATUS: Final = {
    RejectReason.MISSING_TOKEN: 401,
    RejectReason.INVALID_TOKEN: 401,
    RejectReason.UPSTREAM_UNAUTHENTICATED: 401,
    RejectReason.KEYS_UNAVAILABLE: 503,
    RejectReason.MISSING_ROLE: 403,
}
_DETAIL: Final = {401: "Not authenticated", 403: "Forbidden", 503: "Service unavailable"}
_HEADERS: Final = {401: {"WWW-Authenticate": "Bearer"}}


class AuthRejected(Exception):
    """A request the caller may not make; ``reason`` picks the response."""

    def __init__(self, reason: RejectReason) -> None:
        """Reject for ``reason``."""
        super().__init__(reason.value)
        self.reason = reason

    @property
    def status_code(self) -> int:
        """401, 403 or 503."""
        return _STATUS[self.reason]


def rejection_response(reason: RejectReason) -> JSONResponse:
    """The fixed response of ``reason``: identical for every bad token."""
    status = _STATUS[reason]
    return JSONResponse(
        {"detail": _DETAIL[status]}, status_code=status, headers=_HEADERS.get(status)
    )


def bearer_token(headers: Headers) -> str | None:
    """Return the token of an ``Authorization: Bearer <token>`` header, if well formed."""
    scheme, _, token = headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        return None
    return token


def request_id(headers: Headers) -> str:
    """Return the request id bound by ``RequestIdMiddleware`` (or a validated/new one)."""
    bound = structlog.contextvars.get_contextvars().get("request_id")
    return bound if isinstance(bound, str) else accept_request_id(headers.get(REQUEST_ID_HEADER))


async def authenticate(
    headers: Headers, verifier: TokenVerifier, *, required_role: str | None
) -> tuple[str, CallerIdentity]:
    """Return the raw token and its verified identity, or raise ``AuthRejected``.

    ``required_role`` ``None`` accepts any token carrying at least one role.
    """
    token = bearer_token(headers)
    if token is None:
        raise AuthRejected(RejectReason.MISSING_TOKEN)
    try:
        identity = await verifier.verify(token)
    except InvalidToken as error:
        raise AuthRejected(RejectReason.INVALID_TOKEN) from error
    except KeysUnavailable as error:
        raise AuthRejected(RejectReason.KEYS_UNAVAILABLE) from error
    allowed = bool(identity.roles) if required_role is None else required_role in identity.roles
    if not allowed:
        raise AuthRejected(RejectReason.MISSING_ROLE)
    return token, identity


def log_rejection(event: str, reason: RejectReason) -> None:
    """Log one rejected request: a warning when the keys are unavailable, info otherwise."""
    log = _logger.warning if reason is RejectReason.KEYS_UNAVAILABLE else _logger.info
    log(event, reason=reason.value)


def require_caller(
    verifier: TokenVerifier,
    *,
    required_role: str | None = None,
    rejected_event: str = "chat_auth_rejected",
) -> Callable[[Request], AsyncIterator[RequestCaller]]:
    """Return a FastAPI dependency admitting a valid token with ``required_role``.

    ``required_role`` ``None`` admits any role (``x-authenticated``). It answers like ``/mcp``
    (same 401/403/503 bodies), logs a rejection as ``rejected_event`` and binds the
    ``RequestCaller`` while the route runs.
    """

    async def dependency(request: Request) -> AsyncIterator[RequestCaller]:
        try:
            token, identity = await authenticate(
                request.headers, verifier, required_role=required_role
            )
        except AuthRejected as rejected:
            log_rejection(rejected_event, rejected.reason)
            status = rejected.status_code
            raise HTTPException(
                status, detail=_DETAIL[status], headers=_HEADERS.get(status)
            ) from rejected
        caller = RequestCaller(
            identity=identity, token=SecretStr(token), request_id=request_id(request.headers)
        )
        with bind_caller(caller):
            yield caller

    return dependency
