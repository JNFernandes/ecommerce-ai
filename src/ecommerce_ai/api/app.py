"""FastAPI application assembly: routers, the MCP endpoint and middleware, without I/O wiring."""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ecommerce_ai.api import health
from ecommerce_ai.api.body_limit import BodySizeLimitMiddleware
from ecommerce_ai.api.chat import (
    MAX_BODY_BYTES,
    PREFIX,
    ChatHttpCode,
    error_response,
    install_chat_error_handlers,
)
from ecommerce_ai.api.health import ConsumerStateSource, RelayStateSource, WorkerStateSource
from ecommerce_ai.api.mcp import McpEndpoint
from ecommerce_ai.api.middleware import REQUEST_ID_HEADER, RequestIdMiddleware
from ecommerce_ai.api.route_policy import assert_route_policies
from ecommerce_ai.infrastructure.health import ReadinessChecker

Resources = Callable[[], AbstractAsyncContextManager[None]]
"""Factory of the application's own resources (shared HTTP client), opened for its lifetime."""


def _lifespan(
    mcp: McpEndpoint, resources: Resources
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Return a lifespan that opens ``resources``, then runs the MCP app's lifespan inside it."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with resources(), mcp.app.router.lifespan_context(mcp.app):
            yield

    return lifespan


def build_api(
    cors_allowed_origins: Sequence[str],
    readiness_checker: ReadinessChecker,
    *,
    mcp: McpEndpoint,
    resources: Resources,
    chat: APIRouter,
    consumer_state: ConsumerStateSource | None = None,
    worker_state: WorkerStateSource | None = None,
    relay_state: RelayStateSource | None = None,
) -> FastAPI:
    """Create the FastAPI application.

    CORS is enabled only for the configured origins. ``mcp`` adds the secured MCP route and
    chains the MCP app's lifespan; ``resources`` is opened around it for the app's lifetime.
    ``chat`` is the ``/chat`` router; its validation and outage errors get ``{code, message}``,
    and its request bodies are limited to ``MAX_BODY_BYTES`` (innermost middleware, 413).
    ``consumer_state``, ``worker_state`` and ``relay_state`` (``None`` when disabled) are
    reported by ``/ready``.
    Every route must declare its access, or this raises ``RoutePolicyError`` (default deny). The
    request-id middleware is outermost, so every log line of a request carries its
    ``request_id``.
    """
    app = FastAPI(
        lifespan=_lifespan(mcp, resources),
        title="ecommerce-ai",
        summary="MCP tools, order triage and a conversational assistant for the ecommerce system.",
        version="0.1.0",
    )
    app.state.readiness_checker = readiness_checker
    app.state.consumer_state = consumer_state
    app.state.worker_state = worker_state
    app.state.relay_state = relay_state
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=MAX_BODY_BYTES,
        path_prefix=PREFIX,
        reject=lambda: error_response(ChatHttpCode.PAYLOAD_TOO_LARGE),
    )
    if cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_allowed_origins),
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
            expose_headers=[REQUEST_ID_HEADER],
        )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    app.include_router(chat)
    install_chat_error_handlers(app)
    app.router.routes.append(mcp.route)
    assert_route_policies(app)
    return app
