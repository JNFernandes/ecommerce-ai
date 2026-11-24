# Research: US-02

1. **Shared transport** — Extract headers, retry, streamed size cap and status classification from
   `OrdersClient` into `infrastructure/upstream.py::UpstreamTransport`; Orders keeps only GraphQL
   parsing. Why: FR-001 forbids per-service copies. Alt: copy `OrdersClient` (rejected, duplicates).
2. **Service name in errors** — `UpstreamTransport(service="inventory")` labels internal reasons;
   tools map `UpstreamError` → `upstream_error` "`<service>` unavailable". Why: one mapping, FR-008.
3. **404 as `NotFound`** — Transport raises `NotFound` on 404 (non-retryable); `get_product` maps it
   to "product not found"; for `list_*` 404 is `upstream_error`. Why: only one route has a 404 meaning.
4. **One `httpx.AsyncClient` per upstream** — Separate pools so a hanging Inventory can't exhaust
   connections used by Orders/Notifications (FR-010). Alt: shared client (rejected: shared pool limit).
5. **Base URLs** — `INVENTORY_BASE_URL` default `http://localhost:8090/api/inventory` (+ `/products`),
   `NOTIFICATIONS_BASE_URL` default `http://localhost:8090/api` (+ `/notifications`). Validated as
   http(s) like `ORDERS_GRAPHQL_URL`. Paths are constants in tool code, matching the allowlist.
6. **Path safety** — `productId` validated by Pydantic (`^[A-Za-z0-9_-]{1,64}$`) and still
   percent-encoded when joined. Why: defence in depth; regex already excludes `/`, `?`, `.`.
7. **Response validation** — Pydantic models with `extra="ignore"`, strict types; 200 with
   `application/problem+json` or non-object body → `upstream_error`. `createdAt` is `AwareDatetime`
   normalised to UTC. Why: FR-008 "never partial data".
8. **Retries** — Reuse `call_with_retry`: retry transport errors/timeouts/5xx, never 4xx; whole budget
   = tool timeout. Same as US-01.
9. **Notifications scoping** — `customerId` taken from `RequestCaller.identity.customer_id`; `None` →
   `not_allowed` before any request. No `customerId` input field (FR-007).
10. **Free-text limit** — Reuse envelope truncation on `name` and `message`; `type`/`channel`/`status`
    capped at 64 chars as plain strings, not enums (spec assumption).
