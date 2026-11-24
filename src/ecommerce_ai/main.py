"""Composition root: wires settings, logging, probes, the MCP tools and the API into one app.

Run with ``python -m ecommerce_ai`` (reads ``HOST`` and ``PORT``) or
``uvicorn ecommerce_ai.main:create_app --factory``; ``python -m ecommerce_ai migrate`` applies the
database migrations instead. Nothing here connects to Kafka, PostgreSQL,
Identity, Orders or the LLM provider, so the service starts without any of them (constitution
IV, FR-011): the triage consumer runs as a background task that connects on its own, and the
database engine opens its first connection on first use. Each upstream (Orders, which JWKS
shares, Inventory, Notifications) has its own HTTP client, so a slow service cannot exhaust
another's connection pool; clients connect only on first use and are all closed when the app
shuts down, after the consumer has stopped. An invalid tool registry makes ``create_app``
raise ``ToolRegistryError``.
"""

import argparse
import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import structlog
import uvicorn
from fastapi import FastAPI

from ecommerce_ai.agents.triage_intake.service import TriageIntakeService
from ecommerce_ai.api.app import build_api
from ecommerce_ai.api.mcp import mcp_endpoint
from ecommerce_ai.infrastructure.auth import TokenVerifier
from ecommerce_ai.infrastructure.db.dead_letter_repository import DeadLetterRepository
from ecommerce_ai.infrastructure.db.engine import build_engine
from ecommerce_ai.infrastructure.db.migrate import run_migrations
from ecommerce_ai.infrastructure.db.triage_repository import TriageRepository
from ecommerce_ai.infrastructure.health import DependencyProbe, ReadinessChecker, build_probes
from ecommerce_ai.infrastructure.jwks import JwksCache
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.orders_client import OrdersClient
from ecommerce_ai.infrastructure.rest_client import RestClient
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.infrastructure.upstream import UpstreamTransport
from ecommerce_ai.messaging.consumer_state import ConsumerState
from ecommerce_ai.messaging.contracts import TOPIC_MODELS
from ecommerce_ai.messaging.kafka import AIOKafkaConsumerAdapter, ConsumerFactory
from ecommerce_ai.messaging.triage_consumer import TriageConsumer
from ecommerce_ai.tools.inventory import get_product_tool, list_products_tool
from ecommerce_ai.tools.notifications import list_my_notifications_tool
from ecommerce_ai.tools.orders import get_order_tool, list_my_orders_tool
from ecommerce_ai.tools.registry import AnyToolDefinition, ToolRegistry
from ecommerce_ai.tools.server import build_mcp_app, build_mcp_server

logger = structlog.get_logger(__name__)


def build_tool_definitions(
    settings: Settings,
    orders: OrdersClient,
    inventory: RestClient,
    notifications: RestClient,
) -> Sequence[AnyToolDefinition]:
    """Return the definitions of every tool the MCP server serves, each on its upstream client."""
    timeout = settings.tool_timeout_seconds
    max_bytes = settings.tool_max_response_bytes
    text_limit = settings.tool_text_max_chars
    return [
        get_order_tool(orders, timeout=timeout, max_bytes=max_bytes, text_limit=text_limit),
        list_my_orders_tool(orders, timeout=timeout, max_bytes=max_bytes, text_limit=text_limit),
        list_products_tool(inventory, timeout=timeout, max_bytes=max_bytes, text_limit=text_limit),
        get_product_tool(inventory, timeout=timeout, max_bytes=max_bytes, text_limit=text_limit),
        list_my_notifications_tool(
            notifications, timeout=timeout, max_bytes=max_bytes, text_limit=text_limit
        ),
    ]


def new_http_client() -> httpx.AsyncClient:
    """Return an HTTP client for one upstream; it connects on first request only."""
    return httpx.AsyncClient(follow_redirects=False)


def _rest_client(
    http: httpx.AsyncClient, service: str, base_url: str, settings: Settings
) -> RestClient:
    transport = UpstreamTransport(
        http,
        service,
        max_retries=settings.upstream_max_retries,
        backoff_base=settings.upstream_backoff_base_seconds,
    )
    return RestClient(transport, base_url)


def kafka_consumer_factory(settings: Settings) -> ConsumerFactory:
    """Return a factory of aiokafka consumers of the three failure topics."""
    return lambda: AIOKafkaConsumerAdapter(settings, tuple(TOPIC_MODELS))


async def stop_consumer(task: asyncio.Task[None], stop: asyncio.Event, timeout: float) -> None:
    """Ask the consumer to stop, cancel it after ``timeout`` seconds, and log how it ended."""
    stop.set()
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        await asyncio.wait({task})
        logger.warning("triage_consumer_cancelled", timeout_seconds=timeout)
        return
    error = task.exception()
    if error is not None:
        # The class name only: driver messages can echo connection details.
        logger.error("triage_consumer_failed", error=type(error).__name__)


def create_app(
    settings: Settings | None = None,
    probes: Sequence[DependencyProbe] | None = None,
    consumer_factory: ConsumerFactory | None = None,
) -> FastAPI:
    """Build the application; ``probes`` and ``consumer_factory`` replace the real ones in tests."""
    settings = settings or Settings()
    configure_logging(settings.log_level.value)
    checker = ReadinessChecker(build_probes(settings) if probes is None else probes)

    # No connection is opened here: httpx connects on first request only.
    orders_http = new_http_client()
    inventory_http = new_http_client()
    notifications_http = new_http_client()
    jwks = JwksCache(
        orders_http,
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
        orders_http,
        settings.orders_graphql_url,
        max_retries=settings.upstream_max_retries,
        backoff_base=settings.upstream_backoff_base_seconds,
    )
    inventory = _rest_client(inventory_http, "inventory", settings.inventory_base_url, settings)
    notifications = _rest_client(
        notifications_http, "notifications", settings.notifications_base_url, settings
    )
    registry = ToolRegistry.build(
        build_tool_definitions(settings, orders, inventory, notifications)
    )
    mcp = mcp_endpoint(build_mcp_app(build_mcp_server(registry)), verifier)

    # Building the engine opens no connection; the consumer connects in its own task.
    engine = build_engine(settings)
    consumer_state = ConsumerState()
    triage_consumer = TriageConsumer(
        consumer_factory or kafka_consumer_factory(settings),
        TriageIntakeService(TriageRepository(engine)),
        DeadLetterRepository(engine),
        consumer_state,
        settings,
    )

    @asynccontextmanager
    async def resources() -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for client in (orders_http, inventory_http, notifications_http):
                stack.push_async_callback(client.aclose)
            stack.push_async_callback(engine.dispose)
            if settings.triage_consumer_enabled:
                stop = asyncio.Event()
                task = asyncio.create_task(triage_consumer.run(stop), name="triage-consumer")
                # Pushed last, so it runs first: the consumer stops before the engine closes.
                stack.push_async_callback(
                    stop_consumer, task, stop, settings.consumer_shutdown_timeout_seconds
                )
            yield

    return build_api(
        settings.cors_allowed_origins,
        checker,
        mcp=mcp,
        resources=resources,
        consumer_state=consumer_state if settings.triage_consumer_enabled else None,
    )


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


def migrate() -> int:
    """Apply the database migrations; return the process exit code (0 on success)."""
    settings = Settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger(__name__)
    try:
        asyncio.run(run_migrations(settings))
    except Exception as exc:
        # The class name only: driver messages can echo connection details.
        logger.error("migrations_failed", error=type(exc).__name__)
        return 1
    logger.info("migrations_applied")
    return 0


def cli(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``python -m ecommerce_ai [migrate]``; return the process exit code."""
    parser = argparse.ArgumentParser(prog="python -m ecommerce_ai")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["serve", "migrate"],
        default="serve",
        help="`serve` (default) runs the HTTP service; `migrate` upgrades the database schema.",
    )
    args = parser.parse_args(argv)
    if args.command == "migrate":
        return migrate()
    run()
    return 0
