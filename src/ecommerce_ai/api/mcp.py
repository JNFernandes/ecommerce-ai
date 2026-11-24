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
Token checks and response bodies are shared with ``/chat`` through ``api/auth.py``.
"""

from dataclasses import dataclass
from typing import Final

from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ecommerce_ai.api.auth import (
    AuthRejected,
    RejectReason,
    authenticate,
    log_rejection,
    rejection_response,
    request_id,
)
from ecommerce_ai.api.route_policy import PolicyRoute, roles
from ecommerce_ai.infrastructure.auth import TokenVerifier
from ecommerce_ai.infrastructure.caller import RequestCaller, bind_caller
from ecommerce_ai.tools.server import MCP_PATH

REQUIRED_ROLE: Final = "CUSTOMER"
"""Role a token must carry to use the MCP tools."""


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
        try:
            token, identity = await authenticate(
                headers, self._verifier, required_role=REQUIRED_ROLE
            )
        except AuthRejected as rejected:
            log_rejection("mcp_auth_rejected", rejected.reason)
            await rejection_response(rejected.reason)(scope, receive, send)
            return
        caller = RequestCaller(
            identity=identity, token=SecretStr(token), request_id=request_id(headers)
        )
        buffered: list[Message] = []

        async def buffer(message: Message) -> None:
            buffered.append(message)

        with bind_caller(caller):
            await self._app(scope, receive, buffer)
        if caller.upstream_unauthenticated:
            log_rejection("mcp_auth_rejected", RejectReason.UPSTREAM_UNAUTHENTICATED)
            await rejection_response(RejectReason.UPSTREAM_UNAUTHENTICATED)(scope, receive, send)
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
