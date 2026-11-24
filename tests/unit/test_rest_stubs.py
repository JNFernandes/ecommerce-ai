"""Sanity checks for the Inventory and Notifications stubs, so the tool suites can trust them."""

import asyncio

import httpx
import pytest
import respx

from tests.support.inventory_stub import (
    INVENTORY_BASE_URL,
    PRODUCTS_URL,
    InventoryStub,
    sample_product,
    sample_products,
)
from tests.support.notifications_stub import (
    CUSTOMER_A,
    CUSTOMER_B,
    NOTIFICATIONS_URL,
    NotificationsStub,
    sample_notification,
)


async def test_should_answer_pages_and_record_params_when_inventory_stub_is_configured() -> None:
    with respx.mock(assert_all_called=False) as router:
        stub = InventoryStub(router)
        stub.respond_page(sample_products(3), total=3)
        stub.respond_product(sample_product(id="SKU-009"))
        async with httpx.AsyncClient() as client:
            page = await client.get(
                PRODUCTS_URL, params={"limit": 3}, headers={"X-Request-Id": "r"}
            )
            item = await client.get(f"{INVENTORY_BASE_URL}/products/SKU-009")

    assert [p["id"] for p in page.json()["items"]] == ["SKU-001", "SKU-002", "SKU-003"]
    assert page.json()["total"] == 3
    assert stub.products.params() == {"limit": "3"}
    assert stub.products.requests[0].headers["X-Request-Id"] == "r"
    assert item.json()["id"] == "SKU-009"
    assert stub.product.requests[0].url.path.endswith("/SKU-009")


async def test_should_answer_failures_when_inventory_stub_is_told_to_fail() -> None:
    with respx.mock(assert_all_called=False) as router:
        stub = InventoryStub(router)
        async with httpx.AsyncClient() as client:
            unconfigured = await client.get(PRODUCTS_URL)
            stub.products.respond_problem()
            problem = await client.get(PRODUCTS_URL)
            stub.products.respond_oversize(2048, {"items": []})
            oversize = await client.get(PRODUCTS_URL)
            stub.product.respond_status(404)
            missing = await client.get(f"{INVENTORY_BASE_URL}/products/SKU-404")
            stub.products.respond_sequence(httpx.Response(503), httpx.ConnectError("down"))
            sequence = await client.get(PRODUCTS_URL)
            with pytest.raises(httpx.ConnectError):
                await client.get(PRODUCTS_URL)
            stub.products.respond_hang()
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await client.get(PRODUCTS_URL)

    assert unconfigured.status_code == 500
    assert problem.headers["content-type"] == "application/problem+json"
    assert len(oversize.content) > 2048
    assert missing.status_code == 404
    assert sequence.status_code == 503


async def test_should_scope_answers_by_customer_when_notifications_stub_is_configured() -> None:
    with respx.mock(assert_all_called=False) as router:
        stub = NotificationsStub(router)
        stub.respond_by_customer(
            {
                CUSTOMER_A: [sample_notification()],
                CUSTOMER_B: [sample_notification(customerId=CUSTOMER_B, type="ORDER_CANCELLED")],
            }
        )
        async with httpx.AsyncClient() as client:
            a = await client.get(NOTIFICATIONS_URL, params={"customerId": CUSTOMER_A})
            b = await client.get(NOTIFICATIONS_URL, params={"customerId": CUSTOMER_B})
            stub.respond_page([], total=0, offset=40)
            empty = await client.get(NOTIFICATIONS_URL)

    assert [n["type"] for n in a.json()["items"]] == ["ORDER_CONFIRMED"]
    assert [n["type"] for n in b.json()["items"]] == ["ORDER_CANCELLED"]
    assert stub.notifications.params(0) == {"customerId": CUSTOMER_A}
    assert empty.json() == {"items": [], "total": 0, "limit": 20, "offset": 40}
