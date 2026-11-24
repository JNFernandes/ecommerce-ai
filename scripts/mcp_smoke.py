"""Manual smoke test of the running MCP endpoint against the local stack.

Lists the tools, calls ``list_my_orders``, ``list_products``, ``get_product`` (``SKU-001``, then
the missing ``SKU-999``, whose error is printed) and ``list_my_notifications`` and, given an order
id, ``get_order``, with the customer access token in ``TOKEN`` (never printed). ``MCP_URL``
overrides the default ``http://localhost:8002/mcp``. Each result is printed as indented JSON
under a one-line header.
Usage: ``uv run python scripts/mcp_smoke.py [ORDER_ID]``.
"""

import asyncio
import json
import os
import sys

from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import StreamableHttpTransport

DEFAULT_MCP_URL = "http://localhost:8002/mcp"

CALLS: list[tuple[str, dict[str, object]]] = [
    ("list_my_orders", {}),
    ("list_products", {}),
    ("get_product", {"productId": "SKU-001"}),
    ("get_product", {"productId": "SKU-999"}),
    ("list_my_notifications", {}),
]
"""Tools called after listing the tools, in order, with their arguments."""


def render(result: CallToolResult) -> str:
    """Return the result as indented JSON: structured content, else the text content."""
    if result.structured_content is not None:
        return json.dumps(result.structured_content, indent=2, ensure_ascii=False)
    text = "\n".join(getattr(block, "text", "") for block in result.content)
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        return json.dumps(text, indent=2, ensure_ascii=False)


async def smoke(url: str, token: str, order_id: str | None) -> None:
    """List the tools and call each once, printing a header and the JSON result per tool."""
    transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})
    calls = list(CALLS)
    if order_id is not None:
        calls.append(("get_order", {"orderId": order_id}))
    async with Client(transport) as client:
        tools = await client.list_tools()
        print("== tools/list")
        print(json.dumps([tool.name for tool in tools], indent=2))
        for name, arguments in calls:
            result = await client.call_tool(name, arguments, raise_on_error=False)
            outcome = "error" if result.is_error else "ok"
            print(f"\n== {name} {json.dumps(arguments)} ({outcome})")
            print(render(result))


def main() -> int:
    """Run the smoke test; return 2 when ``TOKEN`` is not set."""
    token = os.environ.get("TOKEN")
    if not token:
        print("Set the TOKEN environment variable first.", file=sys.stderr)
        return 2
    url = os.environ.get("MCP_URL", DEFAULT_MCP_URL)
    order_id = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(smoke(url, token, order_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
