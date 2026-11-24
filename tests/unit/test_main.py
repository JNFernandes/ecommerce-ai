"""The composition root starts without its dependencies and serves the configured port."""

import asyncio
import io
import json
import time
from collections.abc import Sequence
from datetime import timedelta

import httpx
import pytest
import respx
import uvicorn
from anthropic import AsyncAnthropic
from pydantic import SecretStr

from ecommerce_ai import main
from ecommerce_ai.agents.triage_intake.model import TriageCause, TriageStatus
from ecommerce_ai.infrastructure.health import DependencyStatus
from ecommerce_ai.infrastructure.llm.anthropic_model import (
    AnthropicTriageModel,
    build_anthropic_model,
)
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.orders_client import OrdersClient
from ecommerce_ai.infrastructure.outbox import OutboxMessage
from ecommerce_ai.infrastructure.rest_client import RestClient
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.main import create_app, run
from ecommerce_ai.messaging.kafka import (
    AIOKafkaConsumerAdapter,
    AIOKafkaProducerAdapter,
    ConsumedRecord,
    TopicPartition,
)
from ecommerce_ai.messaging.triage_published import to_outbox_message
from ecommerce_ai.tools.registry import AnyToolDefinition, ToolDefinition, ToolRegistryError
from tests.support.kafka import FakeConsumer, FakeProducer
from tests.support.mcp import serve_app
from tests.support.polling import wait_until
from tests.support.probes import FakeProbe
from tests.support.stores import InMemoryOutboxStore, InMemoryTriageCaseQueue
from tests.support.triage import NOW, make_case

DATABASE_DOWN = FakeProbe("database", critical=True, status=DependencyStatus.DOWN)


def test_should_serve_the_factory_on_the_configured_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    monkeypatch.setenv("PORT", "8123")

    run()

    ((app, kwargs),) = calls
    assert app == "ecommerce_ai.main:create_app"
    assert kwargs["factory"] is True
    assert (kwargs["host"], kwargs["port"]) == ("127.0.0.1", 8123)


async def test_should_start_and_answer_health_when_identity_and_orders_are_down() -> None:
    started = time.perf_counter()
    with respx.mock(assert_all_called=False) as router:
        router.route().mock(side_effect=httpx.ConnectError("everything is down"))
        async with serve_app(create_app(probes=[DATABASE_DOWN])) as client:
            response = await client.get("/health")

        assert router.calls.call_count == 0

    assert response.status_code == 200
    assert time.perf_counter() - started < 5


def test_should_raise_registry_error_when_a_tool_definition_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(_arguments: object) -> object:  # pragma: no cover
        raise AssertionError("never called")

    def invalid(
        _settings: Settings,
        _orders: OrdersClient,
        _inventory: RestClient,
        _notifications: RestClient,
    ) -> Sequence[AnyToolDefinition]:
        return [
            ToolDefinition(
                name="rogue",
                description="Calls an operation nobody allowlisted.",
                allowlist_entry=None,
                input_model=None,
                output_model=None,
                timeout_seconds=0,
                max_response_bytes=0,
                handler=handler,
            )
        ]

    monkeypatch.setattr(main, "build_tool_definitions", invalid)

    with pytest.raises(ToolRegistryError, match="rogue"):
        create_app(probes=[])


async def test_should_close_one_http_client_per_upstream_when_the_app_shuts_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[httpx.AsyncClient] = []

    def recording_client() -> httpx.AsyncClient:
        client = httpx.AsyncClient(follow_redirects=False)
        created.append(client)
        return client

    monkeypatch.setattr(main, "new_http_client", recording_client)

    async with serve_app(create_app(probes=[])) as client:
        assert (await client.get("/health")).status_code == 200
        assert not any(http.is_closed for http in created)

    assert len({id(http) for http in created}) == 3
    assert all(http.is_closed for http in created)


def test_should_pass_each_upstream_its_own_client_when_building_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[OrdersClient, RestClient, RestClient]] = []

    def record(
        _settings: Settings,
        orders: OrdersClient,
        inventory: RestClient,
        notifications: RestClient,
    ) -> Sequence[AnyToolDefinition]:
        seen.append((orders, inventory, notifications))
        return []

    monkeypatch.setattr(main, "build_tool_definitions", record)
    create_app(Settings(notifications_base_url="http://gateway:8090/api"), probes=[])

    ((_orders, inventory, notifications),) = seen
    assert inventory.url_for(["products"]) == "http://localhost:8090/api/inventory/products"
    assert notifications.url_for(["notifications"]) == "http://gateway:8090/api/notifications"


def test_should_serve_the_app_when_no_command_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(main, "run", lambda: calls.append("run"))

    assert main.cli([]) == 0
    assert main.cli(["serve"]) == 0
    assert calls == ["run", "run"]


def test_should_run_the_migrations_when_the_migrate_command_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[Settings] = []

    async def fake_run_migrations(settings: Settings) -> None:
        received.append(settings)

    monkeypatch.setattr(main, "run_migrations", fake_run_migrations)
    monkeypatch.setattr(main, "run", lambda: pytest.fail("migrate must not serve the app"))

    assert main.cli(["migrate"]) == 0
    assert len(received) == 1


def test_should_log_the_sanitized_cause_when_migrations_fail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing_run_migrations(settings: Settings) -> None:
        raise ConnectionRefusedError("postgresql://ai:s3cret@db:5436 refused")

    monkeypatch.setattr(main, "run_migrations", failing_run_migrations)

    assert main.cli(["migrate"]) == 1
    output = capsys.readouterr().out
    (line,) = [json.loads(text) for text in output.splitlines()]
    assert line["error"] == "ConnectionRefusedError"
    assert line["error_message"] == "postgresql://ai:[REDACTED]@db:5436 refused"
    assert "s3cret" not in output


def test_should_reject_an_unknown_command() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main.cli(["drop-database"])

    assert exit_info.value.code == 2


class StuckConsumer(FakeConsumer):
    """A consumer whose poll never returns, so only cancellation can stop it."""

    async def getmany(self, timeout_ms: int) -> dict[TopicPartition, list[ConsumedRecord]]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


def _enabled(shutdown_timeout: float = 10.0) -> Settings:
    return Settings(
        triage_consumer_enabled=True, consumer_shutdown_timeout_seconds=shutdown_timeout
    )


def _log_lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


async def test_should_not_build_a_consumer_when_the_consumer_is_disabled() -> None:
    built: list[FakeConsumer] = []

    def factory() -> FakeConsumer:
        built.append(FakeConsumer())
        return built[-1]

    async with serve_app(create_app(probes=[], consumer_factory=factory)) as client:
        assert (await client.get("/health")).status_code == 200

    assert built == []


async def test_should_start_the_consumer_with_the_app_and_stop_it_on_shutdown() -> None:
    fake = FakeConsumer()

    async with serve_app(create_app(_enabled(), probes=[], consumer_factory=lambda: fake)):
        await wait_until(lambda: fake.started)
        assert not fake.stopped

    assert fake.stopped


async def test_should_cancel_the_consumer_when_it_does_not_stop_within_the_timeout() -> None:
    fake = StuckConsumer()
    app = create_app(_enabled(shutdown_timeout=0.05), probes=[], consumer_factory=lambda: fake)
    stream = io.StringIO()
    configure_logging("INFO", stream)

    async with serve_app(app):
        await wait_until(lambda: fake.started)

    assert fake.stopped
    assert "triage_consumer_cancelled" in [line["event"] for line in _log_lines(stream)]


async def test_should_log_the_error_class_only_when_the_consumer_cannot_connect() -> None:
    fake = FakeConsumer(start_error=ConnectionRefusedError("kafka://ai:s3cret@broker refused"))
    app = create_app(_enabled(), probes=[], consumer_factory=lambda: fake)
    stream = io.StringIO()
    configure_logging("INFO", stream)

    async with serve_app(app):
        await wait_until(lambda: fake.stopped)

    failure, *_ = [line for line in _log_lines(stream) if line["event"] == "triage_consumer_error"]
    assert failure["error"] == "ConnectionRefusedError"
    assert "s3cret" not in stream.getvalue()


async def test_should_build_aiokafka_consumers_of_the_three_failure_topics() -> None:
    consumer = main.kafka_consumer_factory(Settings())()

    assert isinstance(consumer, AIOKafkaConsumerAdapter)


async def test_should_log_the_error_class_only_when_the_consumer_task_crashed() -> None:
    async def crash() -> None:
        raise RuntimeError("postgresql://ai:s3cret@db refused")

    task = asyncio.create_task(crash())
    await asyncio.wait({task})
    stream = io.StringIO()
    configure_logging("INFO", stream)

    await main.stop_consumer(task, asyncio.Event(), timeout=1)

    (failure,) = [line for line in _log_lines(stream) if line["event"] == "triage_consumer_failed"]
    assert failure["error"] == "RuntimeError"
    assert "s3cret" not in stream.getvalue()


def _worker_enabled(shutdown_timeout: float = 10.0) -> Settings:
    return Settings(
        triage_worker_enabled=True,
        triage_settle_seconds=0,
        triage_poll_interval_seconds=0.01,
        outbox_poll_interval_seconds=0.01,
        consumer_shutdown_timeout_seconds=shutdown_timeout,
    )


async def test_should_not_start_the_worker_or_relay_when_the_worker_is_disabled() -> None:
    built: list[FakeProducer] = []

    def factory() -> FakeProducer:
        built.append(FakeProducer())
        return built[-1]

    async with serve_app(create_app(probes=[], producer_factory=factory)) as client:
        assert (await client.get("/health")).status_code == 200

    assert built == []


async def test_should_triage_and_publish_in_the_background_then_stop_both_on_shutdown() -> None:
    producer = FakeProducer()
    outbox = InMemoryOutboxStore()
    queue = InMemoryTriageCaseQueue(
        serializer=lambda result, case: _stored(outbox, to_outbox_message(result, case))
    )
    case = make_case(TriageCause.INSUFFICIENT_STOCK)
    queue.add(case, NOW - timedelta(hours=1))
    app = create_app(
        _worker_enabled(),
        probes=[],
        producer_factory=lambda: producer,
        triage_queue=queue,
        outbox_store=outbox,
    )

    async with serve_app(app):
        await wait_until(lambda: bool(producer.sent))
        running = (producer.started, producer.stopped)

    assert running == (True, False)
    assert producer.stopped
    assert queue.status(case.case_id) is TriageStatus.COMPLETED
    [record] = producer.sent
    assert record.key == str(case.order_id).encode()
    assert json.loads(record.value)["source"] == "FALLBACK_TEMPLATE"


def _stored(outbox: InMemoryOutboxStore, message: OutboxMessage) -> OutboxMessage:
    outbox.add(message, message.occurred_at)
    return message


async def test_should_keep_retrying_and_log_the_class_when_the_producer_cannot_start() -> None:
    producer = FakeProducer(start_error=ConnectionRefusedError("kafka://ai:s3cret@broker"))
    app = create_app(
        _worker_enabled(),
        probes=[],
        producer_factory=lambda: producer,
        triage_queue=InMemoryTriageCaseQueue(),
        outbox_store=InMemoryOutboxStore(),
    )
    stream = io.StringIO()
    configure_logging("INFO", stream)

    async with serve_app(app) as client:
        assert (await client.get("/health")).status_code == 200
        await wait_until(
            lambda: any(line["event"] == "outbox_relay_retry" for line in _log_lines(stream))
        )

    retries = [line for line in _log_lines(stream) if line["event"] == "outbox_relay_retry"]
    assert retries[0]["error"] == "ConnectionRefusedError"
    assert not [line for line in _log_lines(stream) if line["event"] == "outbox_relay_failed"]
    assert "s3cret" not in stream.getvalue()


async def test_should_build_aiokafka_producers() -> None:
    assert isinstance(main.kafka_producer_factory(Settings())(), AIOKafkaProducerAdapter)


async def test_should_build_the_llm_client_only_when_configured_and_close_it_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[AsyncAnthropic] = []
    real_build = build_anthropic_model

    def recording_build(settings: Settings) -> tuple[AnthropicTriageModel, AsyncAnthropic]:
        model, client = real_build(settings)
        built.append(client)
        return model, client

    monkeypatch.setattr(main, "build_anthropic_model", recording_build)

    async with serve_app(create_app(probes=[])) as client:
        assert (await client.get("/health")).status_code == 200
    assert built == []

    configured = Settings(anthropic_api_key=SecretStr("sk-test"), anthropic_model="claude-test")
    async with serve_app(create_app(configured, probes=[])) as client:
        assert (await client.get("/health")).status_code == 200
        [llm] = built
        assert not llm.is_closed()

    assert llm.is_closed()
