"""Correlation id middleware: one ``X-Request-Id`` per request, on every log line and response.

It also opens the request's root ``http.request`` span (US-08), so every span the request causes
shares its trace and carries the same ``request.id``.
"""

import re
import time
import uuid
from typing import Final

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ecommerce_ai.telemetry.attributes import HTTP_ROUTE, HTTP_STATUS_CODE, REQUEST_ID
from ecommerce_ai.telemetry.tracing import span

REQUEST_ID_HEADER: Final = "X-Request-Id"
"""Header that carries the correlation id in both directions."""

_VALID_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._:-]{1,128}")

logger = structlog.get_logger(__name__)


def accept_request_id(value: str | None) -> str:
    """Return the caller's request id when it is safe to log, otherwise a new UUID."""
    if value is not None and _VALID_REQUEST_ID.fullmatch(value):
        return value
    return str(uuid.uuid4())


def route_template(scope: Scope) -> str | None:
    """The matched route's path template (``/chat/conversations/{conversation_id}``), if any."""
    path = getattr(scope.get("route"), "path", None)
    return path if isinstance(path, str) else None


class RequestIdMiddleware:
    """Bind ``request_id`` to the logging context for the whole request and echo it back.

    An incoming ``X-Request-Id`` is reused when it is 1-128 characters of ``[A-Za-z0-9._:-]``; any
    other value (or none) is replaced by a new UUID, so a caller cannot inject text into the logs.
    One ``request completed`` line is logged per request, without headers or bodies, and one
    ``http.request`` span records the route template (never the raw path or query), the status
    code and the request id.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the next ASGI application."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = accept_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        started = time.perf_counter()
        with (
            structlog.contextvars.bound_contextvars(request_id=request_id),
            span("http.request", {REQUEST_ID: request_id}) as request_span,
        ):
            try:
                await self._app(scope, receive, send_with_request_id)
            finally:
                request_span.set_attribute(HTTP_STATUS_CODE, status_code)
                route = route_template(scope)
                if route is not None:
                    request_span.set_attribute(HTTP_ROUTE, route)
                logger.info(
                    "request completed",
                    method=scope["method"],
                    path=scope["path"],
                    status=status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
