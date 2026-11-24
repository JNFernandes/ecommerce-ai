# Contract: new MCP tools on `POST /mcp`

Endpoint, auth (RS256, `CUSTOMER`), envelope and JSON-RPC shape unchanged from
[001 contract](../../001-mcp-foundation/contracts/mcp-endpoint.md). `tools/list` returns exactly:
`get_order`, `list_my_orders`, `list_products`, `get_product`, `list_my_notifications`, each with
input/output schema, timeout (5 s) and max size (256 KiB). Pinned in
`tests/contract/snapshots/tools.json`.

| Tool | Input | Success `data` |
|---|---|---|
| `list_products` | `limit?` 1–50 (20), `offset?` ≥ 0 (0) | `ProductsPage` |
| `get_product` | `productId` `^[A-Za-z0-9_-]{1,64}$` | `Product` |
| `list_my_notifications` | `limit?`, `offset?`, `orderId?` UUID | `NotificationsPage` |

## Error mapping

| Condition | Result |
|---|---|
| Invalid input (incl. unknown field) | `invalid_input`, no upstream call |
| Inventory 404 on `get_product` | `not_found` "product not found" |
| Upstream 403; token without `customerId` (notifications) | `not_allowed` |
| Upstream 401 | whole MCP call → HTTP 401 |
| Timeout, connection error, other 4xx, 5xx, non-JSON, problem body, schema mismatch, oversize | `upstream_error` "inventory unavailable" / "notifications unavailable" |
