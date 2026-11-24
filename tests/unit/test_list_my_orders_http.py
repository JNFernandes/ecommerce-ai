"""Regression: list_my_orders over the real Orders client, against respx with Orders' shapes."""

import json
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import respx
from pydantic import SecretStr

from ecommerce_ai.infrastructure.caller import CallerIdentity, RequestCaller, bind_caller
from ecommerce_ai.infrastructure.orders_client import OrdersClient
from ecommerce_ai.tools.envelope import ToolEnvelope, ToolErrorCode, ToolFailure
from ecommerce_ai.tools.orders import ListMyOrdersInput, OrdersPage, list_my_orders_tool
from tests.support.orders_stub import ORDERS_GRAPHQL_URL, sample_order
from tests.support.tokens import CUSTOMER_ID

TOKEN = "header.payload.signature"  # noqa: S105  # fake, test-only
SECOND_ORDER_ID = "1c8b6d5f-3e2a-4f4c-9b7d-6e5f4a3b2c1d"
TWO_ORDERS = [
    sample_order(),
    sample_order(orderId=SECOND_ORDER_ID, status="PENDING", confirmedAt=None),
]


def _orders_like(request: httpx.Request) -> httpx.Response:
    """Answer as Orders does: an explicit ``status: null`` filters on null and matches nothing."""
    variables = json.loads(request.content)["variables"]
    matching = [
        order
        for order in TWO_ORDERS
        if "status" not in variables or order["status"] == variables["status"]
    ]
    page = {"totalCount": len(matching), "hasNextPage": False, "items": matching}
    return httpx.Response(200, json={"data": {"orders": page}})


@pytest.fixture
def route() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=True) as router:
        yield router.post(ORDERS_GRAPHQL_URL)


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture(autouse=True)
def _caller() -> Iterator[None]:
    identity = CallerIdentity(
        subject="user-1", roles=frozenset({"CUSTOMER"}), customer_id=CUSTOMER_ID
    )
    with bind_caller(RequestCaller(identity=identity, token=SecretStr(TOKEN), request_id="req-1")):
        yield


async def _list(http: httpx.AsyncClient, **arguments: object) -> ToolEnvelope[OrdersPage]:
    client = OrdersClient(http, ORDERS_GRAPHQL_URL, max_retries=0, backoff_base=0.01)
    tool = list_my_orders_tool(client, timeout=5.0, max_bytes=65536, text_limit=500)
    return await tool.handler(ListMyOrdersInput.model_validate(arguments))


async def test_should_return_both_orders_when_orders_has_two_for_the_caller(
    route: respx.Route, http: httpx.AsyncClient
) -> None:
    route.side_effect = _orders_like

    envelope = await _list(http)

    assert envelope.data.total_count == 2
    assert [str(order.order_id) for order in envelope.data.items] == [
        TWO_ORDERS[0]["orderId"],
        SECOND_ORDER_ID,
    ]
    assert "status" not in json.loads(route.calls.last.request.content)["variables"]


async def test_should_filter_by_status_when_one_is_given(
    route: respx.Route, http: httpx.AsyncClient
) -> None:
    route.side_effect = _orders_like

    envelope = await _list(http, status="PENDING")

    assert [str(order.order_id) for order in envelope.data.items] == [SECOND_ORDER_ID]


async def test_should_fail_upstream_error_not_empty_page_when_orders_returns_graphql_errors(
    route: respx.Route, http: httpx.AsyncClient
) -> None:
    route.respond(
        200,
        json={
            "data": {"orders": {"totalCount": 0, "hasNextPage": False, "items": []}},
            "errors": [
                {
                    "message": "Variable '$status' got invalid value",
                    "extensions": {"code": "BAD_USER_INPUT"},
                }
            ],
        },
    )

    with pytest.raises(ToolFailure) as failure:
        await _list(http)

    assert failure.value.code is ToolErrorCode.UPSTREAM_ERROR
