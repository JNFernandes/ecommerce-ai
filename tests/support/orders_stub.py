"""respx stubs of the Orders endpoints behind the gateway.

``OrdersStub`` answers ``POST {orders_graphql_url}`` with an order, a page, ``null``, a GraphQL
error code, an HTTP status, an oversized body or a request that never ends, and records every
request so tests can check the forwarded headers and variables. ``OrdersCancelStub`` answers
``POST {orders_base_url}/{orderId}/cancel`` (200 / 404 / 409 / hang) and counts the cancels.
"""

import asyncio
import json
import re
from collections.abc import Iterable, Mapping
from typing import Final
from urllib.parse import unquote

import httpx
import respx

ORDERS_GRAPHQL_URL: Final = "http://localhost:8090/api/graphql"
"""Default ``orders_graphql_url`` setting."""

ORDERS_BASE_URL: Final = "http://localhost:8090/api/orders"
"""Default ``orders_base_url`` setting."""

ORDER_ID: Final = "0b9a7c4e-2f1d-4e3b-8a6c-5d4e3f2a1b0c"
"""Order id used by ``sample_order``."""


def sample_order(**overrides: object) -> dict[str, object]:
    """Return an Orders GraphQL ``Order`` object; ``overrides`` replace fields."""
    order: dict[str, object] = {
        "orderId": ORDER_ID,
        "customerId": "6f1c2b8e-4d3a-4f6e-9a7b-1c2d3e4f5a6b",
        "status": "CONFIRMED",
        "totalAmount": 59.98,
        "placedAt": "2026-11-20T10:15:00Z",
        "confirmedAt": "2026-11-20T10:15:05Z",
        "cancelledAt": None,
        "cancellationReason": None,
        "items": [{"productId": "sku-1", "quantity": 2, "unitPrice": 29.99, "subtotal": 59.98}],
    }
    order.update(overrides)
    return order


def graphql_error(code: str, message: str = "upstream message") -> dict[str, object]:
    """Return a GraphQL error body (HTTP 200) with ``extensions.code``."""
    return {"data": None, "errors": [{"message": message, "extensions": {"code": code}}]}


class OrdersStub:
    """Programmable Orders GraphQL endpoint on a respx router."""

    def __init__(self, router: respx.MockRouter, url: str = ORDERS_GRAPHQL_URL) -> None:
        """Register the route on ``router``; it answers 500 until configured."""
        self.route = router.post(url)
        self.route.respond(500)

    @property
    def requests(self) -> list[httpx.Request]:
        """Return every request the endpoint received, in order."""
        return [call.request for call in self.route.calls]

    def request_body(self, index: int = -1) -> dict[str, object]:
        """Return the decoded JSON body of one received request."""
        body: dict[str, object] = json.loads(self.requests[index].content)
        return body

    def respond_json(self, body: Mapping[str, object], status: int = 200) -> None:
        """Answer every request with ``body``."""
        self.route.side_effect = None
        self.route.respond(status, json=dict(body))

    def respond_order(self, order: Mapping[str, object] | None) -> None:
        """Answer ``{"data": {"order": order}}`` (``None`` = not found)."""
        self.respond_json({"data": {"order": None if order is None else dict(order)}})

    def respond_page(
        self, orders: Iterable[Mapping[str, object]], *, total: int, has_next: bool
    ) -> None:
        """Answer an ``orders`` page."""
        page = {"items": [dict(o) for o in orders], "totalCount": total, "hasNextPage": has_next}
        self.respond_json({"data": {"orders": page}})

    def respond_graphql_error(self, code: str) -> None:
        """Answer HTTP 200 with a GraphQL error carrying ``code``."""
        self.respond_json(graphql_error(code))

    def respond_status(self, status: int, text: str = "upstream failure detail") -> None:
        """Answer every request with an HTTP status and a plain-text body."""
        self.route.side_effect = None
        self.route.respond(status, text=text)

    def respond_oversize(self, size: int) -> None:
        """Answer a valid-looking JSON body of at least ``size`` bytes."""
        padding = "x" * size
        self.respond_json({"data": {"order": sample_order(cancellationReason=padding)}})

    def respond_hang(self) -> None:
        """Never answer: the request waits until the caller's timeout cancels it."""

        async def _hang(_request: httpx.Request) -> httpx.Response:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

        self.route.side_effect = _hang

    def respond_sequence(self, *responses: httpx.Response | Exception) -> None:
        """Answer successive requests with ``responses`` (exceptions are raised)."""
        self.route.side_effect = list(responses)


class OrdersCancelStub:
    """Programmable ``POST {orders_base_url}/{orderId}/cancel``; records every cancel call.

    Answers 200 ``{orderId, status: CANCELLED}`` until configured otherwise. Bypass tests assert
    ``call_count == 0`` (SC-001).
    """

    def __init__(self, router: respx.MockRouter, base_url: str = ORDERS_BASE_URL) -> None:
        """Register the route for any order id under ``base_url``."""
        pattern = rf"^{re.escape(base_url.rstrip('/'))}/(?P<order_id>[^/]+)/cancel$"
        self.route = router.post(url__regex=pattern)
        self.respond_cancelled()

    @property
    def call_count(self) -> int:
        """Number of cancel requests received."""
        return self.route.call_count

    @property
    def requests(self) -> list[httpx.Request]:
        """Every cancel request, in order."""
        return [call.request for call in self.route.calls]

    @property
    def order_ids(self) -> list[str]:
        """The (decoded) order id of every cancel request, in order."""
        return [unquote(request.url.path.rsplit("/", 2)[-2]) for request in self.requests]

    def respond_cancelled(self) -> None:
        """Answer 200 with the cancelled order, echoing its id."""

        def _cancelled(_request: httpx.Request, order_id: str) -> httpx.Response:
            return httpx.Response(200, json={"orderId": unquote(order_id), "status": "CANCELLED"})

        self.route.side_effect = _cancelled

    def respond_status(self, status: int) -> None:
        """Answer every cancel with ``status`` (404 unknown or not owned, 409 not PENDING)."""
        self.route.side_effect = None
        self.route.respond(status, json={"statusCode": status, "message": "upstream detail"})

    def respond_json(self, body: Mapping[str, object]) -> None:
        """Answer 200 with ``body`` (e.g. a malformed one)."""
        self.route.side_effect = None
        self.route.respond(200, json=dict(body))

    def respond_hang(self) -> None:
        """Never answer: the cancel waits until the caller's timeout cancels it."""

        async def _hang(_request: httpx.Request, order_id: str) -> httpx.Response:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

        self.route.side_effect = _hang
