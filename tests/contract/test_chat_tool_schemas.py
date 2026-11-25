"""Chat-only tool contracts: ``cancel_order`` against the documented Orders cancel endpoint.

``cancel_order`` is never served on ``/mcp``, so ``snapshots/tools.json`` does not cover it.
``snapshots/chat_tools.json`` pins what the chat offers and enforces (description, input and
output schema, mutating, exposure, allowlist entry, approval target); regenerate it on purpose
with ``UPDATE_SNAPSHOTS=1``. ``orders_cancel.json`` is a vendored copy of the endpoint as
ecommerce-orders documents it (source commit inside): the allowlisted path, the input and every
documented answer are checked against the tool (constitution II, IX).
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Final
from uuid import UUID

import httpx
import pytest
import respx
from pydantic import SecretStr, TypeAdapter

from ecommerce_ai.infrastructure.caller import CallerIdentity, RequestCaller, bind_caller
from ecommerce_ai.infrastructure.rest_client import RestClient
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.infrastructure.upstream import UpstreamTransport
from ecommerce_ai.tools.envelope import ToolEnvelope, ToolErrorCode, ToolFailure
from ecommerce_ai.tools.mutating.cancel_order import (
    CancelledOrder,
    CancelOrderInput,
    cancel_order_tool,
)
from ecommerce_ai.tools.orders import OrderStatus
from ecommerce_ai.tools.registry import ALLOWLIST, AnyToolDefinition

HERE: Final = Path(__file__).parent
SNAPSHOT: Final = HERE / "snapshots" / "chat_tools.json"
DOCUMENTED: Final[dict[str, object]] = json.loads(
    (HERE / "orders_cancel.json").read_text(encoding="utf-8")
)
_BODIES: Final = TypeAdapter(dict[str, dict[str, object]])
RESPONSES: Final = _BODIES.validate_python(DOCUMENTED["responses"])
"""Documented answer bodies by HTTP status."""

SETTINGS: Final = Settings()
ORDER_ID: Final = UUID("8107b8fe-b0ca-497d-8f41-e55010fbfa4d")
"""The order id of the documented 200 example."""


def _tool(http: httpx.AsyncClient) -> AnyToolDefinition:
    transport = UpstreamTransport(http, "orders", max_retries=0, backoff_base=0.01)
    return cancel_order_tool(
        RestClient(transport, SETTINGS.orders_base_url),
        timeout=SETTINGS.tool_timeout_seconds,
        max_bytes=SETTINGS.tool_max_response_bytes,
        ttl_seconds=SETTINGS.approval_ttl_seconds,
        proposals_per_hour=SETTINGS.cancel_proposals_per_hour,
    )


def _contract(tool: AnyToolDefinition) -> dict[str, object]:
    assert tool.input_model is not None
    assert tool.output_model is not None
    assert tool.allowlist_entry is not None
    assert tool.approval is not None
    entry = tool.allowlist_entry
    return {
        "description": tool.description,
        "inputSchema": tool.input_model.model_json_schema(by_alias=True),
        "outputSchema": tool.output_model.model_json_schema(by_alias=True),
        "mutating": tool.mutating,
        "readOnly": tool.read_only,
        "exposure": sorted(tool.exposure),
        "allowlistEntry": [entry.service, entry.method, entry.path, entry.operation],
        "approvalTarget": tool.approval.target_argument,
    }


@pytest.fixture(autouse=True)
def _caller() -> Iterator[None]:
    identity = CallerIdentity(subject="user-1", roles=frozenset({"CUSTOMER"}), customer_id=None)
    caller = RequestCaller(identity=identity, token=SecretStr("t"), request_id="req-1")
    with bind_caller(caller):
        yield


async def test_should_offer_the_snapshotted_cancel_order_contract() -> None:
    async with httpx.AsyncClient() as http:
        served = {"cancel_order": _contract(_tool(http))}

    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        text = json.dumps(served, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        SNAPSHOT.write_text(text, encoding="utf-8", newline="\n")
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    assert served == snapshot


async def test_should_allowlist_exactly_the_documented_route_through_the_gateway() -> None:
    async with httpx.AsyncClient() as http:
        entry = _tool(http).allowlist_entry

    path = str(DOCUMENTED["path"]).replace(":orderId", "{orderId}")
    assert entry is not None
    assert entry in ALLOWLIST
    assert (entry.method, entry.path) == (
        DOCUMENTED["method"],
        f"{DOCUMENTED['gatewayPrefix']}{path}",
    )
    assert SETTINGS.orders_base_url.endswith("/api/orders")


def test_should_take_exactly_the_documented_uuid_path_parameter() -> None:
    schema = CancelOrderInput.model_json_schema(by_alias=True)

    assert DOCUMENTED["pathParameters"] == {"orderId": "uuid"}
    assert schema["required"] == ["orderId"]
    assert set(schema["properties"]) == {"orderId"}
    assert schema["properties"]["orderId"]["format"] == "uuid"


def test_should_read_every_field_of_the_documented_success_answer() -> None:
    example = RESPONSES["200"]

    cancelled = CancelledOrder.model_validate(example)

    assert cancelled == CancelledOrder(order_id=ORDER_ID, status=OrderStatus.CANCELLED)
    aliases = {field.alias for field in CancelledOrder.model_fields.values()}
    assert set(example) == aliases


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("200", None),
        ("404", ToolErrorCode.NOT_FOUND),
        ("409", ToolErrorCode.CONFLICT),
        ("400", ToolErrorCode.UPSTREAM_ERROR),
    ],
)
async def test_should_map_every_documented_answer(
    status: str, expected: ToolErrorCode | None
) -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.post(f"{SETTINGS.orders_base_url}/{ORDER_ID}/cancel").respond(
            int(status), json=RESPONSES[status]
        )
        async with httpx.AsyncClient() as http:
            tool = _tool(http)
            if expected is None:
                result = await tool.handler(CancelOrderInput(order_id=ORDER_ID))
                assert result == ToolEnvelope[CancelledOrder](
                    data=CancelledOrder(order_id=ORDER_ID, status=OrderStatus.CANCELLED)
                )
            else:
                with pytest.raises(ToolFailure) as caught:
                    await tool.handler(CancelOrderInput(order_id=ORDER_ID))
                assert caught.value.code is expected

    assert route.call_count == 1
