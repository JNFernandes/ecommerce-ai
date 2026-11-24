"""``get_order`` parses the caller's order, maps upstream failures and cuts long text."""

from collections.abc import Iterator, Mapping
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from ecommerce_ai.infrastructure.caller import (
    CallerIdentity,
    RequestCaller,
    bind_caller,
)
from ecommerce_ai.infrastructure.upstream import Forbidden, Unauthenticated, UpstreamError
from ecommerce_ai.tools.envelope import ToolEnvelope, ToolErrorCode, ToolFailure
from ecommerce_ai.tools.orders import (
    GET_ORDER,
    GET_ORDER_ENTRY,
    GetOrderInput,
    Order,
    OrderStatus,
    get_order_tool,
)
from ecommerce_ai.tools.registry import ALLOWLIST, ToolDefinition
from ecommerce_ai.tools.server import RegistryTool
from tests.support.orders_stub import ORDER_ID, sample_order

LIMIT = 500


class FakeOrders:
    """Answers every call with ``data`` or raises ``error``; records each call."""

    def __init__(
        self, data: dict[str, object] | None = None, error: Exception | None = None
    ) -> None:
        self.calls: list[tuple[str, dict[str, object], float, int]] = []
        self._data = data or {}
        self._error = error

    async def execute(
        self, document: str, variables: Mapping[str, object], *, timeout: float, max_bytes: int
    ) -> dict[str, object]:
        self.calls.append((document, dict(variables), timeout, max_bytes))
        if self._error is not None:
            raise self._error
        return self._data


@pytest.fixture
def caller() -> Iterator[RequestCaller]:
    identity = CallerIdentity(
        subject="user-1",
        roles=frozenset({"CUSTOMER"}),
        customer_id=UUID("6f1c2b8e-4d3a-4f6e-9a7b-1c2d3e4f5a6b"),
    )
    with bind_caller(
        RequestCaller(identity=identity, token=SecretStr("t"), request_id="r")
    ) as bound:
        yield bound


def _tool(orders: FakeOrders) -> ToolDefinition[GetOrderInput, ToolEnvelope[Order]]:
    return get_order_tool(orders, timeout=5.0, max_bytes=4096, text_limit=LIMIT)


async def _run(orders: FakeOrders) -> ToolEnvelope[Order]:
    return await _tool(orders).handler(GetOrderInput(order_id=UUID(ORDER_ID)))


async def test_should_return_order_in_untrusted_envelope_when_found(
    caller: RequestCaller,
) -> None:
    orders = FakeOrders({"order": sample_order()})

    envelope = await _tool(orders).handler(GetOrderInput(order_id=UUID(ORDER_ID)))

    assert envelope.trust == "untrusted_data"
    assert envelope.source == "ecommerce-orders"
    assert envelope.truncated == []
    order = envelope.data
    assert order.order_id == UUID(ORDER_ID)
    assert order.status is OrderStatus.CONFIRMED
    assert order.items is not None
    assert order.items[0].product_id == "sku-1"
    assert order.placed_at is not None
    assert order.placed_at.utcoffset() is not None
    dumped = envelope.model_dump(mode="json", by_alias=True)
    assert dumped["data"]["orderId"] == ORDER_ID
    assert dumped["data"]["items"][0]["unitPrice"] == 29.99


async def test_should_send_fixed_document_with_order_id_and_limits(caller: RequestCaller) -> None:
    orders = FakeOrders({"order": sample_order()})

    await _run(orders)

    assert orders.calls == [(GET_ORDER, {"orderId": ORDER_ID}, 5.0, 4096)]


async def test_should_accept_null_fields_when_order_is_not_yet_projected(
    caller: RequestCaller,
) -> None:
    unprojected = sample_order(
        status="PENDING", totalAmount=None, placedAt=None, confirmedAt=None, items=None
    )

    envelope = await _tool(FakeOrders({"order": unprojected})).handler(
        GetOrderInput(order_id=UUID(ORDER_ID))
    )

    assert envelope.data.items is None
    assert envelope.data.total_amount is None
    assert envelope.data.placed_at is None


async def test_should_fail_not_found_when_order_is_null(caller: RequestCaller) -> None:
    with pytest.raises(ToolFailure) as failure:
        await _run(FakeOrders({"order": None}))

    assert failure.value.code is ToolErrorCode.NOT_FOUND


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (Forbidden("no"), ToolErrorCode.NOT_ALLOWED),
        (UpstreamError("down", retryable=True), ToolErrorCode.UPSTREAM_ERROR),
    ],
)
async def test_should_map_upstream_errors_to_fixed_codes(
    caller: RequestCaller, error: Exception, code: ToolErrorCode
) -> None:
    with pytest.raises(ToolFailure) as failure:
        await _run(FakeOrders(error=error))

    assert failure.value.code is code
    assert caller.upstream_unauthenticated is False


async def test_should_flag_caller_and_fail_generically_when_orders_rejects_token(
    caller: RequestCaller,
) -> None:
    with pytest.raises(ToolFailure) as failure:
        await _run(FakeOrders(error=Unauthenticated("401")))

    assert failure.value.code is ToolErrorCode.UPSTREAM_ERROR
    assert caller.upstream_unauthenticated is True


@pytest.mark.parametrize(
    "raw",
    [
        sample_order(status="SHIPPED"),
        sample_order(orderId="not-a-uuid"),
        sample_order(placedAt="2026-11-20T10:15:00"),
        "not an object",
    ],
    ids=["unknown status", "bad id", "naive datetime", "not an object"],
)
async def test_should_fail_upstream_error_when_order_shape_does_not_match(
    caller: RequestCaller, raw: object
) -> None:
    with pytest.raises(ToolFailure) as failure:
        await _run(FakeOrders({"order": raw}))

    assert failure.value.code is ToolErrorCode.UPSTREAM_ERROR


async def test_should_truncate_long_free_text_and_list_its_paths(caller: RequestCaller) -> None:
    long = "x" * (LIMIT + 10)
    raw = sample_order(
        cancellationReason=long,
        items=[
            {"productId": "sku-1", "quantity": 1, "unitPrice": 1.0, "subtotal": 1.0},
            {"productId": long, "quantity": 1, "unitPrice": 1.0, "subtotal": 1.0},
        ],
    )

    envelope = await _tool(FakeOrders({"order": raw})).handler(
        GetOrderInput(order_id=UUID(ORDER_ID))
    )

    assert envelope.truncated == ["data.items[1].productId", "data.cancellationReason"]
    assert envelope.data.cancellation_reason is not None
    assert len(envelope.data.cancellation_reason) == LIMIT
    assert envelope.data.cancellation_reason.endswith("…")
    assert envelope.data.items is not None
    assert envelope.data.items[0].product_id == "sku-1"
    assert len(envelope.data.items[1].product_id) == LIMIT


@pytest.mark.parametrize(
    "arguments",
    [{}, {"orderId": "not-a-uuid"}, {"orderId": ORDER_ID, "extra": 1}],
    ids=["missing", "non-uuid", "extra property"],
)
async def test_should_reject_invalid_input_before_calling_orders(
    caller: RequestCaller, arguments: dict[str, object]
) -> None:
    orders = FakeOrders({"order": sample_order()})

    with pytest.raises(ValidationError):
        await RegistryTool.from_definition(_tool(orders)).run(arguments)

    assert orders.calls == []


def test_should_call_an_allowlisted_operation_when_defined() -> None:
    definition = _tool(FakeOrders())

    assert definition.allowlist_entry == GET_ORDER_ENTRY
    assert GET_ORDER_ENTRY in ALLOWLIST
    assert definition.read_only is True
    assert definition.input_model is GetOrderInput
    assert GetOrderInput.model_json_schema()["required"] == ["orderId"]
