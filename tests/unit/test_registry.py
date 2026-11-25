"""The registry rejects any tool definition with a gap, so startup fails (SC-006)."""

from dataclasses import replace

import pytest
from pydantic import BaseModel

from ecommerce_ai.tools.mutating.policy import ApprovalPolicy, Exposure
from ecommerce_ai.tools.registry import (
    ALLOWLIST,
    AllowlistEntry,
    AnyToolDefinition,
    ToolDefinition,
    ToolRegistry,
    ToolRegistryError,
)

ORDER_ENTRY = AllowlistEntry("ecommerce-orders", "POST", "/api/graphql", "order")


class EchoInput(BaseModel):
    text: str


class EchoOutput(BaseModel):
    text: str


async def _echo(arguments: EchoInput) -> EchoOutput:
    return EchoOutput(text=arguments.text)


def _definition(name: str = "echo") -> AnyToolDefinition:
    return ToolDefinition(
        name=name,
        description="Echo the text.",
        allowlist_entry=ORDER_ENTRY,
        input_model=EchoInput,
        output_model=EchoOutput,
        timeout_seconds=5.0,
        max_response_bytes=1024,
        handler=_echo,
    )


def test_should_contain_the_reads_and_only_the_order_cancel_when_reading_the_allowlist() -> None:
    assert {(e.service, e.method, e.path, e.operation) for e in ALLOWLIST} == {
        ("ecommerce-orders", "POST", "/api/graphql", "order"),
        ("ecommerce-orders", "POST", "/api/graphql", "orders"),
        ("ecommerce-inventory", "GET", "/api/inventory/products", "listProducts"),
        ("ecommerce-inventory", "GET", "/api/inventory/products/{id}", "getProduct"),
        ("ecommerce-notifications", "GET", "/api/notifications", "listNotifications"),
        ("ecommerce-orders", "POST", "/api/orders/{orderId}/cancel", "cancelOrder"),
    }


async def test_should_build_when_definition_is_valid() -> None:
    registry = ToolRegistry.build([_definition("a"), _definition("b")])

    assert [tool.name for tool in registry] == ["a", "b"]
    assert len(registry) == 2
    tool = registry.get("a")
    assert tool is not None
    assert tool.read_only is True
    assert await tool.handler(EchoInput(text="hi")) == EchoOutput(text="hi")
    assert registry.get("missing") is None


def test_should_build_empty_registry_when_no_tools_are_given() -> None:
    assert len(ToolRegistry.build([])) == 0


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        pytest.param({"allowlist_entry": None}, "no allowlist entry", id="missing-entry"),
        pytest.param(
            {"allowlist_entry": AllowlistEntry("ecommerce-orders", "POST", "/api/graphql", "x")},
            "non-allowlisted",
            id="entry-not-allowlisted",
        ),
        pytest.param({"input_model": None}, "no input model", id="missing-input"),
        pytest.param({"output_model": None}, "no output model", id="missing-output"),
        pytest.param({"timeout_seconds": 0.0}, "timeout", id="zero-timeout"),
        pytest.param({"timeout_seconds": -1.0}, "timeout", id="negative-timeout"),
        pytest.param({"max_response_bytes": 0}, "max response size", id="zero-size"),
        pytest.param({"max_response_bytes": -5}, "max response size", id="negative-size"),
        pytest.param({"name": " "}, "no name", id="blank-name"),
    ],
)
def test_should_raise_when_definition_has_a_gap(change: dict[str, object], problem: str) -> None:
    definition = replace(_definition(), **change)  # type: ignore[arg-type]  # per-case field

    with pytest.raises(ToolRegistryError, match=problem):
        ToolRegistry.build([definition])


def test_should_raise_when_tool_name_is_duplicated() -> None:
    with pytest.raises(ToolRegistryError, match="more than once"):
        ToolRegistry.build([_definition(), _definition()])


POLICY = ApprovalPolicy(target_argument="text", ttl_seconds=600, proposals_per_hour=5)


def _mutating(**changes: object) -> AnyToolDefinition:
    fields: dict[str, object] = {
        "mutating": True,
        "read_only": False,
        "approval": POLICY,
        "exposure": frozenset({Exposure.CHAT}),
    }
    return replace(_definition("mutate"), **(fields | changes))  # type: ignore[arg-type]  # fields


def test_should_default_a_tool_to_read_only_and_exposed_everywhere() -> None:
    tool = _definition()

    assert (tool.mutating, tool.approval) == (False, None)
    assert tool.exposure == frozenset({Exposure.MCP, Exposure.CHAT})


def test_should_build_a_mutating_tool_with_a_policy_and_chat_exposure() -> None:
    registry = ToolRegistry.build([_definition("read"), _mutating()])

    assert [tool.name for tool in registry.exposed(Exposure.MCP)] == ["read"]
    assert [tool.name for tool in registry.exposed(Exposure.CHAT)] == ["read", "mutate"]


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        pytest.param({"approval": None}, "without an approval policy", id="no-policy"),
        pytest.param(
            {"exposure": frozenset({Exposure.MCP, Exposure.CHAT})},
            "must not be exposed on MCP",
            id="on-mcp",
        ),
        pytest.param({"read_only": True}, "declared read-only", id="read-only"),
        pytest.param(
            {"approval": replace(POLICY, target_argument="orderId")},
            "'orderId' is not an input field",
            id="unknown-target",
        ),
        pytest.param({"approval": replace(POLICY, ttl_seconds=0)}, "TTL", id="zero-ttl"),
        pytest.param(
            {"approval": replace(POLICY, proposals_per_hour=0)},
            "one proposal per hour",
            id="zero-proposals",
        ),
        pytest.param({"exposure": frozenset()}, "exposed nowhere", id="no-exposure"),
    ],
)
def test_should_refuse_a_mutating_tool_when_its_declaration_has_a_gap(
    change: dict[str, object], problem: str
) -> None:
    with pytest.raises(ToolRegistryError, match=problem):
        ToolRegistry.build([_mutating(**change)])


def test_should_refuse_an_approval_policy_on_a_read_only_tool() -> None:
    with pytest.raises(ToolRegistryError, match="not mutating"):
        ToolRegistry.build([replace(_definition(), approval=POLICY)])


def test_should_report_every_gap_when_several_definitions_are_invalid() -> None:
    first = replace(_definition("first"), input_model=None)
    second = replace(_definition("second"), timeout_seconds=0.0)

    with pytest.raises(ToolRegistryError) as caught:
        ToolRegistry.build([first, second])

    assert "'first' has no input model" in str(caught.value)
    assert "'second' needs a timeout" in str(caught.value)
