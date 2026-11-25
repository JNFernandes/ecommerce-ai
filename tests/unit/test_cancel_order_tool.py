"""``cancel_order`` posts one cancel with the caller's token and maps every Orders answer."""

from collections.abc import AsyncIterator, Iterator
from uuid import UUID

import httpx
import pytest
import respx
from pydantic import SecretStr, ValidationError

from ecommerce_ai.infrastructure.caller import CallerIdentity, RequestCaller, bind_caller
from ecommerce_ai.infrastructure.rest_client import RestClient
from ecommerce_ai.infrastructure.upstream import UpstreamTransport
from ecommerce_ai.tools.envelope import ToolErrorCode, ToolFailure
from ecommerce_ai.tools.mutating.cancel_order import (
    CANCEL_ORDER_ENTRY,
    CancelledOrder,
    CancelOrderInput,
    cancel_order_tool,
)
from ecommerce_ai.tools.mutating.policy import Exposure
from ecommerce_ai.tools.orders import OrderStatus
from ecommerce_ai.tools.registry import ALLOWLIST, AnyToolDefinition, ToolRegistry
from tests.support.orders_stub import ORDERS_BASE_URL, OrdersCancelStub

ORDER_ID = UUID("3f2a9c1e-7b4d-4e5f-8a6b-1c2d3e4f5a6b")
TOKEN = "caller-token"  # noqa: S105  # a test bearer value, not a secret


@pytest.fixture
def caller() -> Iterator[RequestCaller]:
    identity = CallerIdentity(
        subject="user-1",
        roles=frozenset({"CUSTOMER"}),
        customer_id=UUID("6f1c2b8e-4d3a-4f6e-9a7b-1c2d3e4f5a6b"),
    )
    bound = RequestCaller(identity=identity, token=SecretStr(TOKEN), request_id="req-1")
    with bind_caller(bound):
        yield bound


@pytest.fixture
def stub() -> Iterator[OrdersCancelStub]:
    with respx.mock(assert_all_called=False) as router:
        yield OrdersCancelStub(router)


@pytest.fixture
async def tool() -> AsyncIterator[AnyToolDefinition]:
    async with httpx.AsyncClient() as http:
        transport = UpstreamTransport(http, "orders", max_retries=2, backoff_base=0.001)
        yield cancel_order_tool(
            RestClient(transport, ORDERS_BASE_URL),
            timeout=0.2,
            max_bytes=4096,
            ttl_seconds=600,
            proposals_per_hour=5,
        )


async def _run(tool: AnyToolDefinition) -> object:
    return await tool.handler(CancelOrderInput(order_id=ORDER_ID))


def test_should_declare_a_mutating_chat_only_tool_with_its_policy(
    tool: AnyToolDefinition,
) -> None:
    assert tool.name == "cancel_order"
    assert (tool.mutating, tool.read_only) == (True, False)
    assert tool.exposure == frozenset({Exposure.CHAT})
    assert tool.approval is not None
    assert (tool.approval.target_argument, tool.approval.ttl_seconds) == ("orderId", 600)
    assert tool.approval.proposals_per_hour == 5
    assert tool.allowlist_entry == CANCEL_ORDER_ENTRY
    assert CANCEL_ORDER_ENTRY in ALLOWLIST
    assert len(ToolRegistry.build([tool]).exposed(Exposure.MCP)) == 0


def test_should_accept_only_a_uuid_order_id() -> None:
    assert CancelOrderInput.model_validate({"orderId": str(ORDER_ID)}).order_id == ORDER_ID
    for bad in ({"orderId": "../admin"}, {"orderId": str(ORDER_ID), "reason": "x"}, {}):
        with pytest.raises(ValidationError):
            CancelOrderInput.model_validate(bad)


async def test_should_post_one_cancel_with_the_caller_token_when_orders_accepts(
    tool: AnyToolDefinition, stub: OrdersCancelStub, caller: RequestCaller
) -> None:
    result = await _run(tool)

    assert result == tool.output_model(  # type: ignore[misc]  # the tool's own envelope model
        data=CancelledOrder(order_id=ORDER_ID, status=OrderStatus.CANCELLED)
    )
    assert stub.order_ids == [str(ORDER_ID)]
    request = stub.requests[0]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["X-Request-Id"] == "req-1"
    assert request.content == b""


@pytest.mark.parametrize(
    ("status", "code", "message"),
    [
        (404, ToolErrorCode.NOT_FOUND, "order not found"),
        (409, ToolErrorCode.CONFLICT, "order cannot be cancelled"),
        (403, ToolErrorCode.NOT_ALLOWED, "not allowed"),
        (400, ToolErrorCode.UPSTREAM_ERROR, "orders service unavailable"),
        (503, ToolErrorCode.UPSTREAM_ERROR, "orders service unavailable"),
    ],
)
async def test_should_map_an_orders_error_once_and_never_retry(
    tool: AnyToolDefinition,
    stub: OrdersCancelStub,
    caller: RequestCaller,
    status: int,
    code: ToolErrorCode,
    message: str,
) -> None:
    stub.respond_status(status)

    with pytest.raises(ToolFailure) as caught:
        await _run(tool)

    assert (caught.value.code, caught.value.message) == (code, message)
    assert stub.call_count == 1
    assert caller.upstream_unauthenticated is False


async def test_should_flag_the_caller_when_orders_rejects_the_token(
    tool: AnyToolDefinition, stub: OrdersCancelStub, caller: RequestCaller
) -> None:
    stub.respond_status(401)

    with pytest.raises(ToolFailure) as caught:
        await _run(tool)

    assert caught.value.code is ToolErrorCode.UPSTREAM_ERROR
    assert caller.upstream_unauthenticated is True


async def test_should_fail_without_retry_when_orders_does_not_answer_in_time(
    tool: AnyToolDefinition, stub: OrdersCancelStub, caller: RequestCaller
) -> None:
    stub.respond_hang()

    with pytest.raises(ToolFailure) as caught:
        await _run(tool)

    assert caught.value.code is ToolErrorCode.UPSTREAM_ERROR


@pytest.mark.parametrize(
    "body",
    [
        {"orderId": "not-a-uuid", "status": "CANCELLED"},
        {"orderId": "0b9a7c4e-2f1d-4e3b-8a6c-5d4e3f2a1b0c", "status": "CANCELLED"},
        {"orderId": "3f2a9c1e-7b4d-4e5f-8a6b-1c2d3e4f5a6b", "status": "PENDING"},
    ],
    ids=["malformed", "other-order", "not-cancelled"],
)
async def test_should_fail_when_orders_answers_something_else_than_this_cancelled_order(
    tool: AnyToolDefinition, stub: OrdersCancelStub, caller: RequestCaller, body: dict[str, str]
) -> None:
    stub.respond_json(body)

    with pytest.raises(ToolFailure) as caught:
        await _run(tool)

    assert (caught.value.code, caught.value.message) == (
        ToolErrorCode.UPSTREAM_ERROR,
        "orders service unavailable",
    )
