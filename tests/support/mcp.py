"""JSON-RPC helpers to drive the stateless MCP endpoint with raw HTTP requests."""

from itertools import count
from typing import Final

import httpx
from mcp.types import LATEST_PROTOCOL_VERSION

MCP_PATH: Final = "/mcp"
"""Exact path of the MCP endpoint (no trailing slash)."""

_ids = count(1)


def mcp_headers(token: str | None) -> dict[str, str]:
    """Return the headers of a Streamable HTTP request, with a bearer token if given."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def rpc_request(method: str, params: dict[str, object] | None = None) -> dict[str, object]:
    """Return a JSON-RPC 2.0 request with a fresh id."""
    request: dict[str, object] = {"jsonrpc": "2.0", "id": next(_ids), "method": method}
    if params is not None:
        request["params"] = params
    return request


def initialize_request() -> dict[str, object]:
    """Return an ``initialize`` request."""
    return rpc_request(
        "initialize",
        {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "tests", "version": "0"},
        },
    )


def tools_list_request() -> dict[str, object]:
    """Return a ``tools/list`` request."""
    return rpc_request("tools/list")


def tools_call_request(name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
    """Return a ``tools/call`` request."""
    return rpc_request("tools/call", {"name": name, "arguments": arguments or {}})


async def post_rpc(
    client: httpx.AsyncClient, request: dict[str, object], token: str | None
) -> httpx.Response:
    """POST one JSON-RPC request to the MCP endpoint."""
    return await client.post(MCP_PATH, json=request, headers=mcp_headers(token))


def rpc_result(response: httpx.Response) -> dict[str, object]:
    """Return the ``result`` of a successful JSON-RPC response (fails the test otherwise)."""
    body = response.json()
    assert "error" not in body, body
    result: dict[str, object] = body["result"]
    return result


def rpc_error(response: httpx.Response) -> dict[str, object]:
    """Return the ``error`` object of a failed JSON-RPC response (fails the test otherwise)."""
    body = response.json()
    assert "error" in body, body
    error: dict[str, object] = body["error"]
    return error


def tool_result(response: httpx.Response) -> tuple[bool, dict[str, object]]:
    """Return ``(isError, structuredContent)`` of a ``tools/call`` response."""
    result = rpc_result(response)
    structured = result.get("structuredContent")
    assert isinstance(structured, dict), result
    return bool(result.get("isError", False)), structured
