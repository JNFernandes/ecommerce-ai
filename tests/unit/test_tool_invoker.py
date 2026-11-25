"""In-process tool invocation: refusals, failure mapping, timeout and the upstream 401 flag."""

import asyncio
import contextvars
import io
import json

import pytest
from pydantic import BaseModel, Field, SecretStr

from ecommerce_ai.infrastructure.caller import (
    CallerIdentity,
    RequestCaller,
    bind_caller,
    current_caller,
)
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.tools import invoker as invoker_module
from ecommerce_ai.tools.envelope import ToolErrorCode, ToolFailure
from ecommerce_ai.tools.invoker import ToolInvoker, failure_result
from ecommerce_ai.tools.registry import (
    AllowlistEntry,
    AnyToolDefinition,
    ToolDefinition,
    ToolRegistry,
)

TOKEN = "header.payload.signature"  # noqa: S105  # fake, test-only
ENTRY = AllowlistEntry("ecommerce-orders", "POST", "/api/graphql", "order")


class EchoInput(BaseModel):
    text: str = Field(max_length=10)


class EchoOutput(BaseModel):
    text: str


calls: list[str] = []


async def _echo(arguments: EchoInput) -> EchoOutput:
    calls.append(arguments.text)
    return EchoOutput(text=arguments.text)


async def _not_found(_arguments: EchoInput) -> EchoOutput:
    raise ToolFailure(ToolErrorCode.NOT_FOUND)


async def _crash(_arguments: EchoInput) -> EchoOutput:
    raise RuntimeError("upstream secret detail")


async def _hang(_arguments: EchoInput) -> EchoOutput:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")  # pragma: no cover


async def _unauthenticated(_arguments: EchoInput) -> EchoOutput:
    current_caller().upstream_unauthenticated = True
    raise ToolFailure(ToolErrorCode.UPSTREAM_ERROR, "orders service unavailable")


def _tool(name: str, handler: object, timeout: float = 5.0) -> AnyToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"The {name} tool.",
        allowlist_entry=ENTRY,
        input_model=EchoInput,
        output_model=EchoOutput,
        timeout_seconds=timeout,
        max_response_bytes=1024,
        handler=handler,  # type: ignore[arg-type]  # test handlers share one signature
    )


REGISTRY = ToolRegistry.build(
    [
        _tool("echo", _echo),
        _tool("not_found", _not_found),
        _tool("crash", _crash),
        _tool("hang", _hang, timeout=0.01),
        _tool("unauthenticated", _unauthenticated),
    ]
)


@pytest.fixture(autouse=True)
def caller() -> object:
    calls.clear()
    identity = CallerIdentity(subject="user-1", roles=frozenset({"CUSTOMER"}), customer_id=None)
    with bind_caller(RequestCaller(identity, SecretStr(TOKEN), "req-1")) as bound:
        yield bound


async def test_should_return_the_output_when_the_tool_succeeds() -> None:
    invocation = await ToolInvoker(REGISTRY).invoke("echo", {"text": "hi"})

    assert invocation.ok
    assert invocation.payload == {"text": "hi"}
    assert (invocation.outcome, invocation.executed, invocation.refused) == ("ok", True, False)
    assert not invocation.upstream_unauthenticated
    assert invocation.duration_ms >= 0


async def test_should_refuse_without_running_when_the_tool_is_unknown() -> None:
    invocation = await ToolInvoker(REGISTRY).invoke("cancel_order", {"orderId": "x"})

    assert not invocation.ok
    assert invocation.refused
    assert invocation.outcome == "unknown_tool"
    assert invocation.payload == {"error": "not_allowed", "message": "not allowed"}


async def test_should_refuse_a_registered_tool_when_its_operation_is_not_allowlisted() -> None:
    invocation = await ToolInvoker(REGISTRY, allowlist=frozenset()).invoke("echo", {"text": "x"})

    assert invocation.refused
    assert calls == []


@pytest.mark.parametrize("arguments", [{}, {"text": "x" * 11}, {"text": 5}])
async def test_should_refuse_without_running_when_the_arguments_are_invalid(
    arguments: dict[str, object],
) -> None:
    invocation = await ToolInvoker(REGISTRY).invoke("echo", arguments)

    assert invocation.refused
    assert invocation.payload == {"error": "invalid_input", "message": "invalid input"}
    assert calls == []


async def test_should_return_the_fixed_error_when_the_tool_fails() -> None:
    invocation = await ToolInvoker(REGISTRY).invoke("not_found", {"text": "x"})

    assert not invocation.ok
    assert invocation.executed
    assert invocation.outcome == "not_found"
    assert invocation.payload == {"error": "not_found", "message": "order not found"}


async def test_should_hide_the_details_when_the_tool_crashes() -> None:
    invocation = await ToolInvoker(REGISTRY).invoke("crash", {"text": "x"})

    assert invocation.outcome == "upstream_error"
    assert invocation.payload == {
        "error": "upstream_error",
        "message": "orders service unavailable",
    }
    assert "secret" not in json.dumps(invocation.payload)


async def test_should_fail_the_call_when_the_tool_outlives_its_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(invoker_module, "TIMEOUT_GRACE_SECONDS", 0.0)

    invocation = await ToolInvoker(REGISTRY).invoke("hang", {"text": "x"})

    assert invocation.executed
    assert invocation.outcome == "upstream_error"


async def test_should_flag_the_caller_when_upstream_rejects_the_token() -> None:
    invocation = await ToolInvoker(REGISTRY).invoke("unauthenticated", {"text": "x"})

    assert invocation.upstream_unauthenticated
    assert not invocation.ok


async def test_should_log_one_line_per_call_without_the_token_when_invoked() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    invoker = ToolInvoker(REGISTRY)

    await invoker.invoke("echo", {"text": "x"})
    await invoker.invoke("cancel_order", {})

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    tool_calls = [line for line in lines if line["event"] == "tool_call"]
    assert [(line["tool"], line["outcome"]) for line in tool_calls] == [
        ("echo", "ok"),
        ("cancel_order", "unknown_tool"),
    ]
    assert TOKEN not in stream.getvalue()


def test_should_expose_the_registry_when_asked_for_the_tool_list() -> None:
    assert ToolInvoker(REGISTRY).registry is REGISTRY


def test_should_map_known_failures_when_building_an_error_result() -> None:
    wrapped = RuntimeError("wrapper")
    wrapped.__cause__ = ToolFailure(ToolErrorCode.NOT_ALLOWED)

    assert failure_result(wrapped) is not None
    assert failure_result(TimeoutError()) is not None
    assert failure_result(RuntimeError("other")) is None


async def test_should_not_flag_upstream_when_no_caller_is_bound() -> None:
    task = asyncio.create_task(
        ToolInvoker(REGISTRY).invoke("not_found", {"text": "x"}), context=contextvars.Context()
    )

    invocation = await task

    assert invocation.executed
    assert not invocation.upstream_unauthenticated
