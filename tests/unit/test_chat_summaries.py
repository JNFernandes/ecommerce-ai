"""``tool.result`` summaries are fixed phrases per tool and outcome, never upstream data."""

import pytest
from pydantic import JsonValue

from ecommerce_ai.agents.assistant.ports import ToolOutcome
from ecommerce_ai.agents.assistant.summaries import summarize
from tests.support.chat import envelope


def _outcome(
    payload: dict[str, JsonValue], *, ok: bool = True, outcome: str = "ok", refused: bool = False
) -> ToolOutcome:
    return ToolOutcome(
        ok=ok,
        payload=payload,
        outcome=outcome,
        refused=refused,
        upstream_unauthenticated=False,
        duration_ms=1.0,
    )


def _page(count: int) -> dict[str, JsonValue]:
    return envelope({"items": [{"id": str(i)} for i in range(count)], "total": count})


@pytest.mark.parametrize(
    ("name", "count", "summary"),
    [
        ("list_my_orders", 3, "3 orders found"),
        ("list_my_orders", 1, "1 order found"),
        ("list_my_orders", 0, "No orders found"),
        ("list_products", 2, "2 products found"),
        ("list_my_notifications", 0, "No notifications found"),
    ],
)
def test_should_count_the_items_of_a_list_tool(name: str, count: int, summary: str) -> None:
    assert summarize(name, _outcome(_page(count))) == summary


@pytest.mark.parametrize(
    ("name", "found", "missing", "down"),
    [
        ("get_order", "Order found", "Order not found", "Orders unavailable"),
        ("get_product", "Product found", "Product not found", "Products unavailable"),
    ],
)
def test_should_say_found_not_found_or_unavailable_for_a_lookup_tool(
    name: str, found: str, missing: str, down: str
) -> None:
    error: dict[str, JsonValue] = {"error": "x", "message": "y"}

    assert summarize(name, _outcome(envelope({"id": "secret-id"}))) == found
    assert summarize(name, _outcome(error, ok=False, outcome="not_found")) == missing
    assert summarize(name, _outcome(error, ok=False, outcome="upstream_error")) == down


def test_should_say_refused_for_unknown_tools_and_calls_that_never_ran() -> None:
    error: dict[str, JsonValue] = {"error": "invalid_input", "message": "invalid input"}

    assert summarize("cancel_order", _outcome(error, ok=False, refused=True)) == "Request refused"
    assert summarize("get_order", _outcome(error, ok=False, refused=True)) == "Request refused"


def test_should_say_unavailable_when_a_list_result_has_no_items() -> None:
    assert summarize("list_my_orders", _outcome(envelope({"weird": True}))) == "Orders unavailable"
    assert (
        summarize("list_my_orders", _outcome({}, ok=False, outcome="not_found"))
        == "Orders unavailable"
    )
