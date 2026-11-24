"""Manual smoke test of the running MCP endpoint against the local stack.

Lists the tools, calls ``list_my_orders`` and, given an order id, ``get_order``, with the
customer access token in ``TOKEN``. ``MCP_URL`` overrides the default
``http://localhost:8002/mcp``. Usage: ``uv run python scripts/mcp_smoke.py [ORDER_ID]``.
"""

import asyncio
import os
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

DEFAULT_MCP_URL = "http://localhost:8002/mcp"


async def smoke(url: str, token: str, order_id: str | None) -> None:
    """List the tools and call them once, printing each result."""
    transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})
    async with Client(transport) as client:
        tools = await client.list_tools()
        print("Tools:", [tool.name for tool in tools])
        print("list_my_orders:", await client.call_tool("list_my_orders", {}))
        if order_id is not None:
            print("get_order:", await client.call_tool("get_order", {"orderId": order_id}))


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
