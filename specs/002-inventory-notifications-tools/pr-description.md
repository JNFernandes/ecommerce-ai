# PR description draft: US-02

Implements US-02: MCP tools for Inventory and Notifications.

## Summary

- Three new read-only tools on `POST /mcp` (five in total): `list_products(limit, offset)`,
  `get_product(productId)` and `list_my_notifications(limit, offset, orderId?)`, all on the
  existing allowlist entries (no constitution change).
- `list_my_notifications` takes `customerId` only from the caller's token claim; a `customerId`
  argument is `invalid_input`, a token without the claim is `not_allowed` before any request.
- Shared upstream core: `UpstreamTransport` (caller's token, `X-Request-Id`, deadline-bounded
  retries, streamed size cap, status classification) under Orders GraphQL and the new
  `RestClient`; one error mapping (`tools/upstream_errors.py`); one `httpx.AsyncClient` per upstream.
- Results use per-service envelopes (`source`: `inventory`, `notifications`); the Orders tools'
  schemas are byte-identical in the snapshot.
- New settings `INVENTORY_BASE_URL`, `NOTIFICATIONS_BASE_URL`.

## Validation

- Quality gate green (ruff, format, mypy strict, pytest with coverage ≥ 80%).
- Isolation tests: every failure mode (hang, connection error, 4xx/5xx, non-JSON, problem body,
  schema mismatch, oversize) gives the generic "<service> unavailable" within timeout + 1 s, and
  Orders keeps answering while Inventory and Notifications are down or hanging.
- Against the local stack: not run here (needs `ecommerce-infra` up).

## Follow-up issue for ecommerce-infra

**Title**: Configure the Inventory and Notifications URLs for the ai service

**Body**:

> ecommerce-ai now serves `list_products`, `get_product` and `list_my_notifications` (US-02).
> Please add to the `ai` service environment:
>
> - `INVENTORY_BASE_URL: http://gateway:8090/api/inventory`
> - `NOTIFICATIONS_BASE_URL: http://gateway:8090/api`
>
> Both are read with the caller's own token (ADR 0001), like `ORDERS_GRAPHQL_URL`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
