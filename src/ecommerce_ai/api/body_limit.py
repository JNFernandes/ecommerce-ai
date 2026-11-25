"""Request body size limit for the ``/chat`` routes (the gateway cannot buffer streamed routes).

The gateway's 1 MB limit is a buffering middleware, which also buffers responses and would hold
back every SSE event, so the stream route skips it and the size is enforced here instead. Under
``path_prefix`` the body is read fully before the app runs, up to ``max_bytes``: a larger declared
``Content-Length`` or a body that grows past the limit gets the ``reject`` response (413) and the
app never runs. Otherwise the app receives the body in one message, and every later ``receive``
goes to the server, so a client disconnect still reaches the SSE stream.
"""

from collections.abc import Callable

from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodySizeLimitMiddleware:
    """Answer ``reject()`` for bodies over ``max_bytes`` on paths under ``path_prefix``."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        path_prefix: str,
        reject: Callable[[], Response],
    ) -> None:
        """Wrap ``app``; ``reject`` builds the response sent instead of running it."""
        self._app = app
        self._max_bytes = max_bytes
        self._prefix = path_prefix
        self._reject = reject

    def _limited(self, scope: Scope) -> bool:
        path = str(scope["path"])
        return scope["type"] == "http" and (
            path == self._prefix or path.startswith(self._prefix + "/")
        )

    def _declared_too_large(self, scope: Scope) -> bool:
        declared = Headers(scope=scope).get("content-length")
        return declared is not None and declared.isdigit() and int(declared) > self._max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Read and check the body of a limited request, then run the app on it."""
        if not self._limited(scope):
            await self._app(scope, receive, send)
            return
        if self._declared_too_large(scope):
            await self._reject()(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            size += len(body)
            if size > self._max_bytes:
                await self._reject()(scope, receive, send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break
        await self._app(scope, _replay(b"".join(chunks), receive), send)


def _replay(body: bytes, receive: Receive) -> Receive:
    """A ``receive`` that gives ``body`` once, then defers to the server's ``receive``."""
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if sent:
            return await receive()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay
