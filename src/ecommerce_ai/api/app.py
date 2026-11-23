"""FastAPI application assembly: routers and middleware, without any I/O wiring."""

from collections.abc import Sequence

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ecommerce_ai.api import health
from ecommerce_ai.api.middleware import REQUEST_ID_HEADER, RequestIdMiddleware
from ecommerce_ai.api.route_policy import assert_route_policies
from ecommerce_ai.infrastructure.health import ReadinessChecker


def build_api(cors_allowed_origins: Sequence[str], readiness_checker: ReadinessChecker) -> FastAPI:
    """Create the FastAPI application.

    CORS is enabled only for the configured origins. Every route must declare its access, or this
    raises ``RoutePolicyError`` (default deny). The request-id middleware is outermost, so every
    log line of a request carries its ``request_id``.
    """
    app = FastAPI(
        title="ecommerce-ai",
        summary="MCP tools, order triage and a conversational assistant for the ecommerce system.",
        version="0.1.0",
    )
    app.state.readiness_checker = readiness_checker
    if cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_allowed_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
            expose_headers=[REQUEST_ID_HEADER],
        )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    assert_route_policies(app)
    return app
