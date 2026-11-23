"""Composition root: wires settings, logging, probes, the MCP tools and the API into one app.

Run with ``python -m ecommerce_ai`` (reads ``HOST`` and ``PORT``) or
``uvicorn ecommerce_ai.main:create_app --factory``. Nothing here connects to Kafka, PostgreSQL,
Identity, Orders or the LLM provider, so the service starts without any of them (constitution
IV, FR-011): the shared HTTP client opens connections only on first use and is closed when the
app shuts down. An invalid tool registry makes ``create_app`` raise ``ToolRegistryError``.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI

from ecommerce_ai.api.app import build_api
from ecommerce_ai.api.mcp import mcp_endpoint
from ecommerce_ai.infrastructure.auth import TokenVerifier
from ecommerce_ai.infrastructure.health import DependencyProbe, ReadinessChecker, build_probes
from ecommerce_ai.infrastructure.jwks import JwksCache
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.orders_client import OrdersClient
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.tools.orders import get_order_tool, list_my_orders_tool
from ecommerce_ai.tools.registry import AnyToolDefinition, ToolRegistry
from ecommerce_ai.tools.server import build_mcp_app, build_mcp_server


def build_tool_definitions(settings: Settings, orders: OrdersClient) -> Sequence[AnyToolDefinition]:
    """Return the definitions of every tool the MCP server serves."""
    timeout = settings.tool_timeout_seconds
    max_bytes = settings.tool_max_response_bytes
    text_limit = settings.tool_text_max_chars
    return [
        get_order_tool(orders, timeout=timeout, max_bytes=max_bytes, text_limit=text_limit),
        list_my_orders_tool(orders, timeout=timeout, max_bytes=max_bytes, text_limit=text_limit),
    ]


def create_app(
    settings: Settings | None = None, probes: Sequence[DependencyProbe] | None = None
) -> FastAPI:
    """Build the application; ``probes`` replaces the real dependency probes in tests."""
    settings = settings or Settings()
    configure_logging(settings.log_level.value)
    checker = ReadinessChecker(build_probes(settings) if probes is None else probes)

    # No connection is opened here: httpx connects on first request only.
    http = httpx.AsyncClient(follow_redirects=False)
    jwks = JwksCache(
        http,
        settings.identity_jwks_url,
        timeout=settings.jwks_timeout_seconds,
        refetch_cooldown=settings.jwks_refetch_cooldown_seconds,
    )
    verifier = TokenVerifier(
        jwks,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        leeway=settings.jwt_clock_tolerance_seconds,
    )
    orders = OrdersClient(
        http,
        settings.orders_graphql_url,
        max_retries=settings.upstream_max_retries,
        backoff_base=settings.upstream_backoff_base_seconds,
    )
    registry = ToolRegistry.build(build_tool_definitions(settings, orders))
    mcp = mcp_endpoint(build_mcp_app(build_mcp_server(registry)), verifier)

    @asynccontextmanager
    async def resources() -> AsyncIterator[None]:
        try:
            yield
        finally:
            await http.aclose()

    return build_api(settings.cors_allowed_origins, checker, mcp=mcp, resources=resources)


def run() -> None:
    """Serve the application on ``HOST``:``PORT`` with uvicorn."""
    settings = Settings()
    uvicorn.run(
        "ecommerce_ai.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        server_header=False,
    )
