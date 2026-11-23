# Feature Specification: US-01 MCP server foundation

**Feature Branch**: `feature/US-01-mcp-foundation`

**Created**: 2026-11-23

**Status**: Draft

**Input**: User description: "US-01 MCP server foundation: an MCP server that exposes the Orders API
as read-only tools (get_order, list_my_orders), authenticated with the caller's own JWT, so that the
AI can never see or do more than the user can."

## Clarifications

### Session 2026-11-23

- Q: Which roles may call `/mcp`? → A: `CUSTOMER` only (`x-roles: [CUSTOMER]`); any other valid
  token gets 403.
- Q: Stateless or stateful Streamable HTTP? → A: Stateless: no session id, each request
  authenticated on its own, JSON responses.
- Q: How do tool errors reach the client? → A: Tool result with `isError: true` and
  `{error: <code>, message: <generic>}`; unknown tool → JSON-RPC error; upstream 401 → HTTP 401.
- Q: Retry bound for upstream reads? → A: Up to 2 retries (3 attempts), exponential backoff with
  jitter from 200 ms, configurable, cut off by the tool timeout.

## User Scenarios & Testing *(mandatory)*

Actor: an **AI client** (the chat assistant now, the triage worker later) calling the MCP endpoint
on behalf of a signed-in user, with that user's access token.

### User Story 1 - Read one of my orders (Priority: P1)

An AI client, acting for a customer, asks for one order by id and gets that order's data, but only
if the customer could see it themselves.

**Why this priority**: The smallest useful slice: it proves authentication, token passthrough, the
tool registry and error mapping end to end.

**Independent Test**: Call `get_order` over `/mcp` with a customer token against a stubbed Orders
API; check the result, the forwarded token and the error cases.

**Acceptance Scenarios**:

1. **Given** a valid customer token and an order of that customer, **When** `get_order(orderId)`
   is called, **Then** the order is returned, labelled as untrusted data, with only fields Orders
   documents.
2. **Given** a valid token and another customer's order id (or an unknown id), **When**
   `get_order` is called, **Then** the tool returns the generic error "order not found", identical
   for both cases.
3. **Given** a valid token, **When** any tool is called, **Then** Orders receives exactly the
   caller's token, unchanged, plus the request's `X-Request-Id`, and the token appears in no log.
4. **Given** Orders is down, slow or answers 5xx, **When** `get_order` is called, **Then** a
   generic tool error is returned within the tool timeout, with no upstream body, URL or trace.
5. **Given** Orders rejects the token as not allowed, **When** the tool runs, **Then** it returns
   "not allowed"; **given** Orders rejects the token as unauthenticated, **Then** the MCP call
   fails with 401.

---

### User Story 2 - List my orders (Priority: P2)

An AI client lists the caller's recent orders, optionally by status, to answer "what are my
orders?".

**Why this priority**: Second read tool on the same foundation; needed by the chat assistant.

**Independent Test**: Call `list_my_orders` with and without `limit`/`status` against a stub and
check paging bounds and scoping.

**Acceptance Scenarios**:

1. **Given** a valid customer token, **When** `list_my_orders()` is called, **Then** at most 20 of
   the caller's orders are returned in Orders' order (newest first), with the total count and whether more exist.
2. **Given** `limit` 50, **Then** up to 50 orders are returned; **given** `limit` < 1 or > 50 or an
   unknown `status`, **Then** the call is rejected as invalid input and Orders is not called.
3. **Given** `status` `PENDING`, **Then** only the caller's `PENDING` orders are returned.
4. **Given** a `CUSTOMER` token without a `customerId` claim, **Then** the tool returns "not
   allowed" without calling Orders and never lists other customers' orders.

---

### User Story 3 - Reject unauthenticated access (Priority: P1)

Only callers with a valid access token from Identity can use the MCP endpoint at all.

**Why this priority**: Security baseline (constitution II); without it nothing else may ship.

**Independent Test**: Send MCP requests with no, tampered, expired, wrong-issuer, wrong-audience
and `alg=none` tokens and with Identity keys unavailable.

**Acceptance Scenarios**:

1. **Given** no `Authorization` header, **When** any MCP request is sent, **Then** 401 with
   `WWW-Authenticate: Bearer` and a generic body.
2. **Given** a tampered, expired, wrong-issuer, wrong-audience, unknown-`kid`, non-RS256 or
   `alg=none` token, **Then** the same 401 response as above, byte for byte.
3. **Given** Identity's keys cannot be fetched and none are cached, **Then** 503.
4. **Given** a valid token without the `CUSTOMER` role (e.g. admin-only), **Then** 403 and no tool
   runs.
5. **Given** Identity and Orders are both down at startup, **Then** the service starts and
   `/health` answers 200.

---

### Edge Cases

- Malformed `orderId` (not a UUID): invalid input, Orders not called.
- Orders returns an order that is not yet fully projected (items, total, placedAt null): returned
  with those fields empty, not treated as an error.
- Orders response larger than the tool's maximum size, or not matching the output schema: generic
  tool error, nothing partial returned.
- Free-text fields (e.g. cancellation reason, product id) longer than their limit: truncated and
  marked as truncated.
- Text inside a tool result that looks like an instruction stays data; it never changes the tools
  available or the identity used.
- JWKS cached but Identity down: tokens still verified with cached keys; unknown `kid` triggers at
  most one refetch per cooldown.
- A tool defined without an allowlist entry, input schema, output schema, timeout or size limit:
  the service refuses to start (and a test fails).
- Unknown tool name: rejected with a JSON-RPC error; never forwarded.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The service MUST expose an MCP server over Streamable HTTP at `/mcp` in the existing
  app; no stdio transport. The transport MUST be stateless (no `Mcp-Session-Id`, no server-side
  session state, each request authenticated on its own) and answer with JSON, not SSE streams.
- **FR-002**: Every `/mcp` request MUST carry `Authorization: Bearer <token>`, verified locally
  against Identity's JWKS: RS256 only, key chosen by `kid`, issuer, audience and clock tolerance
  from settings, required claims `sub`, `exp`, `iat` and `roles`, JWKS fetched lazily, cached, refetched on unknown `kid` at most once per cooldown
  (same behaviour as ecommerce-orders).
- **FR-003**: Every missing or invalid token MUST get one identical 401 (`WWW-Authenticate:
  Bearer`, generic body); no obtainable keys MUST give 503. `/mcp` MUST declare
  `x-roles: [CUSTOMER]` in the route policy (default deny still holds); a valid token without the
  `CUSTOMER` role MUST get 403.
- **FR-004**: Every tool call MUST forward the caller's token unchanged to Orders via the gateway
  GraphQL URL (`ORDERS_GRAPHQL_URL`) and propagate `X-Request-Id`. No service account, no other
  credential, no token storage beyond the request, no token in logs or tool results.
- **FR-005**: A tool registry MUST hold, per tool: allowlist entry, input schema, output schema,
  timeout (default 5 s, configurable) and maximum response size. A tool missing any of these, or
  not on the allowlist, MUST make startup fail.
- **FR-006**: `get_order(orderId: UUID)` MUST return one order through the Orders GraphQL `order`
  query, with only documented fields: orderId, customerId, status, totalAmount, placedAt,
  confirmedAt, cancelledAt, cancellationReason, items (productId, quantity, unitPrice, subtotal).
- **FR-007**: `list_my_orders(limit = 20 [1..50], status?: PENDING|CONFIRMED|CANCELLED)` MUST
  return the caller's orders through the GraphQL `orders` query (newest first) with totalCount and
  hasNextPage, filtered by the caller's own `customerId` claim; a token without one gets "not
  allowed" without calling Orders.
- **FR-008**: Error mapping: `order` null (unknown or another customer's) → "order not found";
  Orders `FORBIDDEN`/403 → "not allowed"; Orders `UNAUTHENTICATED`/401 → the MCP call fails with
  401; timeout, 5xx, `SERVICE_UNAVAILABLE`, oversize or schema mismatch → one generic tool error.
  No upstream body, URL, header or stack trace ever reaches the client. Tool errors (including
  `invalid_input`) MUST be returned as a tool result with `isError: true` and
  `{error: <code>, message: <generic text>}`; an unknown tool name MUST be a JSON-RPC error.
- **FR-009**: Every tool result MUST be wrapped as untrusted data (an explicit data envelope naming
  its source) and every free-text field MUST be length-limited, with truncation marked.
- **FR-010**: Upstream calls MUST respect the tool timeout overall, with up to 2 retries
  (3 attempts, configurable) and exponential backoff with jitter from 200 ms, only for
  timeouts/5xx/`SERVICE_UNAVAILABLE` on these read-only calls; no retry starts once the tool
  timeout would be exceeded, and each attempt's timeout is capped by the remaining budget
  (constitution IV).
- **FR-011**: The service MUST start and stay live when Identity or Orders is down; nothing
  connects at startup.
- **FR-012**: Each tool call MUST be logged (tool name, outcome, duration, `request_id`) without
  tokens or personal data.

### Key Entities

- **Tool definition**: name, allowlist entry (upstream operation), input schema, output schema,
  timeout, maximum response size, read-only flag.
- **Caller identity**: verified token claims (subject, roles, customerId) plus the raw token, held
  only for the duration of the request.
- **Order / OrderItem / OrdersPage**: as documented in Orders' GraphQL schema (fields in FR-006/007).
- **Tool result envelope**: source, untrusted-data label, payload, truncation markers; or a
  generic error code (`not_found`, `not_allowed`, `invalid_input`, `upstream_error`).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of the listed bad-token cases (none, tampered, expired, wrong issuer, wrong
  audience, unknown kid, `alg=none`) get the identical 401 response.
- **SC-002**: In every tool test, the token Orders receives equals the caller's token byte for
  byte, and the token string appears in 0 captured log lines.
- **SC-003**: With Orders unreachable or hanging, a tool call returns a generic error within the
  configured timeout plus 1 s.
- **SC-004**: A customer never receives data of another customer's order in any tested case.
- **SC-005**: The service starts and `/health` answers 200 within 5 s with Identity and Orders down.
- **SC-006**: Registering a tool without any one required attribute fails startup in a test
  (one test per attribute).

## Assumptions

- Orders exposes reads only via GraphQL (`POST /api/graphql` through the gateway, `order` and
  `orders` queries); there is no REST GET for orders. GraphQL errors arrive with HTTP 200 and an
  error code, so the mapping uses both HTTP status and GraphQL codes.
- The `list_my_orders` max of 50 is stricter than Orders' 100, on purpose (bounded model context).
- Defaults: maximum response size 256 KiB per tool; free-text limit 500 characters; JWKS refetch
  cooldown 30 s; all configurable.
- The constitution's allowlist names `POST /api/graphql` "(`orders` query only)"; `get_order`
  needs the `order` query too, so the allowlist entry is amended to "`order` and `orders`
  queries" (constitution MINOR amendment 1.1.0, part of this story).
- The gateway route for `/api/mcp` is added in ecommerce-infra as a follow-up of this story; it
  needs no SSE write timeout, because responses are plain JSON.
- Out of scope: mutating tools (US-06), Inventory/Notifications tools (US-02), admin tools, the
  LLM, Kafka.
