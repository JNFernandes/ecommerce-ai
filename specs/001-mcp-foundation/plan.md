# Implementation Plan: US-01 MCP server foundation

**Branch**: `feature/US-01-mcp-foundation` | **Date**: 2026-11-23 | **Spec**: [spec.md](spec.md)

## Summary

Mount a stateless FastMCP Streamable HTTP server at `/mcp` behind an RS256 bearer middleware
(local JWKS verification, `CUSTOMER` only). Two read-only tools, `get_order` and
`list_my_orders`, call Orders GraphQL through the gateway with the caller's own token, inside a
validated registry (allowlist, schemas, timeout, size cap) and an untrusted-data envelope.
Decisions and reasons: [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.12 (uv)
**Primary Dependencies**: FastAPI, fastmcp 4.2.5 (new), PyJWT[crypto], httpx, Pydantic v2, structlog
**Storage**: N/A
**Testing**: pytest + pytest-asyncio, respx, generated RSA test keys; new `tests/contract/`
**Target Platform**: Linux container, port 8002
**Project Type**: web service (single project)
**Performance Goals**: tool result or error within tool timeout (5 s) + 1 s (SC-003)
**Constraints**: no connection at startup; upstream body ≤ 256 KiB; free text ≤ 500 chars
**Scale/Scope**: 2 tools, 1 endpoint

## Constitution Check

| Principle | Status |
|---|---|
| I Layering | ✅ tools hold registry/tools, infrastructure holds JWKS/auth/Orders client, api holds mount + auth middleware; wiring in `main.py`. No prompts, no LLM. |
| II Security | ✅ RS256 local JWKS, identical 401, 403, 503; default deny extended to mounts; caller token passthrough only; allowlist enforced at startup. ⚠ Allowlist amendment: add the `order` query (MINOR 1.1.0, in this story). |
| III HITL | ✅ read-only tools only. |
| IV Reliability | ✅ per-call timeout, 2 bounded retries with jittered backoff, lazy clients. |
| VI Evals | ✅ deferred by constitution 1.1.0 (`TODO(EVAL_SUITE)`): no LLM yet; the first LLM story's suite covers these tools. |
| VII Observability | ✅ per-call log: tool, outcome, duration, `request_id`; redaction unchanged. |
| VIII/IX Tests | ✅ unit (happy + error per tool/component), contract tests for both tool schemas; ≥80% coverage. |
| XI Gate | ✅ ruff/mypy strict/pytest unchanged; CI unchanged (contract tests are offline). |

Post-design re-check: no new violations.

## Project Structure

```text
specs/001-mcp-foundation/  plan.md research.md data-model.md quickstart.md contracts/mcp-endpoint.md

src/ecommerce_ai/
├── main.py                    # build registry + MCP server + shared httpx client; lifespan
├── api/
│   ├── app.py                 # accepts the MCP ASGI app, chains its lifespan
│   ├── mcp.py                 # exact `/mcp` route (research 2) + BearerAuthMiddleware (401/403/503, upstream-401 rewrite)
│   └── route_policy.py        # also checks ASGI Route and Mount routes
├── tools/
│   ├── registry.py            # ToolDefinition, AllowlistEntry, ALLOWLIST, ToolRegistry.build()
│   ├── envelope.py            # ToolEnvelope, truncation, ToolErrorCode, ToolFailure
│   ├── orders.py              # get_order, list_my_orders, Order models, GraphQL documents
│   └── server.py              # build_mcp_server(): FastMCP + error/logging middleware
└── infrastructure/
    ├── settings.py            # + ORDERS_GRAPHQL_URL, TOOL_*, UPSTREAM_*, JWKS_* settings
    ├── jwks.py                # JwksCache
    ├── auth.py                # TokenVerifier → CallerIdentity
    ├── caller.py              # RequestCaller ContextVar
    ├── retry.py               # deadline-bounded retry with jittered backoff
    └── orders_client.py       # OrdersClient.execute, error classification

tests/
├── support/  tokens.py (RSA key, JWKS, token factory)  orders_stub.py (respx routes)  mcp.py (JSON-RPC helpers)
├── unit/     test_settings.py test_caller.py test_jwks.py test_auth.py test_retry.py test_orders_client.py
│             test_registry.py test_envelope.py test_mcp_server.py test_get_order.py test_list_my_orders.py
│             test_mcp_auth.py test_main.py test_mcp_tools_api.py test_route_policy.py
└── contract/ test_tool_schemas.py  snapshots/tools.json  orders_schema.gql (vendored, source commit noted)
```

**Structure Decision**: existing single-package layout; new modules only where the constitution
places them.

**Also in this story**: constitution MINOR 1.1.0 (allowlist `order` + `orders`; eval-suite
deferral `TODO(EVAL_SUITE)`), ADR index
unchanged, `README.md` / `CLAUDE.md` / `.env` docs for `/mcp` and new settings, follow-up issue in
ecommerce-infra for the `/api/mcp` gateway route (plain JSON, default timeout).

## Complexity Tracking

None. (The eval-suite gap is resolved by the constitution 1.1.0 deferral, not waived here.)
