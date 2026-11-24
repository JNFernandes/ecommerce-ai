# Tasks: US-02 MCP tools for Inventory and Notifications

**Input**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/mcp-tools.md](contracts/mcp-tools.md)

**Tests**: required (constitution VIII/IX). Unit tests use respx only, no network. Write each
story's tests first and see them fail. After every task: quiet gate (CLAUDE.md); commit after green.

## Phase 1: Setup

- [X] T001 Add `inventory_base_url` (default `http://localhost:8090/api/inventory`) and `notifications_base_url` (default `http://localhost:8090/api`) to `src/ecommerce_ai/infrastructure/settings.py`, validated by the existing `_check_http_url`; add both env names to the cleared variables in `tests/conftest.py`; cover defaults and invalid URLs in `tests/unit/test_settings.py`
- [X] T002 [P] Document `INVENTORY_BASE_URL` and `NOTIFICATIONS_BASE_URL` in `.env.example`

## Phase 2: Foundational (blocks all stories)

- [X] T003 Create `src/ecommerce_ai/infrastructure/upstream.py`: move `Unauthenticated`, `Forbidden`, `UpstreamError` from `orders_client.py`, add `NotFound`; add `UpstreamTransport(http, service, *, max_retries, backoff_base, clock, sleep)` with `async request(method, url, *, params=None, json=None, timeout, max_bytes) -> bytes` holding caller headers (bearer, `X-Request-Id`, `Accept: application/json`), `call_with_retry` with the whole-call deadline, streamed size cap and status classification (401/403/404 typed, 5xx retryable, other non-200 non-retryable, `application/problem+json` on 200 → `UpstreamError`); reasons say `"<service> ..."` (research 1–3, 8)
- [X] T004 Refactor `src/ecommerce_ai/infrastructure/orders_client.py` to build on `UpstreamTransport` (keeps only GraphQL `_parse` and `_raise_for_graphql_errors`); update imports in `src/ecommerce_ai/tools/orders.py` and existing tests; `tests/unit/test_orders_client.py` must pass unchanged in behaviour
- [X] T005 [P] Create `src/ecommerce_ai/infrastructure/rest_client.py`: `RestClient(transport, base_url)` with `async get_json(path, params, *, timeout, max_bytes) -> dict[str, object]` (percent-encodes path segments, rejects non-JSON or non-object bodies as `UpstreamError`)
- [X] T006 [P] Move the shared upstream→tool error mapping out of `src/ecommerce_ai/tools/orders.py` into `src/ecommerce_ai/tools/upstream_errors.py`: `Unauthenticated` → flag caller + 401, `Forbidden` → `not_allowed`, `UpstreamError`/`ValidationError` → `upstream_error` with a per-service message ("inventory unavailable", …), optional `NotFound` message; Orders behaviour unchanged
- [ ] T007 [P] Tests for the transport and REST client in `tests/unit/test_upstream.py` and `tests/unit/test_rest_client.py`: headers byte-equal to caller token, retry on 5xx/timeout only, deadline, oversize abort, 401/403/404 types, problem body, non-JSON, path encoding
- [ ] T008 Add three `AllowlistEntry` rows (`ecommerce-inventory GET /api/inventory/products`, `ecommerce-inventory GET /api/inventory/products/{id}`, `ecommerce-notifications GET /api/notifications`) in `src/ecommerce_ai/tools/registry.py`
- [ ] T009 In `src/ecommerce_ai/main.py` create one `httpx.AsyncClient` per upstream (Orders, Inventory, Notifications, JWKS may share Orders'), `RestClient`s for Inventory and Notifications, close all in `resources()`; extend `build_tool_definitions` signature for the new clients; update `tests/unit/test_main.py`
- [ ] T010 [P] Add respx stubs `tests/support/inventory_stub.py` and `tests/support/notifications_stub.py` (routes, sample pages, capture of request headers/params)

**Checkpoint**: gate green; Orders tools unchanged.

## Phase 3: US1 — Check product availability (P1) 🎯 MVP

**Independent test**: `list_products`/`get_product` over `/mcp` with a customer token against the Inventory stub.

- [ ] T011 [P] [US1] Tests in `tests/unit/test_inventory_tools.py`: default page (≤ 20, order kept, `total/limit/offset`, envelope `source=inventory`), `limit=50 offset=40` forwarded, invalid `limit`/`offset`/`productId` (`""`, 65 chars, `../admin`, `SKU 1`, `SKU-1?x=1`) → `invalid_input` with 0 requests, `SKU-003` found, 404 → "product not found", token + `X-Request-Id` forwarded, `availableQuantity` 0 kept, case kept, empty page past end
- [ ] T012 [US1] Implement `src/ecommerce_ai/tools/inventory.py`: `ListProductsInput`, `GetProductInput` (`extra="forbid"`), upstream `Product`/`ProductsPage` models (`extra="ignore"`, `name` truncated ≤ 500 with flag), `list_products` and `get_product` handlers via `RestClient` and `upstream_errors`, two `ToolDefinition`s (timeout and max size from settings)
- [ ] T013 [US1] Register both tools in `src/ecommerce_ai/main.py::build_tool_definitions`; add an `/mcp` call test for each in `tests/unit/test_mcp_tools_api.py`

## Phase 4: US2 — Read my notifications (P1)

**Independent test**: two customer tokens against the Notifications stub scoped by `customerId`; each sees only their own.

- [ ] T014 [P] [US2] Tests in `tests/unit/test_notifications_tool.py`: default page newest-first as given, `customerId` param equals the caller claim (also for `CUSTOMER`+`ADMIN`), token without `customerId` → `not_allowed` with 0 requests, `customerId` input → `invalid_input`, A vs B results differ, `orderId` forwarded, malformed `orderId`/`limit`/`offset` → `invalid_input`, 403 → `not_allowed`, 401 → MCP HTTP 401, 422/503 → "notifications unavailable", `customerId`/`sourceEventId` absent from output, `createdAt` UTC
- [ ] T015 [US2] Implement `src/ecommerce_ai/tools/notifications.py`: `ListMyNotificationsInput` (`extra="forbid"`, `orderId: UUID | None`), `Notification`/`NotificationsPage` (`type`/`channel`/`status` str ≤ 64, `message` truncated ≤ 500, `createdAt: AwareDatetime` → UTC), handler reading `customer_id` from `current_caller()`, `ToolDefinition`
- [ ] T016 [US2] Register `list_my_notifications` in `src/ecommerce_ai/main.py::build_tool_definitions`; add an `/mcp` call test in `tests/unit/test_mcp_tools_api.py`

## Phase 5: US3 — Fail safely and in isolation (P1)

**Independent test**: Inventory/Notifications down, slow, 5xx, malformed, oversize, injection text; every tool called.

- [ ] T017 [P] [US3] Tests in `tests/unit/test_tool_isolation.py`: each failure mode → generic "<service> unavailable" with no body/URL/trace, within timeout + 1 s (fake clock); Orders tools unchanged while Inventory/Notifications are down/hanging; injection-like `name`/`message` returned unchanged and tool list + forwarded token identical before/after; no token → 401 for each new tool; token absent from captured logs (SC-001)
- [ ] T018 [US3] Regenerate `tests/contract/snapshots/tools.json` with `UPDATE_SNAPSHOTS=1 uv run pytest tests/contract`; assert exactly 5 tools with schema, timeout and max size in `tests/contract/test_tool_schemas.py`
- [ ] T019 [US3] Fix any gap the T017 tests reveal in `src/ecommerce_ai/infrastructure/upstream.py` or `src/ecommerce_ai/tools/upstream_errors.py` (no per-tool special cases)

## Phase 6: Polish

- [ ] T020 [P] Update `README.md` and `CLAUDE.md` (HTTP and security: five tools, the two settings, per-upstream clients)
- [ ] T021 [P] Note the ecommerce-infra follow-up (add `INVENTORY_BASE_URL`, `NOTIFICATIONS_BASE_URL` to the `ai` service) in `specs/002-inventory-notifications-tools/pr-description.md`
- [ ] T022 Run the full quiet gate and the [quickstart.md](quickstart.md) offline steps; coverage ≥ 80%

## Dependencies

Setup → Foundational (T003 → T004; T005/T006/T007 after T003; T009 after T005) → US1, US2 in any
order or parallel → US3 (needs both tools) → Polish.

## Parallel examples

- Foundational: T005, T006, T007, T010 together after T003.
- US1 ‖ US2: T011+T012 alongside T014+T015 (different files); T013/T016 both touch `main.py`, run sequentially.

## Strategy

MVP = Phases 1–3 (catalog tools). Then US2, then US3 hardening + snapshot, then Polish. Commit per task
after a green gate.
