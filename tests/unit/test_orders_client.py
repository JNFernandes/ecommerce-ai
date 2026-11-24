"""The Orders client forwards the caller's token and classifies every upstream outcome."""

import json
from collections.abc import AsyncIterator, Iterator
from uuid import UUID

import httpx
import pytest
import respx
from pydantic import SecretStr

from ecommerce_ai.infrastructure.caller import CallerIdentity, RequestCaller, bind_caller
from ecommerce_ai.infrastructure.orders_client import OrdersClient
from ecommerce_ai.infrastructure.upstream import Forbidden, Unauthenticated, UpstreamError
from tests.support.orders_stub import ORDERS_GRAPHQL_URL, OrdersStub, sample_order

TOKEN = "header.payload.signature"  # noqa: S105  # fake, test-only
DOCUMENT = "query GetOrder($orderId: ID!) { order(orderId: $orderId) { orderId } }"
VARIABLES = {"orderId": "0b9a7c4e-2f1d-4e3b-8a6c-5d4e3f2a1b0c"}


async def _no_sleep(_delay: float) -> None:
    return None


@pytest.fixture
def stub() -> Iterator[OrdersStub]:
    with respx.mock(assert_all_called=False) as router:
        yield OrdersStub(router)


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture(autouse=True)
def _caller() -> Iterator[None]:
    identity = CallerIdentity(
        subject="user-1",
        roles=frozenset({"CUSTOMER"}),
        customer_id=UUID("6f1c2b8e-4d3a-4f6e-9a7b-1c2d3e4f5a6b"),
    )
    with bind_caller(RequestCaller(identity=identity, token=SecretStr(TOKEN), request_id="req-9")):
        yield


def _client(http: httpx.AsyncClient, *, max_retries: int = 0) -> OrdersClient:
    return OrdersClient(
        http, ORDERS_GRAPHQL_URL, max_retries=max_retries, backoff_base=0.01, sleep=_no_sleep
    )


async def _execute(
    http: httpx.AsyncClient, *, max_retries: int = 0, timeout: float = 5.0, max_bytes: int = 4096
) -> dict[str, object]:
    return await _client(http, max_retries=max_retries).execute(
        DOCUMENT, VARIABLES, timeout=timeout, max_bytes=max_bytes
    )


async def test_should_send_document_and_variables_when_executing(
    stub: OrdersStub, http: httpx.AsyncClient
) -> None:
    stub.respond_order(sample_order())

    data = await _execute(http)

    assert data == {"order": sample_order()}
    assert stub.request_body() == {"query": DOCUMENT, "variables": VARIABLES}


async def test_should_forward_caller_token_and_request_id_when_executing(
    stub: OrdersStub, http: httpx.AsyncClient
) -> None:
    stub.respond_order(sample_order())

    await _execute(http)

    request = stub.requests[-1]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["X-Request-Id"] == "req-9"


async def test_should_raise_unauthenticated_when_orders_answers_401(
    stub: OrdersStub, http: httpx.AsyncClient
) -> None:
    stub.respond_status(401)

    with pytest.raises(Unauthenticated):
        await _execute(http)


async def test_should_raise_unauthenticated_when_graphql_code_is_unauthenticated(
    stub: OrdersStub, http: httpx.AsyncClient
) -> None:
    stub.respond_graphql_error("UNAUTHENTICATED")

    with pytest.raises(Unauthenticated):
        await _execute(http)


async def test_should_raise_forbidden_when_orders_answers_403(
    stub: OrdersStub, http: httpx.AsyncClient
) -> None:
    stub.respond_status(403)

    with pytest.raises(Forbidden):
        await _execute(http)


async def test_should_raise_forbidden_when_graphql_code_is_forbidden(
    stub: OrdersStub, http: httpx.AsyncClient
) -> None:
    stub.respond_graphql_error("FORBIDDEN")

    with pytest.raises(Forbidden):
        await _execute(http)


async def test_should_check_errors_before_data_when_both_are_present(
    stub: OrdersStub, http: httpx.AsyncClient
) -> None:
    stub.respond_json(
        {
            "data": {"order": sample_order()},
            "errors": [{"message": "no", "extensions": {"code": "FORBIDDEN"}}],
        }
    )

    with pytest.raises(Forbidden):
        await _execute(http)


@pytest.mark.parametrize(
    ("configure", "retryable"),
    [
        pytest.param(lambda s: s.respond_status(500), True, id="http-500"),
        pytest.param(lambda s: s.respond_status(503), True, id="http-503"),
        pytest.param(lambda s: s.respond_graphql_error("SERVICE_UNAVAILABLE"), True, id="gql-503"),
        pytest.param(lambda s: s.respond_graphql_error("BAD_USER_INPUT"), False, id="bad-input"),
        pytest.param(lambda s: s.respond_graphql_error("INTERNAL"), False, id="other-code"),
        pytest.param(lambda s: s.respond_status(200, text="<html>"), False, id="non-json"),
        pytest.param(lambda s: s.respond_status(404), False, id="http-404"),
        pytest.param(lambda s: s.respond_json({"data": None}), False, id="no-data"),
        pytest.param(lambda s: s.route.respond(200, json=[1]), False, id="not-an-object"),
        pytest.param(
            lambda s: s.respond_json({"data": None, "errors": ["bare"]}), False, id="bare-error"
        ),
        pytest.param(lambda s: s.respond_json({"errors": "x"}), False, id="errors-not-list"),
        pytest.param(
            lambda s: s.route.mock(side_effect=httpx.ConnectError("refused")), True, id="connect"
        ),
        pytest.param(
            lambda s: s.route.mock(side_effect=httpx.ReadTimeout("slow")), True, id="httpx-timeout"
        ),
    ],
)
async def test_should_raise_upstream_error_with_retryable_flag_when_orders_fails(
    stub: OrdersStub,
    http: httpx.AsyncClient,
    configure: object,
    retryable: bool,
) -> None:
    assert callable(configure)
    configure(stub)

    with pytest.raises(UpstreamError) as caught:
        await _execute(http)

    assert caught.value.retryable is retryable


async def test_should_raise_retryable_upstream_error_when_orders_hangs_past_timeout(
    stub: OrdersStub, http: httpx.AsyncClient
) -> None:
    stub.respond_hang()

    with pytest.raises(UpstreamError) as caught:
        await _execute(http, timeout=0.05)

    assert caught.value.retryable is True


async def test_should_abort_stream_when_body_exceeds_max_bytes(
    stub: OrdersStub, http: httpx.AsyncClient
) -> None:
    stub.respond_oversize(8192)

    with pytest.raises(UpstreamError) as caught:
        await _execute(http, max_bytes=1024)

    assert caught.value.retryable is False


async def test_should_retry_then_succeed_when_failure_is_transient(
    stub: OrdersStub, http: httpx.AsyncClient
) -> None:
    stub.respond_sequence(
        httpx.Response(503),
        httpx.ConnectError("refused"),
        httpx.Response(200, json={"data": {"order": None}}),
    )

    data = await _execute(http, max_retries=2)

    assert data == {"order": None}
    assert len(stub.requests) == 3


async def test_should_not_retry_when_failure_is_permanent(
    stub: OrdersStub, http: httpx.AsyncClient
) -> None:
    stub.respond_graphql_error("BAD_USER_INPUT")

    with pytest.raises(UpstreamError):
        await _execute(http, max_retries=2)

    assert len(stub.requests) == 1


async def test_should_raise_upstream_error_when_budget_is_already_spent(
    stub: OrdersStub, http: httpx.AsyncClient
) -> None:
    stub.respond_order(None)

    with pytest.raises(UpstreamError):
        await _execute(http, timeout=0.0)

    assert stub.requests == []


async def test_should_keep_upstream_details_out_of_error_messages_when_failing(
    stub: OrdersStub, http: httpx.AsyncClient
) -> None:
    stub.respond_status(502, text="secret upstream detail")

    with pytest.raises(UpstreamError) as caught:
        await _execute(http)

    message = str(caught.value)
    assert "secret upstream detail" not in message
    assert ORDERS_GRAPHQL_URL not in message
    assert TOKEN not in json.dumps(message)
