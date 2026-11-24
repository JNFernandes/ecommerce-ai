"""The registry rejects any tool definition with a gap, so startup fails (SC-006)."""

from dataclasses import replace

import pytest
from pydantic import BaseModel

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


def test_should_contain_only_read_operations_when_reading_the_allowlist() -> None:
    assert {(e.service, e.method, e.path, e.operation) for e in ALLOWLIST} == {
        ("ecommerce-orders", "POST", "/api/graphql", "order"),
        ("ecommerce-orders", "POST", "/api/graphql", "orders"),
        ("ecommerce-inventory", "GET", "/api/inventory/products", "listProducts"),
        ("ecommerce-inventory", "GET", "/api/inventory/products/{id}", "getProduct"),
        ("ecommerce-notifications", "GET", "/api/notifications", "listNotifications"),
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


def test_should_report_every_gap_when_several_definitions_are_invalid() -> None:
    first = replace(_definition("first"), input_model=None)
    second = replace(_definition("second"), timeout_seconds=0.0)

    with pytest.raises(ToolRegistryError) as caught:
        ToolRegistry.build([first, second])

    assert "'first' has no input model" in str(caught.value)
    assert "'second' needs a timeout" in str(caught.value)
