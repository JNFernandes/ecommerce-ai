"""respx stub of the Inventory catalog behind the gateway.

``InventoryStub.products`` answers ``GET {INVENTORY_BASE_URL}/products`` and
``InventoryStub.product`` answers ``GET {INVENTORY_BASE_URL}/products/{id}`` for any id; both
record every request (headers, query, raw path).
"""

from collections.abc import Iterable, Mapping
from typing import Final

import respx

from tests.support.rest_stub import RestRoute

INVENTORY_BASE_URL: Final = "http://localhost:8090/api/inventory"
"""Default ``inventory_base_url`` setting."""

PRODUCTS_URL: Final = f"{INVENTORY_BASE_URL}/products"


def sample_product(**overrides: object) -> dict[str, object]:
    """Return an Inventory product; ``overrides`` replace fields."""
    product: dict[str, object] = {"id": "SKU-003", "name": "Desk Lamp", "availableQuantity": 12}
    product.update(overrides)
    return product


def sample_products(count: int, *, start: int = 1) -> list[dict[str, object]]:
    """Return ``count`` distinct products, ``SKU-<n>`` from ``start`` on."""
    return [
        sample_product(id=f"SKU-{n:03d}", name=f"Product {n}", availableQuantity=n % 7)
        for n in range(start, start + count)
    ]


def products_page(
    items: Iterable[Mapping[str, object]], *, total: int, limit: int = 20, offset: int = 0
) -> dict[str, object]:
    """Return an Inventory products page body."""
    return {
        "items": [dict(item) for item in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


class InventoryStub:
    """Programmable Inventory catalog on a respx router."""

    def __init__(self, router: respx.MockRouter, base_url: str = INVENTORY_BASE_URL) -> None:
        """Register the list route and the item route on ``router``."""
        self.products = RestRoute(router.get(f"{base_url}/products"))
        self.product = RestRoute(router.get(url__regex=rf"^{base_url}/products/[^?]+"))

    def respond_page(
        self,
        items: Iterable[Mapping[str, object]],
        *,
        total: int,
        limit: int = 20,
        offset: int = 0,
    ) -> None:
        """Answer the list route with one page."""
        self.products.respond_json(products_page(items, total=total, limit=limit, offset=offset))

    def respond_product(self, product: Mapping[str, object] | None = None) -> None:
        """Answer the item route with ``product`` (``sample_product()`` when omitted)."""
        self.product.respond_json(dict(product) if product is not None else sample_product())
