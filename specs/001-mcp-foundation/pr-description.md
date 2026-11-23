# PR description draft: US-01

Implements US-01: MCP server foundation.

## Summary

- Stateless FastMCP server at `POST /mcp` (exact path, plain JSON, no session), declared
  `x-roles: [CUSTOMER]`; default deny now also covers ASGI routes and mounts.
- `BearerAuthMiddleware`: RS256 tokens verified locally against Identity's JWKS (lazy,
  single-flight cache, refetch on unknown `kid` once per cooldown). One identical 401 for every
  bad token, 503 without keys, 403 without `CUSTOMER`; an Orders 401 turns the call into 401.
- Tools `get_order` and `list_my_orders`: read-only, allowlisted (`order`, `orders` on
  `POST /api/graphql`), caller's own token forwarded (ADR 0001), 5 s budget with bounded
  jittered retries, 256 KiB body cap, free text cut to 500 chars, results in an
  `untrusted_data` envelope, fixed error codes.
- Contract tests: tool schemas snapshotted; GraphQL documents validated against a vendored copy
  of Orders' `docs/schema.gql` (`graphql-core`, dev only).
- Constitution amended to 1.1.0 (allowlist `order` + `orders`; eval suite deferred to the first
  LLM story, `TODO(EVAL_SUITE)`): **needs a second maintainer's approval**.

## Validation

- Quality gate green (ruff, format, mypy strict, pytest with coverage ≥ 80%).
- `quickstart.md` offline section: `uv run pytest -q tests/unit tests/contract` passes.
- Against the local stack: not run here (needs `ecommerce-infra` up and the gateway route below).

## Follow-up issue for ecommerce-infra

**Title**: Route `/api/mcp` to the ai service

**Body**:

> ecommerce-ai now serves an MCP endpoint at `POST /mcp` (US-01). Please add:
>
> 1. A gateway route `/api/mcp` → `http://ai:8002/mcp` (exact path, no trailing slash; the
>    service redirects `/mcp/`). Methods `POST` (and `DELETE`, answered by the MCP transport).
>    Responses are plain JSON (stateless, no SSE, no `Mcp-Session-Id`), so the **default
>    timeout** is enough; no streaming or long write timeout is needed. Forward the
>    `Authorization` and `X-Request-Id` headers unchanged; the service verifies the token itself.
> 2. `ORDERS_GRAPHQL_URL: http://gateway:8090/api/graphql` in the `ai` service environment
>    (the tools read Orders through GraphQL, next to the existing `ORDERS_BASE_URL`).
>
> Check: `curl -s localhost:8090/api/mcp -H "Authorization: Bearer $TOKEN"
> -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d
> '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'` lists `get_order` and `list_my_orders`;
> without the header it answers 401.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
