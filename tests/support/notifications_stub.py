"""respx stub of the Notifications API behind the gateway.

``NotificationsStub.notifications`` answers ``GET {NOTIFICATIONS_BASE_URL}/notifications`` and
records every request, so tests can check the forwarded token and the ``customerId`` scoping.
``respond_by_customer`` answers each customer only their own notifications.
"""

from collections.abc import Iterable, Mapping
from typing import Final

import httpx
import respx

from tests.support.rest_stub import RestRoute

NOTIFICATIONS_BASE_URL: Final = "http://localhost:8090/api"
"""Default ``notifications_base_url`` setting."""

NOTIFICATIONS_URL: Final = f"{NOTIFICATIONS_BASE_URL}/notifications"

CUSTOMER_A: Final = "6f1c2b8e-4d3a-4f6e-9a7b-1c2d3e4f5a6b"
CUSTOMER_B: Final = "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b"
ORDER_ID: Final = "0b9a7c4e-2f1d-4e3b-8a6c-5d4e3f2a1b0c"


def sample_notification(**overrides: object) -> dict[str, object]:
    """Return a Notifications item as the API sends it; ``overrides`` replace fields."""
    notification: dict[str, object] = {
        "id": "3c2b1a0f-9e8d-4c7b-a6f5-e4d3c2b1a0f9",
        "customerId": CUSTOMER_A,
        "orderId": ORDER_ID,
        "sourceEventId": "7a6b5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d",
        "type": "ORDER_CONFIRMED",
        "channel": "EMAIL",
        "status": "SENT",
        "message": "Your order was confirmed.",
        "createdAt": "2026-11-20T10:15:06Z",
    }
    notification.update(overrides)
    return notification


def notifications_page(
    items: Iterable[Mapping[str, object]], *, total: int, limit: int = 20, offset: int = 0
) -> dict[str, object]:
    """Return a Notifications page body."""
    return {
        "items": [dict(item) for item in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


class NotificationsStub:
    """Programmable Notifications API on a respx router."""

    def __init__(self, router: respx.MockRouter, base_url: str = NOTIFICATIONS_BASE_URL) -> None:
        """Register the list route on ``router``."""
        self.notifications = RestRoute(router.get(f"{base_url}/notifications"))

    def respond_page(
        self,
        items: Iterable[Mapping[str, object]],
        *,
        total: int,
        limit: int = 20,
        offset: int = 0,
    ) -> None:
        """Answer every request with one page."""
        self.notifications.respond_json(
            notifications_page(items, total=total, limit=limit, offset=offset)
        )

    def respond_by_customer(self, by_customer: Mapping[str, list[dict[str, object]]]) -> None:
        """Answer each request with the notifications of its ``customerId`` query parameter."""

        def _answer(request: httpx.Request) -> httpx.Response:
            items = by_customer.get(request.url.params.get("customerId", ""), [])
            return httpx.Response(200, json=notifications_page(items, total=len(items)))

        self.notifications.route.side_effect = _answer
