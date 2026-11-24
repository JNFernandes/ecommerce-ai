# Implementation Plan: US-02 MCP tools for Inventory and Notifications

**Branch**: `feature/US-02-inventory-notifications-tools` | **Date**: 2026-11-24 | **Spec**: [spec.md](spec.md)

## Summary

Add three read-only tools (`list_products`, `get_product`, `list_my_notifications`) on the US-01
registry. Extract the transport core of `OrdersClient` (token passthrough, `X-Request-Id`,
retry, streamed size cap, status classification) into a shared upstream module, add a small REST
`GET` client on it, and give each upstream its own `httpx.AsyncClient` for isolation.
Decisions: [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.12 (uv)
**Primary Dependencies**: unchanged (FastAPI, fastmcp, httpx, Pydantic v2, structlog); no new deps
**Storage**: N/A
**Testing**: pytest, respx; new stubs `tests/support/inventory_stub.py`, `notifications_stub.py`
**Target Platform**: Linux container, port 8002
**Project Type**: web service (single project)
**Performance Goals**: result or error within tool timeout (5 s) + 1 s (SC-003)
**Constraints**: nothing connects at startup; body ≤ 256 KiB; free text ≤ 500 chars; limit ≤ 50
**Scale/Scope**: +3 tools (5 total), 2 new upstreams, 2 new settings

## Constitution Check

| Principle | Status |
|---|---|
| I Layering | ✅ tools: models, validation, error mapping; infrastructure: HTTP clients; wiring in `main.py`. No prompts/LLM. |
| II Security | ✅ caller token passthrough only; paths already on the constitution allowlist (no amendment); `customerId` only from the caller's claim; `productId` path-safe regex. |
| III HITL | ✅ read-only. |
| IV Reliability | ✅ shared deadline-bounded retry; per-upstream client → one slow service can't exhaust another's pool. |
| VI Evals | ✅ deferred (`TODO(EVAL_SUITE)`, constitution 1.1.0). |
| VII Observability | ✅ existing per-call log middleware; no new fields with personal data. |
| VIII/IX Tests | ✅ unit per tool and client; contract snapshot regenerated to 5 tools; ≥ 80% coverage. |
| XI Gate | ✅ unchanged. |

Post-design re-check: no violations.

## Project Structure

```text
specs/002-inventory-notifications-tools/  plan.md research.md data-model.md quickstart.md contracts/mcp-tools.md

src/ecommerce_ai/
├── main.py                         # 3 AsyncClients; Inventory/Notifications clients; 5 tool definitions
├── tools/
│   ├── registry.py                 # ALLOWLIST + 3 entries
│   ├── upstream_errors.py          # shared exception → ToolFailure mapping (extracted from orders.py)
│   ├── orders.py                   # uses the shared mapping; behaviour unchanged
│   ├── inventory.py                # list_products, get_product, Product models
│   └── notifications.py            # list_my_notifications, Notification models
└── infrastructure/
    ├── settings.py                 # + INVENTORY_BASE_URL, NOTIFICATIONS_BASE_URL
    ├── upstream.py                 # Unauthenticated/Forbidden/NotFound/UpstreamError, UpstreamTransport
    ├── orders_client.py            # on UpstreamTransport (POST + GraphQL parse)
    └── rest_client.py              # RestClient.get_json(path, params) on UpstreamTransport

tests/
├── support/   inventory_stub.py notifications_stub.py (respx)
├── unit/      test_upstream.py test_rest_client.py test_inventory_tools.py
│              test_notifications_tool.py test_tool_isolation.py (+ settings/main updates)
└── contract/  snapshots/tools.json regenerated (UPDATE_SNAPSHOTS=1)
```

**Structure Decision**: existing layout; the refactor keeps `orders_client` import paths for
exceptions via re-export only if needed by tests, otherwise tests move to `upstream`.

**Also in this story**: `README.md`, `CLAUDE.md`, `.env.example` for the two settings; follow-up
issue in ecommerce-infra to add them to the `ai` compose service.

## Complexity Tracking

None.
