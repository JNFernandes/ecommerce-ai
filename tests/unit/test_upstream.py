"""The shared upstream transport forwards the caller's token and classifies every outcome."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from uuid import UUID

import httpx
import pytest
import respx
from pydantic import SecretStr

from ecommerce_ai.infrastructure.caller import CallerIdentity, RequestCaller, bind_caller
from ecommerce_ai.infrastructure.upstream import (
    Forbidden,
    NotFound,
    Unauthenticated,
    UpstreamError,
    UpstreamTransport,
)

TOKEN = "header.payload.signature"  # noqa: S105  # fake, test-only
URL = "http://localhost:8090/api/inventory/products"


async def _no_sleep(_delay: float) -> None:
    return None


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


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
    with bind_caller(RequestCaller(identity=identity, token=SecretStr(TOKEN), request_id="req-7")):
        yield


async def _get(
    http: httpx.AsyncClient,
    *,
    max_retries: int = 0,
    timeout: float = 5.0,
    max_bytes: int = 4096,
    params: dict[str, str | int] | None = None,
) -> bytes:
    transport = UpstreamTransport(
        http, "inventory", max_retries=max_retries, backoff_base=0.01, sleep=_no_sleep
    )
    return await transport.request("GET", URL, params=params, timeout=timeout, max_bytes=max_bytes)


async def test_should_forward_caller_token_and_request_id_when_calling(
    router: respx.MockRouter, http: httpx.AsyncClient
) -> None:
    route = router.get(URL).respond(200, json={"items": []})

    body = await _get(http, params={"limit": 20, "offset": 0})

    assert body == b'{"items":[]}'
    request = route.calls.last.request
    assert request.headers["Authorization"].encode() == f"Bearer {TOKEN}".encode()
    assert request.headers["X-Request-Id"] == "req-7"
    assert request.headers["Accept"] == "application/json"
    assert dict(request.url.params) == {"limit": "20", "offset": "0"}


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, Unauthenticated), (403, Forbidden), (404, NotFound)],
)
async def test_should_raise_typed_error_without_retry_when_status_is_401_403_or_404(
    router: respx.MockRouter, http: httpx.AsyncClient, status: int, error_type: type[Exception]
) -> None:
    route = router.get(URL).respond(status)

    with pytest.raises(error_type):
        await _get(http, max_retries=2)

    assert route.call_count == 1


@pytest.mark.parametrize(
    ("response", "retryable"),
    [
        (httpx.Response(503), True),
        (httpx.Response(500), True),
        (httpx.Response(422), False),
        (httpx.Response(302), False),
        (
            httpx.Response(
                200, headers={"content-type": "application/problem+json"}, json={"title": "x"}
            ),
            False,
        ),
    ],
    ids=["503", "500", "422", "302", "problem-body"],
)
async def test_should_raise_upstream_error_with_retryable_flag_when_status_is_unusable(
    router: respx.MockRouter, http: httpx.AsyncClient, response: httpx.Response, retryable: bool
) -> None:
    router.get(URL).mock(return_value=response)

    with pytest.raises(UpstreamError) as caught:
        await _get(http)

    assert caught.value.retryable is retryable


async def test_should_retry_then_succeed_when_failures_are_transient(
    router: respx.MockRouter, http: httpx.AsyncClient
) -> None:
    route = router.get(URL)
    route.side_effect = [
        httpx.Response(503),
        httpx.ReadTimeout("slow"),
        httpx.Response(200, json={"ok": True}),
    ]

    body = await _get(http, max_retries=2)

    assert body == b'{"ok":true}'
    assert route.call_count == 3


async def test_should_not_retry_when_status_is_a_client_error(
    router: respx.MockRouter, http: httpx.AsyncClient
) -> None:
    route = router.get(URL).respond(400)

    with pytest.raises(UpstreamError):
        await _get(http, max_retries=2)

    assert route.call_count == 1


async def test_should_raise_retryable_error_when_upstream_is_unreachable(
    router: respx.MockRouter, http: httpx.AsyncClient
) -> None:
    router.get(URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(UpstreamError) as caught:
        await _get(http)

    assert caught.value.retryable is True
    assert str(caught.value) == "inventory unreachable"


async def test_should_time_out_when_upstream_hangs_past_the_deadline(
    router: respx.MockRouter, http: httpx.AsyncClient
) -> None:
    async def _hang(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    router.get(URL).mock(side_effect=_hang)

    with pytest.raises(UpstreamError) as caught:
        await _get(http, timeout=0.05)

    assert str(caught.value) == "inventory timed out"


async def test_should_send_nothing_when_the_budget_is_already_spent(
    router: respx.MockRouter, http: httpx.AsyncClient
) -> None:
    route = router.get(URL).respond(200, json={})

    with pytest.raises(UpstreamError) as caught:
        await _get(http, timeout=0.0)

    assert str(caught.value) == "inventory call budget spent"
    assert route.call_count == 0


async def test_should_abort_the_stream_when_the_body_exceeds_max_bytes(
    router: respx.MockRouter, http: httpx.AsyncClient
) -> None:
    router.get(URL).respond(200, json={"padding": "x" * 8192})

    with pytest.raises(UpstreamError) as caught:
        await _get(http, max_bytes=1024)

    assert caught.value.retryable is False
    assert str(caught.value) == "inventory response too large"


async def test_should_keep_upstream_details_out_of_error_reasons_when_failing(
    router: respx.MockRouter, http: httpx.AsyncClient
) -> None:
    router.get(URL).respond(502, text="secret upstream detail")

    with pytest.raises(UpstreamError) as caught:
        await _get(http)

    reason = str(caught.value)
    assert reason == "inventory answered 502"
    assert URL not in reason
    assert TOKEN not in reason
