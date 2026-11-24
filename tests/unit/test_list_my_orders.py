"""``list_my_orders`` lists only the caller's orders, within bounds, as untrusted data (US2)."""

from collections.abc import Iterator, Mapping
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from ecommerce_ai.infrastructure.caller import CallerIdentity, RequestCaller, bind_caller
from ecommerce_ai.infrastructure.upstream import Forbidden
from ecommerce_ai.tools.envelope import ToolEnvelope, ToolErrorCode, ToolFailure
from ecommerce_ai.tools.orders import (
    LIST_ORDERS,
    LIST_ORDERS_ENTRY,
    ListMyOrdersInput,
    OrdersPage,
    OrderStatus,
    list_my_orders_tool,
)
from ecommerce_ai.tools.registry import ALLOWLIST, ToolDefinition
from ecommerce_ai.tools.server import RegistryTool
from tests.support.orders_stub import sample_order
from tests.support.tokens import CUSTOMER_ID

LIMIT = 500


class FakeOrders:
    """Answers every call with an ``orders`` page or raises ``error``; records each call."""

    def __init__(self, page: object = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._page = page if page is not None else _page([sample_order()])
        self._error = error

    async def execute(
        self, document: str, variables: Mapping[str, object], *, timeout: float, max_bytes: int
    ) -> dict[str, object]:
        self.calls.append((document, dict(variables)))
        if self._error is not None:
            raise self._error
        return {"orders": self._page}


def _page(
    orders: list[dict[str, object]], *, total: int | None = None, has_next: bool = False
) -> dict[str, object]:
    count = len(orders) if total is None else total
    return {"items": orders, "totalCount": count, "hasNextPage": has_next}


def _bind(customer_id: UUID | None) -> Iterator[RequestCaller]:
    identity = CallerIdentity(
        subject="user-1", roles=frozenset({"CUSTOMER"}), customer_id=customer_id
    )
    with bind_caller(
        RequestCaller(identity=identity, token=SecretStr("t"), request_id="r")
    ) as bound:
        yield bound


@pytest.fixture
def caller() -> Iterator[RequestCaller]:
    yield from _bind(CUSTOMER_ID)


@pytest.fixture
def caller_without_customer() -> Iterator[RequestCaller]:
    yield from _bind(None)


def _tool(orders: FakeOrders) -> ToolDefinition[ListMyOrdersInput, ToolEnvelope[OrdersPage]]:
    return list_my_orders_tool(orders, timeout=5.0, max_bytes=4096, text_limit=LIMIT)


async def _run(orders: FakeOrders, **arguments: object) -> ToolEnvelope[OrdersPage]:
    return await _tool(orders).handler(ListMyOrdersInput.model_validate(arguments))


async def test_should_omit_status_and_send_default_limit_when_no_status_is_given(
    caller: RequestCaller,
) -> None:
    orders = FakeOrders()

    await _run(orders)

    assert orders.calls == [
        (LIST_ORDERS, {"customerId": str(CUSTOMER_ID), "limit": 20, "offset": 0})
    ]


async def test_should_forward_status_and_max_limit_when_given(caller: RequestCaller) -> None:
    orders = FakeOrders()

    await _run(orders, limit=50, status="PENDING")

    ((_, variables),) = orders.calls
    assert variables["limit"] == 50
    assert variables["status"] == "PENDING"


async def test_should_parse_page_with_paging_info(caller: RequestCaller) -> None:
    page = _page([sample_order(), sample_order(status="PENDING", items=None)], total=7)
    page["hasNextPage"] = True

    envelope = await _run(FakeOrders(page))

    assert envelope.trust == "untrusted_data"
    assert envelope.data.total_count == 7
    assert envelope.data.has_next_page is True
    assert [order.status for order in envelope.data.items] == [
        OrderStatus.CONFIRMED,
        OrderStatus.PENDING,
    ]
    dumped = envelope.model_dump(mode="json", by_alias=True)
    assert set(dumped["data"]) == {"items", "totalCount", "hasNextPage"}


async def test_should_truncate_text_inside_listed_orders(caller: RequestCaller) -> None:
    long = "y" * (LIMIT + 1)
    items = [{"productId": long, "quantity": 1, "unitPrice": 1.0, "subtotal": 1.0}]
    page = _page([sample_order(), sample_order(cancellationReason=long, items=items)])

    envelope = await _run(FakeOrders(page))

    assert envelope.truncated == [
        "data.items[1].items[0].productId",
        "data.items[1].cancellationReason",
    ]
    assert envelope.data.items[1].cancellation_reason is not None
    assert len(envelope.data.items[1].cancellation_reason) == LIMIT


async def test_should_fail_not_allowed_without_calling_orders_when_token_has_no_customer_id(
    caller_without_customer: RequestCaller,
) -> None:
    orders = FakeOrders()

    with pytest.raises(ToolFailure) as failure:
        await _run(orders)

    assert failure.value.code is ToolErrorCode.NOT_ALLOWED
    assert orders.calls == []


async def test_should_fail_not_allowed_when_orders_forbids(caller: RequestCaller) -> None:
    with pytest.raises(ToolFailure) as failure:
        await _run(FakeOrders(error=Forbidden("no")))

    assert failure.value.code is ToolErrorCode.NOT_ALLOWED


@pytest.mark.parametrize(
    "page",
    ["not a page", {"items": [], "totalCount": -1, "hasNextPage": False}, {"items": [{}]}],
    ids=["not an object", "negative total", "bad item"],
)
async def test_should_fail_upstream_error_when_page_shape_does_not_match(
    caller: RequestCaller, page: object
) -> None:
    with pytest.raises(ToolFailure) as failure:
        await _run(FakeOrders(page))

    assert failure.value.code is ToolErrorCode.UPSTREAM_ERROR


@pytest.mark.parametrize(
    "arguments",
    [{"limit": 0}, {"limit": 51}, {"status": "SHIPPED"}, {"limit": "many"}, {"extra": True}],
    ids=["limit 0", "limit 51", "unknown status", "non-integer limit", "extra property"],
)
async def test_should_reject_invalid_input_before_calling_orders(
    caller: RequestCaller, arguments: dict[str, object]
) -> None:
    orders = FakeOrders()

    with pytest.raises(ValidationError):
        await RegistryTool.from_definition(_tool(orders)).run(arguments)

    assert orders.calls == []


def test_should_call_an_allowlisted_operation_when_defined() -> None:
    definition = _tool(FakeOrders())

    assert definition.allowlist_entry == LIST_ORDERS_ENTRY
    assert LIST_ORDERS_ENTRY in ALLOWLIST
    assert definition.read_only is True
    assert "required" not in ListMyOrdersInput.model_json_schema()
