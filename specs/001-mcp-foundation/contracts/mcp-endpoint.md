# Contract: `/mcp` endpoint and tools

## HTTP

`POST /mcp/` — MCP Streamable HTTP, stateless, JSON responses (no `Mcp-Session-Id`).
Request headers: `Authorization: Bearer <jwt>`, `Content-Type: application/json`,
`Accept: application/json, text/event-stream`, optional `X-Request-Id`. Policy `x-roles: [CUSTOMER]`.

| Case | Status | Body |
|---|---|---|
| no / invalid token (any reason) | 401, `WWW-Authenticate: Bearer` | `{"detail":"Not authenticated"}` (identical for all) |
| no JWKS keys obtainable | 503 | `{"detail":"Service unavailable"}` |
| valid token, no `CUSTOMER` role | 403 | `{"detail":"Forbidden"}` |
| Orders answered 401 / `UNAUTHENTICATED` during a call | 401 | as above |
| otherwise | 200 | JSON-RPC response |

## Tools (`tools/list`)

Both tools: `readOnlyHint: true`, timeout 5 s, max upstream response 256 KiB.

### `get_order`
- Input: `{ "orderId": string (uuid) }`, required, no extra properties.
- Upstream: `POST /api/graphql`, `query GetOrder($orderId: ID!) { order(orderId: $orderId) {
  orderId customerId status totalAmount placedAt confirmedAt cancelledAt cancellationReason
  items { productId quantity unitPrice subtotal } } }`.
- Output (`structuredContent`): `ToolEnvelope<Order>` (see data-model.md).

### `list_my_orders`
- Input: `{ "limit"?: integer 1..50 = 20, "status"?: "PENDING"|"CONFIRMED"|"CANCELLED" }`.
- Upstream: `orders(customerId: <token customerId>, status, limit, offset: 0)` with
  `totalCount hasNextPage items { <Order fields> }`.
- Output: `ToolEnvelope<OrdersPage>`.

## Tool errors

`CallToolResult` with `isError: true`, `structuredContent: {"error": code, "message": text}`:

| code | message |
|---|---|
| `not_found` | `order not found` |
| `not_allowed` | `not allowed` |
| `invalid_input` | `invalid input` |
| `upstream_error` | `orders service unavailable` |

Unknown tool name → JSON-RPC error `-32602`, nothing forwarded. Schemas are snapshotted in
`tests/contract/snapshots/tools.json`; any change fails the contract test.
