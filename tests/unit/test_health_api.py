"""/health always answers; /ready reports dependencies without them affecting startup."""

import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from ecommerce_ai.api.route_policy import RoutePolicyError, assert_route_policies
from ecommerce_ai.infrastructure.health import DependencyProbe, DependencyStatus
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.main import create_app
from tests.support.kafka import FakeConsumer
from tests.support.mcp import serve_app
from tests.support.polling import poll_until
from tests.support.probes import FakeProbe

UP, DOWN = DependencyStatus.UP, DependencyStatus.DOWN


def _client(probes: Sequence[DependencyProbe]) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(probes=probes))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_should_answer_liveness_without_checking_any_dependency() -> None:
    async with _client([FakeProbe("database", critical=True, status=DOWN)]) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "UP"}


async def test_should_be_ready_when_every_dependency_is_up() -> None:
    probes = [FakeProbe("database", True, UP), FakeProbe("kafka", False, UP)]
    async with _client(probes) as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "UP",
        "dependencies": {"database": "UP", "kafka": "UP"},
        "consumer": None,
    }


async def test_should_stay_ready_when_only_kafka_is_down() -> None:
    probes = [FakeProbe("database", True, UP), FakeProbe("kafka", False, DOWN)]
    async with _client(probes) as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json()["dependencies"]["kafka"] == "DOWN"


async def test_should_answer_503_when_the_database_is_down() -> None:
    probes = [FakeProbe("database", True, DOWN), FakeProbe("kafka", False, UP)]
    async with _client(probes) as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "DOWN",
        "dependencies": {"database": "DOWN", "kafka": "UP"},
        "consumer": None,
    }


async def test_should_start_with_real_probes_and_no_dependency_running() -> None:
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.get("/health")

    assert response.status_code == 200


def test_should_refuse_a_route_without_an_access_declaration() -> None:
    app = FastAPI()

    @app.get("/undeclared")
    def undeclared() -> None:
        """Route that forgot to declare its access."""

    with pytest.raises(RoutePolicyError, match="GET /undeclared"):
        assert_route_policies(app)


async def test_should_allow_cors_only_for_configured_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173")
    transport = httpx.ASGITransport(app=create_app(probes=[]))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.get("/health", headers={"Origin": "http://localhost:5173"})
        foreign = await client.get("/health", headers={"Origin": "http://evil.example"})

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-origin" not in foreign.headers


type ReadyBody = dict[str, Any]
"""A decoded `/ready` body (JSON values are untyped)."""


def _consumer_app(fake: FakeConsumer, database: DependencyStatus = UP) -> FastAPI:
    return create_app(
        Settings(triage_consumer_enabled=True),
        probes=[FakeProbe("database", True, database), FakeProbe("kafka", False, DOWN)],
        consumer_factory=lambda: fake,
    )


async def _ready_body(
    client: httpx.AsyncClient, condition: Callable[[ReadyBody], bool]
) -> ReadyBody:
    async def fetch() -> ReadyBody:
        body: ReadyBody = (await client.get("/ready")).json()
        return body

    return await poll_until(fetch, condition)


async def test_should_report_the_consumer_as_connected_and_caught_up_when_it_runs() -> None:
    async with serve_app(_consumer_app(FakeConsumer())) as client:
        body = await _ready_body(client, lambda body: body["consumer"]["caughtUp"])

    assert body["consumer"] == {"connected": True, "caughtUp": True, "lastError": None}
    assert body["status"] == "UP"


async def test_should_stay_ready_and_name_the_error_when_the_consumer_cannot_connect() -> None:
    fake = FakeConsumer(start_error=ConnectionRefusedError("kafka://ai:s3cret@broker refused"))
    async with serve_app(_consumer_app(fake)) as client:
        body = await _ready_body(client, lambda body: body["consumer"]["lastError"] is not None)
        response = await client.get("/ready")

    assert response.status_code == 200
    assert body["status"] == "UP"
    assert body["consumer"] == {
        "connected": False,
        "caughtUp": False,
        "lastError": "ConnectionRefusedError",
    }
    assert "s3cret" not in response.text


async def test_should_answer_503_when_the_database_is_down_even_if_the_consumer_runs() -> None:
    async with serve_app(_consumer_app(FakeConsumer(), database=DOWN)) as client:
        await _ready_body(client, lambda body: body["consumer"]["connected"])
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["consumer"]["connected"] is True


async def test_should_answer_health_and_ready_within_5_seconds_without_kafka_or_database() -> None:
    fake = FakeConsumer(start_error=ConnectionRefusedError("broker down"))
    started = time.perf_counter()

    async with serve_app(_consumer_app(fake, database=DOWN)) as client:
        health = await client.get("/health")
        ready = await client.get("/ready")

    assert (health.status_code, ready.status_code) == (200, 503)
    assert time.perf_counter() - started < 5
