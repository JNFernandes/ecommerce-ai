"""The composition root starts without its dependencies and serves the configured port."""

import time
from collections.abc import Sequence

import httpx
import pytest
import respx
import uvicorn

from ecommerce_ai import main
from ecommerce_ai.infrastructure.health import DependencyStatus
from ecommerce_ai.infrastructure.orders_client import OrdersClient
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.main import create_app, run
from ecommerce_ai.tools.registry import AnyToolDefinition, ToolDefinition, ToolRegistryError
from tests.support.mcp import serve_app
from tests.support.probes import FakeProbe

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

    def invalid(_settings: Settings, _orders: OrdersClient) -> Sequence[AnyToolDefinition]:
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
