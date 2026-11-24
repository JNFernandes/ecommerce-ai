"""The REST client encodes path segments and accepts only JSON objects."""

from collections.abc import AsyncIterator, Iterator
from uuid import UUID

import httpx
import pytest
import respx
from pydantic import SecretStr

from ecommerce_ai.infrastructure.caller import CallerIdentity, RequestCaller, bind_caller
from ecommerce_ai.infrastructure.rest_client import RestClient
from ecommerce_ai.infrastructure.upstream import UpstreamError, UpstreamTransport

BASE_URL = "http://localhost:8090/api/inventory"


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def client() -> AsyncIterator[RestClient]:
    async with httpx.AsyncClient() as http:
        transport = UpstreamTransport(http, "inventory", max_retries=0, backoff_base=0.01)
        yield RestClient(transport, f"{BASE_URL}/")


@pytest.fixture(autouse=True)
def _caller() -> Iterator[None]:
    identity = CallerIdentity(
        subject="user-1",
        roles=frozenset({"CUSTOMER"}),
        customer_id=UUID("6f1c2b8e-4d3a-4f6e-9a7b-1c2d3e4f5a6b"),
    )
    caller = RequestCaller(identity=identity, token=SecretStr("t"), request_id="req-1")
    with bind_caller(caller):
        yield


async def test_should_return_the_json_object_when_upstream_answers_one(
    router: respx.MockRouter, client: RestClient
) -> None:
    route = router.get(f"{BASE_URL}/products").respond(200, json={"items": [], "total": 0})

    data = await client.get_json(["products"], {"limit": 5}, timeout=5.0, max_bytes=4096)

    assert data == {"items": [], "total": 0}
    assert route.calls.last.request.url.params["limit"] == "5"


@pytest.mark.parametrize(
    ("segment", "encoded"),
    [("SKU-003", "SKU-003"), ("../admin", "..%2Fadmin"), ("a b?x=1", "a%20b%3Fx%3D1")],
)
def test_should_percent_encode_each_segment_when_building_the_url(
    client: RestClient, segment: str, encoded: str
) -> None:
    assert client.url_for(["products", segment]) == f"{BASE_URL}/products/{encoded}"


async def test_should_request_the_encoded_path_when_segment_has_reserved_characters(
    router: respx.MockRouter, client: RestClient
) -> None:
    route = router.get(f"{BASE_URL}/products/..%2Fadmin").respond(200, json={})

    await client.get_json(["products", "../admin"], timeout=5.0, max_bytes=4096)

    assert route.call_count == 1
    assert route.calls.last.request.url.raw_path == b"/api/inventory/products/..%2Fadmin"


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(200, text="<html>"), "inventory answered non-JSON"),
        (httpx.Response(200, json=[1, 2]), "inventory answered an unexpected shape"),
    ],
    ids=["non-json", "array"],
)
async def test_should_raise_upstream_error_when_body_is_not_a_json_object(
    router: respx.MockRouter, client: RestClient, response: httpx.Response, reason: str
) -> None:
    router.get(f"{BASE_URL}/products").mock(return_value=response)

    with pytest.raises(UpstreamError) as caught:
        await client.get_json(["products"], timeout=5.0, max_bytes=4096)

    assert str(caught.value) == reason
    assert caught.value.retryable is False
