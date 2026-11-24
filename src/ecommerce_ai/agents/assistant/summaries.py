"""The fixed ``tool.result`` summary of each tool and outcome (contract: never upstream data).

A list tool reports how many items its page holds (``3 orders found``); a lookup tool reports
found / not found; any other failure is ``... unavailable``; a call that never ran (unknown tool,
invalid arguments) is refused. Only the item count is derived from the result, nothing else.
"""

from collections.abc import Mapping
from typing import Final, NamedTuple

from pydantic import JsonValue

from ecommerce_ai.agents.assistant.ports import ToolOutcome

REFUSED: Final = "Request refused"
NOT_FOUND_CODE: Final = "not_found"


class _Phrases(NamedTuple):
    noun: str
    """Singular noun of one item (``order``)."""
    plural: str
    """Plural noun (``orders``)."""
    service: str
    """Name used when the call failed (``Orders``)."""
    lists: bool
    """Whether the tool returns a page of items."""


_TOOLS: Final[Mapping[str, _Phrases]] = {
    "get_order": _Phrases("order", "orders", "Orders", lists=False),
    "list_my_orders": _Phrases("order", "orders", "Orders", lists=True),
    "get_product": _Phrases("product", "products", "Products", lists=False),
    "list_products": _Phrases("product", "products", "Products", lists=True),
    "list_my_notifications": _Phrases("notification", "notifications", "Notifications", lists=True),
}


def _item_count(payload: Mapping[str, JsonValue]) -> int | None:
    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    return len(items) if isinstance(items, list) else None


def summarize(name: str, outcome: ToolOutcome) -> str:
    """The fixed summary of tool ``name`` ending with ``outcome``."""
    phrases = _TOOLS.get(name)
    if outcome.refused or phrases is None:
        return REFUSED
    if not outcome.ok:
        if outcome.outcome == NOT_FOUND_CODE and not phrases.lists:
            return f"{phrases.noun.capitalize()} not found"
        return f"{phrases.service} unavailable"
    if not phrases.lists:
        return f"{phrases.noun.capitalize()} found"
    count = _item_count(outcome.payload)
    if count is None:
        return f"{phrases.service} unavailable"
    if count == 0:
        return f"No {phrases.plural} found"
    return f"{count} {phrases.noun if count == 1 else phrases.plural} found"
