"""``InvokerToolRunner``: the chat's tools are the registry's, run through ``ToolInvoker``."""

from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from ecommerce_ai.agents.assistant.tool_runner import InvokerToolRunner
from ecommerce_ai.infrastructure.caller import (
    CallerIdentity,
    RequestCaller,
    bind_caller,
    current_caller,
)
from ecommerce_ai.tools.invoker import ToolInvoker
from tests.unit.test_tool_invoker import REGISTRY, TOKEN, EchoInput


@pytest.fixture(autouse=True)
def caller() -> Iterator[RequestCaller]:
    identity = CallerIdentity(subject="user-1", roles=frozenset({"CUSTOMER"}), customer_id=None)
    with bind_caller(RequestCaller(identity, SecretStr(TOKEN), "req-1")) as bound:
        yield bound


def test_should_offer_every_registry_tool_with_its_input_schema() -> None:
    specs = InvokerToolRunner(ToolInvoker(REGISTRY)).tool_specs()

    assert [spec.name for spec in specs] == [definition.name for definition in REGISTRY]
    assert specs[0].description == "The echo tool."
    assert specs[0].input_schema == EchoInput.model_json_schema()


async def test_should_map_success_refusal_and_upstream_401_to_outcomes() -> None:
    runner = InvokerToolRunner(ToolInvoker(REGISTRY))

    ok = await runner.run("echo", {"text": "hi"})
    invalid = await runner.run("echo", {"text": "far too long text"})
    unknown = await runner.run("cancel_order", {})
    rejected = await runner.run("unauthenticated", {"text": "x"})

    assert (ok.ok, ok.outcome, ok.refused, ok.payload) == (True, "ok", False, {"text": "hi"})
    assert (invalid.ok, invalid.outcome, invalid.refused) == (False, "invalid_input", True)
    assert (unknown.outcome, unknown.refused) == ("unknown_tool", True)
    assert (rejected.ok, rejected.upstream_unauthenticated) == (False, True)
    assert TOKEN not in str(rejected.payload)
    assert current_caller().upstream_unauthenticated is True
