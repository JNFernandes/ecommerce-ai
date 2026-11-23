"""JSON-RPC helpers to drive the stateless MCP endpoint with raw HTTP requests.

Requests use the stateless 2026-07-28 protocol: no ``initialize`` handshake, and every request
carries the ``_meta`` envelope (protocol version, client info and capabilities).
"""

from itertools import count
from typing import Final

import httpx
from mcp.types import LATEST_PROTOCOL_VERSION
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    PROTOCOL_VERSION_META_KEY,
)

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


def request_meta() -> dict[str, object]:
    """Return the per-request ``_meta`` envelope of the stateless protocol."""
    return {
        PROTOCOL_VERSION_META_KEY: LATEST_PROTOCOL_VERSION,
        CLIENT_INFO_META_KEY: {"name": "tests", "version": "0"},
        CLIENT_CAPABILITIES_META_KEY: {},
    }


def rpc_request(method: str, params: dict[str, object] | None = None) -> dict[str, object]:
    """Return a JSON-RPC 2.0 request with a fresh id and the ``_meta`` envelope."""
    return {
        "jsonrpc": "2.0",
        "id": next(_ids),
        "method": method,
        "params": {**(params or {}), "_meta": request_meta()},
    }


def discover_request() -> dict[str, object]:
    """Return a ``server/discover`` request (the stateless replacement of ``initialize``)."""
    return rpc_request("server/discover")


def tools_list_request() -> dict[str, object]:
    """Return a ``tools/list`` request."""
    return rpc_request("tools/list")


def tools_call_request(name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
    """Return a ``tools/call`` request."""
    return rpc_request("tools/call", {"name": name, "arguments": arguments or {}})


def routing_headers(request: dict[str, object]) -> dict[str, str]:
    """Return the ``Mcp-Method`` (and, for ``tools/call``, ``Mcp-Name``) headers of ``request``."""
    headers = {"Mcp-Method": str(request["method"])}
    params = request.get("params")
    if request["method"] == "tools/call" and isinstance(params, dict):
        headers["Mcp-Name"] = str(params["name"])
    return headers


async def post_rpc(
    client: httpx.AsyncClient,
    request: dict[str, object],
    token: str | None,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST one JSON-RPC request to the MCP endpoint with every header the protocol requires."""
    headers = {**mcp_headers(token), **routing_headers(request), **(extra_headers or {})}
    return await client.post(MCP_PATH, json=request, headers=headers)


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
