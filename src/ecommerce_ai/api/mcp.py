"""The ``/mcp`` endpoint: the MCP app behind local bearer-token verification (research 2, 3).

``BearerAuthMiddleware`` is a pure ASGI middleware in front of the FastMCP app. It answers every
missing or invalid token with one identical 401, 503 when no signing key can be obtained and 403
when the token lacks the required role, so no tool runs for a rejected caller. For an accepted
caller it binds a ``RequestCaller`` (identity, raw token, request id) for the request only, so
tools can forward the caller's own token (ADR 0001). ``mcp_route`` serves it at exactly
``/mcp`` with ``x-roles: [CUSTOMER]``, so a POST without a trailing slash never gets a redirect.

When Orders rejects the forwarded token during a call, the tool flags the caller; the middleware
then replaces the MCP response with the same 401 (research 8). The response is buffered for
that: it is one JSON body (``json_response=True``) built from size-capped tool results.
"""

from dataclasses import dataclass
from typing import Final

import structlog
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ecommerce_ai.api.middleware import REQUEST_ID_HEADER, accept_request_id
from ecommerce_ai.api.route_policy import PolicyRoute, roles
from ecommerce_ai.infrastructure.auth import InvalidToken, TokenVerifier
from ecommerce_ai.infrastructure.caller import RequestCaller, bind_caller
from ecommerce_ai.infrastructure.jwks import KeysUnavailable
from ecommerce_ai.tools.server import MCP_PATH

REQUIRED_ROLE: Final = "CUSTOMER"
"""Role a token must carry to use the MCP tools."""

_logger = structlog.get_logger(__name__)


def unauthenticated_response() -> JSONResponse:
    """Return the one 401 every rejected token gets."""
    return JSONResponse(
        {"detail": "Not authenticated"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
    )


def _forbidden_response() -> JSONResponse:
    return JSONResponse({"detail": "Forbidden"}, status_code=403)


def _unavailable_response() -> JSONResponse:
    return JSONResponse({"detail": "Service unavailable"}, status_code=503)


def _bearer_token(headers: Headers) -> str | None:
    """Return the token of an ``Authorization: Bearer <token>`` header, if well formed."""
    scheme, _, token = headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        return None
    return token


def _request_id(headers: Headers) -> str:
    """Return the request id bound by ``RequestIdMiddleware`` (or a validated/new one)."""
    bound = structlog.contextvars.get_contextvars().get("request_id")
    return bound if isinstance(bound, str) else accept_request_id(headers.get(REQUEST_ID_HEADER))


class BearerAuthMiddleware:
    """Verifies the bearer token and binds the caller before the wrapped app runs."""

    def __init__(self, app: ASGIApp, verifier: TokenVerifier) -> None:
        """Protect ``app`` with tokens checked by ``verifier``."""
        self._app = app
        self._verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject the request or run it with the caller bound."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        token = _bearer_token(headers)
        if token is None:
            _logger.info("mcp_auth_rejected", reason="missing_token")
            await unauthenticated_response()(scope, receive, send)
            return
        try:
            identity = await self._verifier.verify(token)
        except InvalidToken:
            _logger.info("mcp_auth_rejected", reason="invalid_token")
            await unauthenticated_response()(scope, receive, send)
            return
        except KeysUnavailable:
            _logger.warning("mcp_auth_rejected", reason="keys_unavailable")
            await _unavailable_response()(scope, receive, send)
            return
        if REQUIRED_ROLE not in identity.roles:
            _logger.info("mcp_auth_rejected", reason="missing_role")
            await _forbidden_response()(scope, receive, send)
            return
        caller = RequestCaller(
            identity=identity, token=SecretStr(token), request_id=_request_id(headers)
        )
        buffered: list[Message] = []

        async def buffer(message: Message) -> None:
            buffered.append(message)

        with bind_caller(caller):
            await self._app(scope, receive, buffer)
        if caller.upstream_unauthenticated:
            _logger.info("mcp_auth_rejected", reason="upstream_unauthenticated")
            await unauthenticated_response()(scope, receive, send)
            return
        for message in buffered:
            await send(message)


@dataclass(frozen=True, slots=True)
class McpEndpoint:
    """The secured route to mount and the MCP app whose lifespan the API must run."""

    route: PolicyRoute
    app: Starlette


def mcp_endpoint(mcp_app: Starlette, verifier: TokenVerifier) -> McpEndpoint:
    """Serve ``mcp_app`` at exactly ``MCP_PATH`` behind ``BearerAuthMiddleware``."""
    route = PolicyRoute(
        MCP_PATH,
        BearerAuthMiddleware(mcp_app, verifier),
        openapi_extra=roles(REQUIRED_ROLE),
        name="mcp",
    )
    return McpEndpoint(route=route, app=mcp_app)
