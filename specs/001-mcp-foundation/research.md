# Research: US-01 MCP server foundation

Each entry: decision — reason (alternatives rejected).

1. **MCP library**: `fastmcp` 4.2.5 (pulls `mcp` 2.3.0), added with `uv add fastmcp`. — Declarative
   tools from Pydantic-typed functions, `on_call_tool` middleware, `ToolResult(is_error=...)`.
   (Raw `mcp` SDK: more boilerplate for the same transport.)
2. **Transport**: `mcp.http_app(stateless_http=True, json_response=True)` served at exactly
   `/mcp`, with no 307 redirect to `/mcp/` (MCP clients don't follow a redirect on POST). Preferred:
   `http_app(path="/mcp")` behind an exact-path ASGI `Route("/mcp", …)`. Fallback: `Mount("/mcp")`
   plus a slash-normalising wrapper. T025 decides between them. FastMCP's lifespan is chained into
   FastAPI's. — FR-001: no session id, JSON bodies. (SSE streaming / stateful sessions: rejected in
   clarification.)
3. **Auth placement**: pure ASGI middleware wrapping the mount (`api/mcp.py`), not FastMCP's
   `AuthMiddleware`/providers. — Keeps one verifier shared with future REST routes and the exact
   401/403/503 bodies of constitution II. (FastMCP `JWTVerifier`: different error bodies, own JWKS
   cache.)
4. **Route policy for a mount**: `assert_route_policies` extended to ASGI `Route` and `Mount`
   routes; the MCP route is registered with `x-roles: [CUSTOMER]` metadata. — Default deny must cover non-`APIRoute`s.
5. **JWKS cache**: own async `JwksCache` on `httpx.AsyncClient` (timeout 3 s), `PyJWK` per key,
   `asyncio.Lock` single-flight, refetch on unknown `kid` at most once per 30 s cooldown, cached
   keys kept when Identity is down. — Mirrors ecommerce-orders. (`PyJWKClient`: sync urllib, blocks
   the loop.)
6. **Verification**: `jwt.decode(algorithms=["RS256"], issuer, audience, leeway)`, required claims
   `sub`, `exp`, `iat`, `roles`; header `alg` checked before key lookup so `none`/HS* never reach a
   key. Every `PyJWTError` → one `InvalidToken`; no keys and fetch failed → `KeysUnavailable`.
7. **Caller propagation**: a `ContextVar[RequestCaller]` (`infrastructure/caller.py`) set by the
   auth middleware, read by tools; holds claims, the raw token as `SecretStr`, `request_id` and an
   `upstream_unauthenticated` flag. Reset in `finally`. — No token in tool arguments or results.
8. **Upstream 401 → HTTP 401**: the tool sets the flag and returns a generic error; the auth
   middleware buffers the (JSON, size-bounded) response and replaces it with the standard 401 when
   the flag is set. — FastMCP converts exceptions to tool results, so HTTP status must be decided
   outside it.
9. **Orders access**: GraphQL `POST {ORDERS_GRAPHQL_URL}` (new setting, default
   `http://localhost:8090/api/graphql`; the gateway routes `/api/graphql` to Orders, while
   `ORDERS_BASE_URL` points at REST `/api/orders`). Fixed query documents as constants in
   `tools/orders.py`, next to each tool's allowlist entry, passed to `OrdersClient.execute`; only
   variables vary. — Allowlist = these two operations.
10. **Error classification** (client → typed exceptions): HTTP 401 or `UNAUTHENTICATED` →
    `Unauthenticated`; 403 or `FORBIDDEN` → `Forbidden`; timeout, connect error, 5xx,
    `SERVICE_UNAVAILABLE`, non-JSON, oversize → `UpstreamError`; `errors` checked before `data`
    (Orders README). `BAD_USER_INPUT` cannot occur (inputs pre-validated) → `UpstreamError`.
11. **Retry**: own loop, no new dependency: attempts = 1 + `UPSTREAM_MAX_RETRIES` (2), delay
    `base·2^n` with full jitter (`random.SystemRandom`, avoids ruff S311), per-attempt timeout =
    min(remaining budget, tool timeout); no attempt starts if the delay would pass the deadline.
    Retries only on `UpstreamError` from timeout/connect/5xx/`SERVICE_UNAVAILABLE`. (tenacity:
    extra dependency for ~30 lines.)
12. **Size limit**: response read with `client.stream()` and aborted past
    `TOOL_MAX_RESPONSE_BYTES` (256 KiB). — Never buffer an unbounded body.
13. **Error results**: a FastMCP `on_call_tool` middleware maps `ToolFailure(code)` and argument
    `ValidationError` to `ToolResult(is_error=True, structured_content={error, message})` with
    fixed messages; unknown tool → JSON-RPC `-32602`. Same middleware logs name, outcome,
    duration. Behaviour pinned by unit tests against fastmcp 4.2.5.
14. **Untrusted envelope & truncation**: every success is `{source, trust: "untrusted_data",
    data, truncated: [json-paths]}`; free-text fields (`cancellationReason`, `productId`) cut to
    `TOOL_TEXT_MAX_CHARS` (500) with `…` and their path listed.
15. **Registry validation**: `ToolRegistry.build()` checks each `ToolDefinition` against the
    `ALLOWLIST` constant and for input/output schema, timeout > 0 and max size > 0; any gap raises
    `ToolRegistryError` inside `create_app` (startup fails).
16. **HTTP client lifetime**: one shared `httpx.AsyncClient` created at lifespan start (no
    connection until first use) and closed at shutdown. — FR-011.
17. **Tests**: RSA key generated per session (`cryptography`) in `tests/support/tokens.py`, JWKS and
    Orders mocked with respx; MCP driven by raw JSON-RPC POSTs over `ASGITransport` with the app's
    lifespan entered by a fixture. Contract tests in `tests/contract/` compare tool schemas with
    committed snapshots and the requested GraphQL fields with a vendored copy of Orders'
    `docs/schema.gql`; they run in the default (offline) gate, so `ci.yml` is unchanged.
