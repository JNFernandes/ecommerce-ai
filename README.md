# ecommerce-ai

The AI service of the ecommerce system (Orders: NestJS, Payments: .NET, Inventory: Java,
Notifications: Python, Identity). It reaches the platform only through the gateway at
`http://localhost:8090/api`, with the caller's own access token
([ADR 0001](docs/adr/0001-token-passthrough.md)). The rules it follows are in
[`.specify/memory/constitution.md`](.specify/memory/constitution.md).

**Status: bootstrap.** The service runs, answers health and readiness checks and logs structured
JSON. None of the capabilities below is implemented yet; each arrives with its own user story,
together with its dependencies (LangGraph, FastMCP, aiokafka, the Anthropic SDK).

## Planned capabilities

1. **MCP server**: exposes the platform's documented read APIs (products, orders through GraphQL,
   notifications, the current user) and one mutating tool (cancel order) as MCP tools. Every tool
   is allowlisted, typed, time-limited and called with the caller's token.
2. **Triage worker**: consumes `orders.order-cancelled`, `payments.payment-failed` and
   `inventory.stock-rejected`, explains what happened with a LangGraph flow (with a deterministic
   fallback, [ADR 0003](docs/adr/0003-fallback-and-cost-caps.md)) and publishes
   `ai.order-triage-completed`. It never acts; its `suggestedAction` is advice for a human.
3. **Conversational assistant**: FastAPI with SSE streaming and LangGraph with a PostgreSQL
   checkpointer, using the MCP tools. Mutating actions pause for explicit approval of the exact
   call ([ADR 0002](docs/adr/0002-human-in-the-loop-for-mutating-tools.md)), and a gatekeeper
   validates every customer-facing answer.

Releases are gated by an eval suite ([ADR 0004](docs/adr/0004-evals-as-release-gate.md)).

## HTTP API

| Route | Access | Behaviour |
|---|---|---|
| `GET /health` | public | Liveness: always `200 {"status": "UP"}` while the process answers; checks no dependency. |
| `GET /ready` | public | Readiness: `{"status", "dependencies": {"database", "kafka"}}` with `UP`/`DOWN` each. `503` when PostgreSQL is unreachable; Kafka is reported but never makes the service unready. The LLM provider will be reported (never failing) once its client exists. |
| `POST /mcp` | `CUSTOMER` | MCP Streamable HTTP, stateless, plain JSON responses (no session id, no SSE). Tools, all read-only: `get_order(orderId)`, `list_my_orders(limit=20 (1–50), status?)`, `list_products(limit=20 (1–50), offset=0)`, `get_product(productId)`, `list_my_notifications(limit, offset, orderId?)`. |
| `GET /docs`, `GET /openapi.json` | public | API documentation. |

### MCP endpoint

`/mcp` (exact path, no trailing slash) needs `Authorization: Bearer <Identity access token>`,
verified locally (RS256 only) against `IDENTITY_JWKS_URL`, which is fetched on first use and
cached; an unknown `kid` refetches at most once per `JWKS_REFETCH_COOLDOWN_SECONDS`. Every
missing or invalid token gets the same `401 {"detail":"Not authenticated"}` with
`WWW-Authenticate: Bearer`; no obtainable key gives `503`; a token without `CUSTOMER` gives `403`.
Tools call Orders GraphQL (`ORDERS_GRAPHQL_URL`), Inventory (`INVENTORY_BASE_URL`) and
Notifications (`NOTIFICATIONS_BASE_URL`, scoped by the token's `customerId` claim, never by input)
with the caller's own token and `X-Request-Id` (never a service account); if an upstream rejects
that token the whole call answers `401`. Each upstream has its own HTTP client, so one slow
service cannot starve the others. Results are
`{source, trust: "untrusted_data", data, truncated}` (`source`: `ecommerce-orders`, `inventory`
or `notifications`), with free text cut to `TOOL_TEXT_MAX_CHARS`;
errors are `isError` results with a fixed `{error, message}` (`not_found`, `not_allowed`,
`invalid_input`, `upstream_error`). Each call has a `TOOL_TIMEOUT_SECONDS` budget, retries
included, and reads at most `TOOL_MAX_RESPONSE_BYTES`. Tool schemas are pinned in
`tests/contract/snapshots/tools.json`. Examples: `specs/001-mcp-foundation/quickstart.md`,
`specs/002-inventory-notifications-tools/quickstart.md`.

Every route must declare its access (public or required roles); building the app fails for a route
that declares neither (default deny). Probes are TCP connects with a 1 s timeout, made only when
`/ready` is called: nothing connects at startup, so the service starts without Kafka, PostgreSQL or
an LLM key.

### Logging

Every line is one JSON object (`event`, `level`, `logger`, `timestamp` in UTC) and, inside a
request, `request_id`. The id comes from the `X-Request-Id` request header when it is 1–128
characters of `[A-Za-z0-9._:-]`, otherwise a new UUID; it is echoed in the `X-Request-Id` response
header. One `request completed` line is logged per request (method, path, status, duration), never
headers or bodies. A redaction filter runs on every record, including uvicorn's and httpx's:
fields named like `authorization`, `*token*`, `password`, `secret`, `api_key` or `cookie` are
replaced with `[REDACTED]`, and bearer/basic credentials, JWTs, passwords in URLs and
`password=...`-style pairs are scrubbed from all text, tracebacks included.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) 0.11 (it installs Python 3.12; never use the system Python).
- Docker, for the image and, later, the integration tests.
- The `ecommerce-infra` stack for PostgreSQL (`localhost:5436`), Kafka (`localhost:9092`) and the
  gateway (`localhost:8090`). Not needed to start the service or run the tests.

## Running locally

```sh
uv sync
cp .env.example .env              # optional; set DATABASE_URL credentials here, never in git
uv run --env-file .env python -m ecommerce_ai      # or: uv run python -m ecommerce_ai
curl localhost:8002/health
curl localhost:8002/ready
```

`python -m ecommerce_ai` serves `ecommerce_ai.main:create_app` on `HOST`:`PORT`.

Smoke-test `/mcp` against the local stack with a customer access token (lists the tools, calls
`list_my_orders`, and `get_order` when given an order id; `MCP_URL` overrides the default URL):

```sh
TOKEN=<access token> uv run python scripts/mcp_smoke.py [ORDER_ID]
```

### Configuration

All settings come from environment variables (no `.env` file is read implicitly). An invalid value
stops the service at startup with an error naming the variable.

| Variable | Default | Meaning |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address (`0.0.0.0` in the image) |
| `PORT` | `8002` | HTTP port |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `CORS_ALLOWED_ORIGINS` | empty | Comma-separated browser origins; empty = no cross-origin access |
| `IDENTITY_JWKS_URL` | `http://localhost:3001/.well-known/jwks.json` | Identity JWKS (http/https), fetched lazily, never at startup |
| `JWT_ISSUER` | `http://localhost:3001` | Required `iss` of access tokens |
| `JWT_AUDIENCE` | `ecommerce` | Required `aud` of access tokens |
| `JWT_CLOCK_TOLERANCE_SECONDS` | `5` | Clock skew allowed on `exp`/`iat` (`0`–`300`) |
| `ORDERS_BASE_URL` | `http://localhost:8090/api/orders` | Orders API through the gateway |
| `INVENTORY_BASE_URL` | `http://localhost:8090/api/inventory` | Inventory API through the gateway |
| `NOTIFICATIONS_BASE_URL` | `http://localhost:8090/api` | Notifications API through the gateway (`/notifications` appended) |
| `ORDERS_GRAPHQL_URL` | `http://localhost:8090/api/graphql` | Orders GraphQL (`order`, `orders`) used by the MCP tools |
| `JWKS_TIMEOUT_SECONDS` | `3` | Timeout of one JWKS fetch |
| `JWKS_REFETCH_COOLDOWN_SECONDS` | `30` | Minimum gap between refetches on an unknown `kid` |
| `TOOL_TIMEOUT_SECONDS` | `5` | Budget of one tool call, retries included |
| `TOOL_MAX_RESPONSE_BYTES` | `262144` | Largest upstream body a tool reads |
| `TOOL_TEXT_MAX_CHARS` | `500` | Length limit of free-text fields in tool results |
| `UPSTREAM_MAX_RETRIES` | `2` | Retries of a read after the first attempt (`0`–`5`) |
| `UPSTREAM_BACKOFF_BASE_SECONDS` | `0.2` | First retry delay; doubles per retry, full jitter |
| `DATABASE_URL` | `postgresql://localhost:5436/ecommerce_ai` | PostgreSQL 16 URL; put credentials in the environment only. Treated as a secret (never logged) |
| `KAFKA_BROKERS` | `localhost:9092` | Comma-separated `host:port` (`kafka:29092` in the compose network) |
| `ANTHROPIC_MODEL` | unset | LLM model id; unset = every LLM step uses its fallback |
| `LLM_TIMEOUT_SECONDS` | `30` | Timeout of one LLM call (`> 0`, `≤ 300`) |
| `DAILY_TOKEN_BUDGET` | `50000` | Hard cap on LLM tokens per UTC day (`0` = no LLM calls) |

### Docker

```sh
docker build -t ecommerce-ai .
docker run --rm -p 8002:8002 --network ecommerce_default \
  -e DATABASE_URL='postgresql://<user>:<password>@postgres-ai:5432/ecommerce_ai' \
  -e KAFKA_BROKERS=kafka:29092 \
  -e IDENTITY_JWKS_URL=http://identity:3001/.well-known/jwks.json \
  -e ORDERS_GRAPHQL_URL=http://gateway:8090/api/graphql \
  ecommerce-ai
```

Multi-stage build: uv installs the locked dependencies in a build stage; the runtime stage is
`python:3.12-slim` with only the virtual environment, running as non-root user `10001`. Its health
check calls `/health`. In `ecommerce-infra` it is the `ai` service (`apps` profile, no host port; `docker-compose.dev.yml` publishes 8002, `gateway-debug` serves its docs on 127.0.0.1:9002), with `postgres-ai` (`ai_user` / `ai_pass`, local only) and the `ai.order-triage-completed` topic.

## Quality gate

Run after every change, in this order. All four must pass before a commit.

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest --cov
```

- `ruff format` (without `--check`) fixes formatting; `ruff check --fix` fixes fixable findings.
- All four are blocking in CI (constitution XI). Lint is more than style: it also catches
  undefined names, likely bugs and security issues. CI runs every check even after one fails and
  prints the fix command.
- Optional pre-commit hook, once per clone: `git config core.hooksPath .githooks`. It runs
  `ruff format` and `ruff check --fix` on the staged files and stops the commit when it changed
  something, so you review, `git add` and commit again (it never stages for you). Disable with
  `git config --unset core.hooksPath`.
- `mypy` runs in strict mode over `src` and `tests`.
- `pytest --cov` fails below 80% line and branch coverage. Tests never touch the network; the
  `live_llm` marker is excluded by default (`-m live_llm` to run those).

## CI/CD and security

- `.github/workflows/ci.yml`: the quality gate above, plus actionlint on the workflows.
- `.github/workflows/security.yml`: gitleaks over the full history, pip-audit over every locked
  package, CodeQL (Python and Actions), and Trivy on the built image (SARIF to the Security tab;
  fails on fixable HIGH/CRITICAL). Also weekly on Mondays.
- `.github/workflows/cd.yml`: publishes the image to GitHub Container Registry after the
  quality gate (the same `ci.yml`, reused) and a Trivy gate pass. Every push to `main` publishes
  `ghcr.io/<owner>/ecommerce-ai:main` and `:sha-<short>`; a release tag `vX.Y.Z` publishes `:X.Y.Z`,
  `:X.Y` and `:latest`. Images carry SLSA provenance and an SBOM.
- `.github/dependabot.yml`: weekly grouped updates for uv, Docker and GitHub Actions.

To release: set `version` in `pyproject.toml` (the tag must match it, or CD fails), merge, then
`git tag v0.1.0 && git push origin v0.1.0`. Pull with `docker pull ghcr.io/<owner>/ecommerce-ai:0.1.0`
(log in to ghcr.io first if the package is private; its visibility is set in the package's
settings on GitHub).

Actions are pinned by commit SHA and every job has least-privilege permissions.

## Project layout

```text
src/ecommerce_ai/
  main.py            Composition root: settings, logging, readiness probes, the app factory
  __main__.py        `python -m ecommerce_ai`
  api/               FastAPI app, routers, request-id middleware, route access policy, /mcp auth
  agents/            LangGraph graphs, nodes and versioned prompts (empty)
  tools/             MCP server, tool registry/allowlist, Orders/Inventory/Notifications tools
  messaging/         Kafka consumers, producers, dead letters (empty)
  infrastructure/    Settings, logging/redaction, probes, JWKS cache, token verifier, Orders client
tests/unit/          Unit tests (no network)
tests/contract/      Tool schema snapshot (five tools) and Orders queries checked against its vendored schema
tests/support/       Test tokens/JWKS, Orders stub, MCP JSON-RPC helpers, fake probes
scripts/             Manual tools: mcp_smoke.py (smoke test of /mcp against the local stack)
docs/adr/            Architecture Decision Records
```
