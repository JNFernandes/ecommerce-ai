"""The MCP server lists registry tools and maps their failures to fixed error results."""

import io
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import BaseModel, Field

from ecommerce_ai.api.middleware import RequestIdMiddleware
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.tools.envelope import ToolErrorCode, ToolFailure
from ecommerce_ai.tools.registry import (
    AllowlistEntry,
    AnyToolDefinition,
    ToolDefinition,
    ToolRegistry,
)
from ecommerce_ai.tools.server import build_mcp_app, build_mcp_server
from tests.support.mcp import (
    discover_request,
    post_rpc,
    rpc_error,
    rpc_result,
    tool_result,
    tools_call_request,
    tools_list_request,
)

TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl"  # noqa: S105  # fake, test-only
ENTRY = AllowlistEntry("ecommerce-orders", "POST", "/api/graphql", "order")


class EchoInput(BaseModel):
    text: str = Field(max_length=10, description="Text to echo.")


class EchoOutput(BaseModel):
    text: str


async def _echo(arguments: EchoInput) -> EchoOutput:
    return EchoOutput(text=arguments.text)


async def _not_found(_arguments: EchoInput) -> EchoOutput:
    raise ToolFailure(ToolErrorCode.NOT_FOUND)


async def _crash(_arguments: EchoInput) -> EchoOutput:
    raise RuntimeError("upstream secret detail")


def _tool(name: str, handler: object) -> AnyToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"The {name} tool.",
        allowlist_entry=ENTRY,
        input_model=EchoInput,
        output_model=EchoOutput,
        timeout_seconds=5.0,
        max_response_bytes=1024,
        handler=handler,  # type: ignore[arg-type]  # test handlers share one signature
    )


@pytest.fixture
def log_output() -> io.StringIO:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    return stream


@asynccontextmanager
async def _serve() -> AsyncIterator[httpx.AsyncClient]:
    """Client of the MCP app; its lifespan runs in the calling test's own task."""
    registry = ToolRegistry.build(
        [_tool("echo", _echo), _tool("not_found", _not_found), _tool("crash", _crash)]
    )
    mcp_app = build_mcp_app(build_mcp_server(registry))
    transport = httpx.ASGITransport(app=RequestIdMiddleware(mcp_app))
    async with (
        mcp_app.router.lifespan_context(mcp_app),
        httpx.AsyncClient(transport=transport, base_url="http://localhost") as client,
    ):
        yield client


async def _call(name: str, arguments: dict[str, object]) -> httpx.Response:
    async with _serve() as client:
        return await post_rpc(client, tools_call_request(name, arguments), TOKEN)


def _tool_calls(stream: io.StringIO) -> list[dict[str, object]]:
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    return [line for line in lines if line["event"] == "tool_call"]


async def test_should_answer_json_without_a_session_id_when_stateless() -> None:
    async with _serve() as client:
        response = await post_rpc(client, discover_request(), TOKEN)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in response.headers
    assert rpc_result(response)["supportedVersions"] == ["2026-07-28"]


async def test_should_list_registered_tools_as_read_only_when_listing() -> None:
    async with _serve() as client:
        response = await post_rpc(client, tools_list_request(), TOKEN)

    tools = rpc_result(response)["tools"]
    assert isinstance(tools, list)
    by_name = {tool["name"]: tool for tool in tools}
    assert set(by_name) == {"echo", "not_found", "crash"}
    echo = by_name["echo"]
    assert echo["description"] == "The echo tool."
    assert echo["annotations"]["readOnlyHint"] is True
    assert echo["inputSchema"]["properties"]["text"]["description"] == "Text to echo."
    assert echo["inputSchema"]["required"] == ["text"]
    assert echo["outputSchema"]["properties"]["text"]["type"] == "string"


async def test_should_return_structured_output_when_tool_succeeds() -> None:
    response = await _call("echo", {"text": "hi"})

    assert tool_result(response) == (False, {"text": "hi"})


async def test_should_return_fixed_error_result_when_tool_raises_failure() -> None:
    response = await _call("not_found", {"text": "x"})

    assert tool_result(response) == (True, {"error": "not_found", "message": "order not found"})


@pytest.mark.parametrize("arguments", [{}, {"text": "x" * 11}, {"text": 5}])
async def test_should_return_invalid_input_when_arguments_fail_validation(
    arguments: dict[str, object],
) -> None:
    response = await _call("echo", arguments)

    assert tool_result(response) == (True, {"error": "invalid_input", "message": "invalid input"})


async def test_should_return_generic_error_without_details_when_tool_crashes() -> None:
    response = await _call("crash", {"text": "x"})

    assert tool_result(response) == (
        True,
        {"error": "upstream_error", "message": "orders service unavailable"},
    )
    assert "upstream secret detail" not in response.text


async def test_should_answer_invalid_params_when_tool_is_unknown() -> None:
    response = await _call("cancel_order", {})

    assert rpc_error(response)["code"] == -32602


async def test_should_log_tool_outcome_duration_and_request_id_without_token_when_called(
    log_output: io.StringIO,
) -> None:
    async with _serve() as client:
        for name in ("echo", "not_found"):
            request = tools_call_request(name, {"text": "x"})
            await post_rpc(client, request, TOKEN, {"X-Request-Id": f"req-{name}"})

    calls = _tool_calls(log_output)
    assert [(c["tool"], c["outcome"], c["request_id"]) for c in calls] == [
        ("echo", "ok", "req-echo"),
        ("not_found", "not_found", "req-not_found"),
    ]
    assert all(isinstance(c["duration_ms"], float) for c in calls)
    assert TOKEN not in log_output.getvalue()
