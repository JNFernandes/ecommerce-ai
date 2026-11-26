"""Composition root: wires settings, logging, probes, the MCP tools and the API into one app.

Run with ``python -m ecommerce_ai`` (reads ``HOST`` and ``PORT``) or ``uvicorn
ecommerce_ai.main:create_app --factory``; ``python -m ecommerce_ai migrate`` applies the database
migrations instead. Nothing here connects to Kafka, PostgreSQL, Identity, Orders or the LLM
provider (its client is built only when ``model_configured``), so the service starts without any of
them (constitution IV, FR-011): the triage consumer runs as a background task that connects on its
own, and the database engine opens its first connection on first use. Each upstream (Orders, which
JWKS shares, Inventory, Notifications) has its own HTTP client, so a slow service cannot exhaust
another's connection pool; clients connect only on first use and are all closed when the app shuts
down, after the background tasks have stopped. With ``TRIAGE_WORKER_ENABLED`` the triage worker and
the outbox relay run as two more background tasks; both stop (cancelled after
``CONSUMER_SHUTDOWN_TIMEOUT_SECONDS``) before the database engine is disposed. An invalid tool
registry makes ``create_app`` raise ``ToolRegistryError``.
"""

import argparse
import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
import structlog
import uvicorn
from anthropic import AsyncAnthropic
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider

from ecommerce_ai.agents.assistant.approvals import APPROVAL_ACTIONS, TARGET_ARGUMENT
from ecommerce_ai.agents.assistant.graph import ChatGraph, load_assistant_prompt
from ecommerce_ai.agents.assistant.limits import ChatLimits
from ecommerce_ai.agents.assistant.ports import (
    ApprovalStore,
    ChatModel,
    Checkpointer,
    ConversationStore,
    ToolRunner,
)
from ecommerce_ai.agents.assistant.service import ChatService
from ecommerce_ai.agents.assistant.tool_results import ToolResultSanitizer
from ecommerce_ai.agents.assistant.tool_runner import InvokerToolRunner
from ecommerce_ai.agents.guardrails import Guardrails
from ecommerce_ai.agents.guardrails.canary import new_canary
from ecommerce_ai.agents.guardrails.events import GuardrailEventStore, UserUsageStore
from ecommerce_ai.agents.guardrails.input_rules import RuleInputGuard, load_input_rules
from ecommerce_ai.agents.guardrails.output_checks import LeakChecks
from ecommerce_ai.agents.guardrails.quotas import QuotaLimits, UserQuotas
from ecommerce_ai.agents.guardrails.tool_guard import CallGuard
from ecommerce_ai.agents.ports import LlmCallRecorder
from ecommerce_ai.agents.prompts.loader import load_prompt
from ecommerce_ai.agents.triage.gatekeeper import check
from ecommerce_ai.agents.triage.graph import TriageLimits, build_triage_graph
from ecommerce_ai.agents.triage.ports import (
    Clock,
    SystemClock,
    TokenBudget,
    TriageCaseQueue,
    TriageModel,
)
from ecommerce_ai.agents.triage.service import TriageService
from ecommerce_ai.agents.triage.worker import TriageWorker, WorkerState
from ecommerce_ai.agents.triage_intake.service import TriageIntakeService
from ecommerce_ai.agents.usage import LlmUsage, UsageSummaryStore
from ecommerce_ai.api.app import build_api
from ecommerce_ai.api.auth import require_caller
from ecommerce_ai.api.chat import build_chat_router
from ecommerce_ai.api.mcp import mcp_endpoint
from ecommerce_ai.api.usage import ADMIN_ROLE, build_usage_router
from ecommerce_ai.infrastructure.auth import TokenVerifier
from ecommerce_ai.infrastructure.db.approval_repository import ApprovalRepository
from ecommerce_ai.infrastructure.db.checkpoint_saver import PostgresCheckpointSaver
from ecommerce_ai.infrastructure.db.conversation_repository import ConversationRepository
from ecommerce_ai.infrastructure.db.dead_letter_repository import DeadLetterRepository
from ecommerce_ai.infrastructure.db.engine import build_engine, is_transient_db_error
from ecommerce_ai.infrastructure.db.guardrail_event_repository import GuardrailEventRepository
from ecommerce_ai.infrastructure.db.llm_call_repository import LlmCallRepository
from ecommerce_ai.infrastructure.db.migrate import run_migrations
from ecommerce_ai.infrastructure.db.outbox_repository import OutboxRepository
from ecommerce_ai.infrastructure.db.token_budget_repository import TokenBudgetRepository
from ecommerce_ai.infrastructure.db.triage_queue_repository import TriageQueueRepository
from ecommerce_ai.infrastructure.db.triage_repository import TriageRepository
from ecommerce_ai.infrastructure.db.usage_summary_repository import UsageSummaryRepository
from ecommerce_ai.infrastructure.db.user_usage_repository import UserUsageRepository
from ecommerce_ai.infrastructure.errors import error_fields
from ecommerce_ai.infrastructure.health import DependencyProbe, ReadinessChecker, build_probes
from ecommerce_ai.infrastructure.jwks import JwksCache
from ecommerce_ai.infrastructure.llm.anthropic_chat_model import build_anthropic_chat_model
from ecommerce_ai.infrastructure.llm.anthropic_model import build_anthropic_model
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.orders_client import OrdersClient
from ecommerce_ai.infrastructure.pricing import load_model_prices
from ecommerce_ai.infrastructure.rate_limit import SlidingWindowLimiter
from ecommerce_ai.infrastructure.rest_client import RestClient
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.infrastructure.tracing import build_tracer_provider, shutdown_tracer_provider
from ecommerce_ai.infrastructure.upstream import UpstreamTransport
from ecommerce_ai.messaging.consumer_state import ConsumerState
from ecommerce_ai.messaging.contracts import TOPIC_MODELS
from ecommerce_ai.messaging.kafka import (
    AIOKafkaConsumerAdapter,
    AIOKafkaProducerAdapter,
    ConsumerFactory,
    ProducerFactory,
)
from ecommerce_ai.messaging.outbox_relay import OutboxRelay, OutboxStore, RelayState
from ecommerce_ai.messaging.triage_consumer import TriageConsumer
from ecommerce_ai.messaging.triage_published import to_outbox_message
from ecommerce_ai.telemetry.tracing import use_provider
from ecommerce_ai.tools.inventory import get_product_tool, list_products_tool
from ecommerce_ai.tools.invoker import ToolInvoker
from ecommerce_ai.tools.mutating.cancel_order import cancel_order_tool
from ecommerce_ai.tools.mutating.policy import Exposure
from ecommerce_ai.tools.notifications import list_my_notifications_tool
from ecommerce_ai.tools.orders import get_order_tool, list_my_orders_tool
from ecommerce_ai.tools.registry import AnyToolDefinition, ToolRegistry
from ecommerce_ai.tools.server import build_mcp_app, build_mcp_server

logger = structlog.get_logger(__name__)

RATE_WINDOW_SECONDS: Final = 60.0
"""Window of the per-user minute limit (`USER_MESSAGES_PER_MINUTE`)."""


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


def build_chat_tool_definitions(
    settings: Settings, orders: RestClient
) -> Sequence[AnyToolDefinition]:
    """Return the chat-only tools: ``cancel_order`` on ``orders`` (based at ORDERS_BASE_URL)."""
    return [
        cancel_order_tool(
            orders,
            timeout=settings.tool_timeout_seconds,
            max_bytes=settings.tool_max_response_bytes,
            ttl_seconds=settings.approval_ttl_seconds,
            proposals_per_hour=settings.cancel_proposals_per_hour,
        )
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


def kafka_producer_factory(settings: Settings) -> ProducerFactory:
    """Return a factory of aiokafka producers (``acks=all``, idempotent)."""
    return lambda: AIOKafkaProducerAdapter(settings)


async def stop_task(task: asyncio.Task[None], stop: asyncio.Event, timeout: float) -> None:
    """Ask a background task to stop, cancel it after ``timeout`` seconds, log how it ended.

    The log events are ``<task name>_cancelled`` and ``<task name>_failed`` (dashes become
    underscores), with the error class and a sanitized message only.
    """
    event = task.get_name().replace("-", "_")
    stop.set()
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        await asyncio.wait({task})
        logger.warning(f"{event}_cancelled", timeout_seconds=timeout)
        return
    error = task.exception()
    if error is not None:
        # Root cause and a sanitized message (no password, no query values).
        logger.error(f"{event}_failed", **error_fields(error))


async def stop_consumer(task: asyncio.Task[None], stop: asyncio.Event, timeout: float) -> None:
    """Stop the triage consumer task (``triage_consumer_*`` log events)."""
    task.set_name("triage-consumer")
    await stop_task(task, stop, timeout)


def build_triage_worker(
    settings: Settings,
    queue: TriageCaseQueue,
    budget: TokenBudget,
    model: TriageModel | None,
    wake: asyncio.Event,
    usage: LlmUsage | None = None,
) -> TriageWorker:
    """Build the triage worker, which sets ``wake`` after each saved result.

    ``model=None`` (no key or model id) gives every case its cause's template; ``usage`` records
    every model call (``None``: nothing is recorded).
    """
    clock = SystemClock()
    graph = build_triage_graph(
        model=model,
        budget=budget,
        gatekeeper=check,
        prompt=load_prompt("triage", "v1"),
        limits=TriageLimits(
            max_retries=settings.llm_max_retries,
            case_token_cap=settings.case_token_cap,
            max_output_tokens=settings.llm_max_output_tokens,
            backoff_base_seconds=settings.triage_backoff_base_seconds,
        ),
        clock=clock,
        usage=usage,
    )
    service = TriageService(
        queue,
        graph,
        clock,
        settle_seconds=settings.triage_settle_seconds,
    )
    return TriageWorker(
        service,
        poll_interval_seconds=settings.triage_poll_interval_seconds,
        wake=wake,
        state=WorkerState(model_configured=model is not None),
        backoff_base_seconds=settings.triage_backoff_base_seconds,
        backoff_max_seconds=settings.triage_backoff_max_seconds,
        is_transient=is_transient_db_error,
    )


def build_guardrails(settings: Settings) -> Guardrails:
    """The chat's US-07 guards; an invalid input rules file raises, so the build fails."""
    input_guard = RuleInputGuard(load_input_rules())
    return Guardrails(
        input_guard=input_guard,
        tool_result_guard=ToolResultSanitizer(settings.tool_result_max_chars, input_guard),
        output_checks=LeakChecks(),
        tool_guard=CallGuard(dict.fromkeys(APPROVAL_ACTIONS, TARGET_ARGUMENT)),
        canary_factory=new_canary,
    )


async def _stop_tracing(provider: TracerProvider | None, *, owns: bool) -> None:
    """Flush ``provider`` off the event loop, and shut it down when this app built it."""
    if provider is None:
        return
    if owns:
        await asyncio.to_thread(shutdown_tracer_provider, provider)
    else:
        await asyncio.to_thread(provider.force_flush)


def create_app(
    settings: Settings | None = None,
    probes: Sequence[DependencyProbe] | None = None,
    consumer_factory: ConsumerFactory | None = None,
    producer_factory: ProducerFactory | None = None,
    triage_queue: TriageCaseQueue | None = None,
    outbox_store: OutboxStore | None = None,
    conversation_store: ConversationStore | None = None,
    chat_model: ChatModel | None = None,
    checkpointer: Checkpointer | None = None,
    tool_runner: ToolRunner | None = None,
    token_budget: TokenBudget | None = None,
    approval_store: ApprovalStore | None = None,
    clock: Clock | None = None,
    guard_store: GuardrailEventStore | None = None,
    guardrails: Guardrails | None = None,
    user_usage_store: UserUsageStore | None = None,
    tracer_provider: TracerProvider | None = None,
    llm_call_recorder: LlmCallRecorder | None = None,
    usage_summary_store: UsageSummaryStore | None = None,
) -> FastAPI:
    """Build the application; ``probes``, the Kafka factories, the stores, the chat model, the
    checkpointer, the chat's tool runner, the token budget, the approval store, the chat's
    clock, the guard audit store, the chat's guardrails, the per-user usage store, the tracer
    provider, the per-call usage recorder and the usage summary store replace the real ones in
    tests.

    Without ``chat_model`` the chat uses the Anthropic model when ``model_configured`` (else every
    answer ends ``LLM_UNAVAILABLE``); without ``checkpointer`` the graph state goes to PostgreSQL.
    ``/mcp`` serves only the MCP-exposed tools; the chat also gets ``cancel_order`` (US-06).
    Without ``tracer_provider`` the provider comes from the standard ``OTEL_*`` variables (none by
    default); one built here is flushed and shut down last when the app stops, an injected one is
    only flushed. The model price file (``MODEL_PRICES_PATH``) is read here: a missing or
    invalid file raises ``PriceFileError``.
    """
    settings = settings or Settings()
    configure_logging(settings.log_level.value)
    prices = load_model_prices(settings.model_prices_path)
    owns_provider = tracer_provider is None
    provider = build_tracer_provider(os.environ) if owns_provider else tracer_provider
    use_provider(provider)
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
    orders_rest = _rest_client(orders_http, "orders", settings.orders_base_url, settings)
    registry = ToolRegistry.build(
        [
            *build_tool_definitions(settings, orders, inventory, notifications),
            *build_chat_tool_definitions(settings, orders_rest),
        ]
    )
    mcp_registry = registry.exposed(Exposure.MCP)
    mcp = mcp_endpoint(build_mcp_app(build_mcp_server(mcp_registry)), verifier)

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
    # Building the worker, the model client and the relay opens no connection either.
    model: TriageModel | None = None
    llm_client: AsyncAnthropic | None = None
    if settings.model_configured:
        model, llm_client = build_anthropic_model(settings)
        if chat_model is None:
            chat_model = build_anthropic_chat_model(settings, llm_client)
    wake = asyncio.Event()
    usage = LlmUsage(llm_call_recorder or LlmCallRepository(engine), prices)
    budget = token_budget or TokenBudgetRepository(engine, settings.daily_token_budget)
    worker = build_triage_worker(
        settings,
        triage_queue or TriageQueueRepository(engine, to_outbox_message),
        budget,
        model,
        wake,
        usage,
    )
    relay_state = RelayState()
    relay = OutboxRelay(
        outbox_store or OutboxRepository(engine),
        producer_factory or kafka_producer_factory(settings),
        poll_interval_seconds=settings.outbox_poll_interval_seconds,
        max_attempts=settings.outbox_max_attempts,
        backoff_base_seconds=settings.outbox_backoff_base_seconds,
        backoff_max_seconds=settings.outbox_backoff_max_seconds,
        state=relay_state,
    )
    chat_registry = registry.exposed(Exposure.CHAT)
    store = conversation_store or ConversationRepository(
        engine, tuple(tool.name for tool in chat_registry)
    )
    chat_clock = clock or SystemClock()
    chat_graph = ChatGraph(
        model=chat_model,
        tools=tool_runner or InvokerToolRunner(ToolInvoker(chat_registry)),
        prompt=load_assistant_prompt(),
        checkpointer=checkpointer or PostgresCheckpointSaver(engine),
        max_tool_rounds=settings.chat_max_tool_rounds,
        max_retries=settings.llm_max_retries,
        clock=chat_clock,
        approval_ttl_seconds=settings.approval_ttl_seconds,
        guardrails=guardrails or build_guardrails(settings),
        usage=usage,
    )
    guard_events = guard_store or GuardrailEventRepository(engine)
    user_usage = user_usage_store or UserUsageRepository(engine)
    quotas = UserQuotas(
        SlidingWindowLimiter(settings.user_messages_per_minute, RATE_WINDOW_SECONDS),
        user_usage,
        guard_events,
        chat_clock,
        QuotaLimits(
            messages_per_day=settings.user_messages_per_day,
            violation_threshold=settings.guard_violation_threshold,
            violation_window=timedelta(seconds=settings.guard_violation_window_seconds),
            cooldown=timedelta(seconds=settings.guard_cooldown_seconds),
        ),
    )
    chat_service = ChatService(
        store,
        chat_graph,
        chat_clock,
        approvals=approval_store or ApprovalRepository(engine),
        proposals_per_hour=settings.cancel_proposals_per_hour,
        guard_store=guard_events,
        quotas=quotas,
        user_usage=user_usage,
        user_daily_tokens=settings.user_daily_token_budget,
        budget=budget,
        limits=ChatLimits(
            max_messages=settings.chat_max_messages,
            request_token_cap=settings.chat_request_token_cap,
            max_output_tokens=settings.llm_max_output_tokens,
        ),
    )
    chat = build_chat_router(
        store,
        chat_service,
        require_caller(verifier),
        clock=lambda: datetime.now(UTC),
        is_unavailable=is_transient_db_error,
        keepalive_seconds=settings.chat_keepalive_seconds,
        quotas=quotas,
    )
    usage_router = build_usage_router(
        usage_summary_store or UsageSummaryRepository(engine),
        require_caller(verifier, required_role=ADMIN_ROLE, rejected_event="usage_auth_rejected"),
        clock=lambda: datetime.now(UTC),
        is_unavailable=is_transient_db_error,
    )
    shutdown_timeout = settings.consumer_shutdown_timeout_seconds

    @asynccontextmanager
    async def resources() -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            # Pushed first, so it runs last: spans of the stopping tasks are still exported.
            stack.push_async_callback(_stop_tracing, provider, owns=owns_provider)
            for client in (orders_http, inventory_http, notifications_http):
                stack.push_async_callback(client.aclose)
            stack.push_async_callback(engine.dispose)
            if llm_client is not None:
                stack.push_async_callback(llm_client.close)
            if settings.triage_consumer_enabled:
                stop = asyncio.Event()
                task = asyncio.create_task(triage_consumer.run(stop), name="triage-consumer")
                # Pushed after the engine, so it runs first: it stops before the engine closes.
                stack.push_async_callback(stop_consumer, task, stop, shutdown_timeout)
            if settings.triage_worker_enabled:
                relay_stop = asyncio.Event()
                relay_task = asyncio.create_task(relay.run(relay_stop, wake), name="outbox-relay")
                stack.push_async_callback(stop_task, relay_task, relay_stop, shutdown_timeout)
                worker_stop = asyncio.Event()
                worker_task = asyncio.create_task(worker.run(worker_stop), name="triage-worker")
                # Pushed last, so it runs first: no new result is saved while the relay stops.
                stack.push_async_callback(stop_task, worker_task, worker_stop, shutdown_timeout)
            yield

    return build_api(
        settings.cors_allowed_origins,
        checker,
        mcp=mcp,
        resources=resources,
        chat=chat,
        usage=usage_router,
        consumer_state=consumer_state if settings.triage_consumer_enabled else None,
        worker_state=worker.state if settings.triage_worker_enabled else None,
        relay_state=relay_state if settings.triage_worker_enabled else None,
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
        # Root cause and a sanitized message (no password, no query values).
        logger.error("migrations_failed", **error_fields(exc))
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
