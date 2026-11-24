"""``list_products`` and ``get_product`` read the Inventory catalog with the caller's token."""

from collections.abc import AsyncIterator, Iterator
from uuid import UUID

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError
from pydantic import SecretStr, ValidationError

from ecommerce_ai.infrastructure.caller import CallerIdentity, RequestCaller, bind_caller
from ecommerce_ai.infrastructure.rest_client import RestClient
from ecommerce_ai.infrastructure.upstream import UpstreamTransport
from ecommerce_ai.tools.envelope import ToolErrorCode, ToolFailure
from ecommerce_ai.tools.inventory import (
    GET_PRODUCT_ENTRY,
    LIST_PRODUCTS_ENTRY,
    GetProductInput,
    ListProductsInput,
    get_product_tool,
    list_products_tool,
)
from ecommerce_ai.tools.registry import ALLOWLIST
from ecommerce_ai.tools.server import RegistryTool
from tests.support.inventory_stub import (
    INVENTORY_BASE_URL,
    InventoryStub,
    sample_product,
    sample_products,
)

TOKEN = "caller-token"  # noqa: S105  # fake bearer value for header assertions
REQUEST_ID = "req-inventory-1"
LIMIT = 500


@pytest.fixture
def stub() -> Iterator[InventoryStub]:
    with respx.mock(assert_all_called=False) as router:
        yield InventoryStub(router)


@pytest.fixture
async def inventory() -> AsyncIterator[RestClient]:
    async with httpx.AsyncClient() as http:
        transport = UpstreamTransport(http, "inventory", max_retries=0, backoff_base=0.01)
        yield RestClient(transport, INVENTORY_BASE_URL)


@pytest.fixture(autouse=True)
def _caller() -> Iterator[None]:
    identity = CallerIdentity(
        subject="user-1",
        roles=frozenset({"CUSTOMER"}),
        customer_id=UUID("6f1c2b8e-4d3a-4f6e-9a7b-1c2d3e4f5a6b"),
    )
    caller = RequestCaller(identity=identity, token=SecretStr(TOKEN), request_id=REQUEST_ID)
    with bind_caller(caller):
        yield


def _list(inventory: RestClient) -> RegistryTool:
    return RegistryTool.from_definition(
        list_products_tool(inventory, timeout=5.0, max_bytes=65536, text_limit=LIMIT)
    )


def _get(inventory: RestClient) -> RegistryTool:
    return RegistryTool.from_definition(
        get_product_tool(inventory, timeout=5.0, max_bytes=65536, text_limit=LIMIT)
    )


async def _failure(tool: RegistryTool, arguments: dict[str, object]) -> ToolFailure:
    with pytest.raises(ToolError) as raised:
        await tool.run(arguments)
    cause = raised.value.__cause__
    assert isinstance(cause, ToolFailure)
    return cause


async def _call(tool: RegistryTool, arguments: dict[str, object]) -> dict[str, object]:
    result = await tool.run(arguments)
    content = result.structured_content
    assert isinstance(content, dict)
    return content


async def test_should_return_default_page_in_order_when_no_arguments(
    stub: InventoryStub, inventory: RestClient
) -> None:
    products = sample_products(20)
    stub.respond_page(products, total=42)

    content = await _call(_list(inventory), {})

    assert content["source"] == "inventory"
    assert content["trust"] == "untrusted_data"
    data = content["data"]
    assert isinstance(data, dict)
    assert [item["id"] for item in data["items"]] == [p["id"] for p in products]
    assert (data["total"], data["limit"], data["offset"]) == (42, 20, 0)
    assert stub.products.params() == {"limit": "20", "offset": "0"}


async def test_should_forward_limit_and_offset_when_given(
    stub: InventoryStub, inventory: RestClient
) -> None:
    stub.respond_page(sample_products(2, start=41), total=42, limit=50, offset=40)

    content = await _call(_list(inventory), {"limit": 50, "offset": 40})

    assert stub.products.params() == {"limit": "50", "offset": "40"}
    data = content["data"]
    assert isinstance(data, dict)
    assert len(data["items"]) == 2


async def test_should_return_empty_page_when_offset_is_past_the_end(
    stub: InventoryStub, inventory: RestClient
) -> None:
    stub.respond_page([], total=3, offset=40)

    content = await _call(_list(inventory), {"offset": 40})

    data = content["data"]
    assert isinstance(data, dict)
    assert data["items"] == []
    assert data["total"] == 3


@pytest.mark.parametrize(
    "arguments",
    [{"limit": 0}, {"limit": 51}, {"offset": -1}, {"limit": "many"}, {"customerId": "x"}],
)
async def test_should_reject_invalid_page_arguments_without_calling_inventory(
    stub: InventoryStub, inventory: RestClient, arguments: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        await _list(inventory).run(arguments)

    assert stub.products.requests == []


@pytest.mark.parametrize("product_id", ["", "x" * 65, "../admin", "SKU 1", "SKU-1?x=1"])
async def test_should_reject_unsafe_product_id_without_calling_inventory(
    stub: InventoryStub, inventory: RestClient, product_id: str
) -> None:
    with pytest.raises(ValidationError):
        await _get(inventory).run({"productId": product_id})

    assert stub.product.requests == []


async def test_should_return_product_and_keep_case_when_found(
    stub: InventoryStub, inventory: RestClient
) -> None:
    stub.respond_product(sample_product(id="SKU-003", availableQuantity=0))

    content = await _call(_get(inventory), {"productId": "SKU-003"})

    assert content["source"] == "inventory"
    assert content["data"] == {"id": "SKU-003", "name": "Desk Lamp", "availableQuantity": 0}
    (request,) = stub.product.requests
    assert request.url.raw_path == b"/api/inventory/products/SKU-003"


async def test_should_keep_lower_case_product_id_when_forwarding(
    stub: InventoryStub, inventory: RestClient
) -> None:
    stub.respond_product(sample_product(id="sku-003"))

    await _call(_get(inventory), {"productId": "sku-003"})

    assert stub.product.requests[0].url.raw_path.endswith(b"/sku-003")


async def test_should_fail_product_not_found_when_inventory_answers_404(
    stub: InventoryStub, inventory: RestClient
) -> None:
    stub.product.respond_status(404)

    failure = await _failure(_get(inventory), {"productId": "SKU-404"})

    assert failure.code is ToolErrorCode.NOT_FOUND
    assert failure.message == "product not found"


async def test_should_fail_inventory_unavailable_when_list_answers_404(
    stub: InventoryStub, inventory: RestClient
) -> None:
    stub.products.respond_status(404)

    failure = await _failure(_list(inventory), {})

    assert failure.code is ToolErrorCode.UPSTREAM_ERROR
    assert failure.message == "inventory unavailable"


async def test_should_forward_caller_token_and_request_id(
    stub: InventoryStub, inventory: RestClient
) -> None:
    stub.respond_page([], total=0)
    stub.respond_product()

    await _call(_list(inventory), {})
    await _call(_get(inventory), {"productId": "SKU-003"})

    for request in (*stub.products.requests, *stub.product.requests):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.headers["X-Request-Id"] == REQUEST_ID
        assert request.headers["Accept"] == "application/json"


async def test_should_truncate_long_product_name_and_flag_it(
    stub: InventoryStub, inventory: RestClient
) -> None:
    stub.respond_page([sample_product(name="n" * 600)], total=1)
    stub.respond_product(sample_product(name="n" * 600))

    listed = await _call(_list(inventory), {})
    got = await _call(_get(inventory), {"productId": "SKU-003"})

    assert listed["truncated"] == ["data.items[0].name"]
    assert got["truncated"] == ["data.name"]
    data = got["data"]
    assert isinstance(data, dict)
    assert len(data["name"]) == LIMIT


@pytest.mark.parametrize(
    "product",
    [
        {"id": "SKU-1", "name": "x"},
        {"id": "SKU-1", "name": "x", "availableQuantity": -1},
        {"id": "", "name": "x", "availableQuantity": 1},
    ],
)
async def test_should_fail_inventory_unavailable_when_product_is_malformed(
    stub: InventoryStub, inventory: RestClient, product: dict[str, object]
) -> None:
    stub.respond_product(product)

    failure = await _failure(_get(inventory), {"productId": "SKU-1"})

    assert failure.code is ToolErrorCode.UPSTREAM_ERROR
    assert failure.message == "inventory unavailable"


async def test_should_ignore_unknown_upstream_fields(
    stub: InventoryStub, inventory: RestClient
) -> None:
    stub.respond_product(sample_product(warehouse="internal"))

    content = await _call(_get(inventory), {"productId": "SKU-003"})

    data = content["data"]
    assert isinstance(data, dict)
    assert "warehouse" not in data


def test_should_declare_read_only_allowlisted_tools_with_schemas() -> None:
    client = RestClient.__new__(RestClient)
    listed = list_products_tool(client, timeout=5.0, max_bytes=1, text_limit=LIMIT)
    got = get_product_tool(client, timeout=5.0, max_bytes=1, text_limit=LIMIT)

    assert (listed.name, got.name) == ("list_products", "get_product")
    assert listed.allowlist_entry == LIST_PRODUCTS_ENTRY
    assert got.allowlist_entry == GET_PRODUCT_ENTRY
    assert {LIST_PRODUCTS_ENTRY, GET_PRODUCT_ENTRY} <= set(ALLOWLIST)
    assert listed.read_only and got.read_only
    assert listed.input_model is ListProductsInput
    assert GetProductInput.model_json_schema()["required"] == ["productId"]
