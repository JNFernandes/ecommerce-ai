"""Tool contracts: served schemas match the snapshot and queries match Orders' schema.

``snapshots/tools.json`` holds what ``tools/list`` serves for every tool (description, input and
output schema, annotations); any change fails here until the snapshot is regenerated on
purpose with ``UPDATE_SNAPSHOTS=1``. Each tool's GraphQL document is validated against the
vendored ``orders_schema.gql`` (source commit in its header).
"""

import json
import os
from pathlib import Path
from typing import Final

import pytest
import respx
from graphql import FieldNode, OperationDefinitionNode, build_schema, parse, validate

from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.main import create_app
from ecommerce_ai.tools.orders import (
    GET_ORDER,
    GET_ORDER_ENTRY,
    LIST_ORDERS,
    LIST_ORDERS_ENTRY,
)
from ecommerce_ai.tools.registry import AllowlistEntry
from tests.support.mcp import post_rpc, rpc_result, serve_app, tools_list_request
from tests.support.tokens import jwks, make_token

HERE: Final = Path(__file__).parent
SNAPSHOT: Final = HERE / "snapshots" / "tools.json"
ORDERS_SCHEMA: Final = build_schema((HERE / "orders_schema.gql").read_text(encoding="utf-8"))
JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default

DOCUMENTS: Final[dict[str, tuple[str, AllowlistEntry]]] = {
    "get_order": (GET_ORDER, GET_ORDER_ENTRY),
    "list_my_orders": (LIST_ORDERS, LIST_ORDERS_ENTRY),
}


async def _served_tools() -> dict[str, dict[str, object]]:
    """Return every tool as ``tools/list`` serves it over ``/mcp``, by name."""
    with respx.mock(assert_all_called=False) as router:
        router.get(JWKS_URL).respond(json=jwks())
        async with serve_app(create_app(probes=[])) as client:
            response = await post_rpc(client, tools_list_request(), make_token())
    tools = rpc_result(response)["tools"]
    assert isinstance(tools, list)
    keys = ("description", "inputSchema", "outputSchema", "annotations")
    return {tool["name"]: {key: tool.get(key) for key in keys} for tool in tools}


async def test_should_serve_the_snapshotted_tool_schemas() -> None:
    served = await _served_tools()

    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        text = json.dumps(served, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        SNAPSHOT.write_text(text, encoding="utf-8")
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    assert served == snapshot


async def test_should_describe_every_tool_and_input_property() -> None:
    served = await _served_tools()

    assert set(served) == set(DOCUMENTS)
    for name, tool in served.items():
        description = tool["description"]
        assert isinstance(description, str), name
        assert description.strip(), name
        schema = tool["inputSchema"]
        assert isinstance(schema, dict), name
        for prop, definition in schema.get("properties", {}).items():
            assert definition.get("description"), f"{name}.{prop}"
        annotations = tool["annotations"]
        assert isinstance(annotations, dict), name
        assert annotations["readOnlyHint"] is True, name


@pytest.mark.parametrize("name", DOCUMENTS)
def test_should_request_only_fields_and_arguments_that_exist_in_orders_schema(name: str) -> None:
    document, _ = DOCUMENTS[name]

    assert validate(ORDERS_SCHEMA, parse(document)) == []


@pytest.mark.parametrize("name", DOCUMENTS)
def test_should_query_exactly_the_allowlisted_operation(name: str) -> None:
    document, entry = DOCUMENTS[name]

    (operation,) = parse(document).definitions
    assert isinstance(operation, OperationDefinitionNode)
    assert operation.operation.value == "query"
    selections = operation.selection_set.selections
    assert all(isinstance(selection, FieldNode) for selection in selections)
    assert [
        selection.name.value for selection in selections if isinstance(selection, FieldNode)
    ] == [entry.operation]
