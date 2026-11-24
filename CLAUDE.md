# CLAUDE.md

AI microservice: Python 3.12, FastAPI, Pydantic v2, structlog; later LangGraph, FastMCP, aiokafka,
PostgreSQL 16 and the Anthropic API. The source of truth for every rule below is
`.specify/memory/constitution.md`. If this file and the constitution disagree, the constitution
wins.

## Stack

- Python 3.12 **through uv only**: always `uv run ...`. The machine's global Python must not be
  used. Add dependencies with `uv add` / `uv add --dev`, and commit `uv.lock`.
- Runtime now: FastAPI + uvicorn, Pydantic v2 + pydantic-settings, httpx, structlog,
  PyJWT[crypto]. Dev: Ruff, mypy strict (pydantic plugin), pytest, pytest-asyncio
  (`asyncio_mode = "auto"`), pytest-cov, respx.
- LangGraph, FastMCP, aiokafka and the Anthropic SDK are added only by the story that needs them.
- HTTP port `8002`. Run: `uv run python -m ecommerce_ai`. Local infra comes from
  `ecommerce-infra`: PostgreSQL `localhost:5436`, Kafka `localhost:9092`, gateway
  `http://localhost:8090/api`.

## Quality gate (after every task, before every commit)

Run these in order. All four must pass (quiet form under Token economy).

```sh
uv run ruff check            # rules E F I B UP ANN S C4 SIM RUF; `--fix` for fixable findings
uv run ruff format --check   # `uv run ruff format` to fix
uv run mypy                  # strict, over src and tests; zero errors
uv run pytest --cov          # 80% line+branch coverage; live_llm excluded by default
```

Never commit a red gate. A task is not done until all four pass. Lint and format stay blocking in
CI (the user confirmed this; constitution XI). `.githooks/pre-commit` (enabled with
`git config core.hooksPath .githooks`) auto-fixes style on staged files and stops the commit when
it changed one: re-add and commit again. Unit tests never touch the
network: fake LLM, httpx `MockTransport`/respx, fake probes (`tests/support/probes.py`).

## Token economy

- Run the gate quietly and report only failures:
  `uv run -q ruff check -q && uv run -q ruff format --check -q && uv run -q mypy --no-error-summary && uv run -q pytest -q --no-header --cov --cov-report=`.
  If it passes, just say it passed.
- Do not re-read a file you just wrote or edited.
- Read only the files and line ranges you need (search first, then read a range).
- Read the constitution only for the principles relevant to the task, not the whole file.
- Keep Spec Kit documents (spec, plan, tasks, research) concise: each decision and its reason in
  one or two lines.
- Final summaries: at most 10 lines.
- Batch related edits and independent tool calls; commit after every task (Conventional Commits,
  after a green gate).
- One story per session: `/clear` before `/speckit-specify`; never carry a finished story's
  context.
- Small stories: only spec.md, a short plan.md and tasks.md. Fixes: no spec, just a `fix/` branch.

## Layering

**api → agents → tools → infrastructure**, and **messaging → agents**.

- `api`: routers and schemas, I/O only (auth, validation, SSE). One agent entry point per route.
- `agents`: graphs, nodes, state, prompts. No HTTP/Kafka/SQL; outside world only via tools or
  `typing.Protocol` ports. Domain decisions (triage cause, suggested action, gatekeeper) live here.
- `tools`: MCP server and tools, one documented endpoint each. No prompts, LLM calls or decisions.
- `messaging`: consumers/producers, dead letters, retry worker. No business logic.
- `infrastructure`: settings, logging, probes, HTTP/DB/LLM clients. No business decisions.
- `main.py` is the composition root (`create_app(settings=None, probes=None)`).
- Prompts live in versioned files under `agents/prompts/` (`triage.v1.md`), immutable once
  released; never inline prompt strings in Python.

## HTTP and security

- Now: `GET /health` (liveness, no dependencies, always 200) and `GET /ready` (TCP probes:
  `database` critical → 503 when down; `kafka` reported only). Probes live in
  `infrastructure/health.py`; nothing connects at startup.
- `POST /mcp` (exact path, `x-roles: [CUSTOMER]`): stateless FastMCP, plain JSON, five read-only
  tools: `get_order`, `list_my_orders` (Orders GraphQL via `ORDERS_GRAPHQL_URL`), `list_products`,
  `get_product` (`INVENTORY_BASE_URL`), `list_my_notifications` (`NOTIFICATIONS_BASE_URL`,
  `customerId` only from the token claim). REST upstreams go through
  `infrastructure/upstream.py::UpstreamTransport` + `rest_client.py::RestClient`; each upstream
  has its own `httpx.AsyncClient`; errors map once in `tools/upstream_errors.py`.
  `api/mcp.py::BearerAuthMiddleware` gives 401/503/403 and binds `RequestCaller`
  (`infrastructure/caller.py`); an upstream 401 turns the whole call into 401. Tools register in
  `main.py::build_tool_definitions`, are checked against `tools/registry.py::ALLOWLIST` at
  startup, and their schemas are pinned in `tests/contract/snapshots/tools.json`
  (`UPDATE_SNAPSHOTS=1` regenerates it). Settings: `JWKS_*`, `TOOL_*`, `UPSTREAM_*`.
- Every route declares access through `openapi_extra`: `PUBLIC` (`api/route_policy.py`) or
  `x-roles`. `assert_route_policies` fails `build_api` for an undeclared route (default deny).
- Protected routes (`/mcp` now): RS256 bearer from ecommerce-identity, verified locally against
  `IDENTITY_JWKS_URL` (lazy, cached), same 401 for every bad token, 403 without a role, 503 without
  keys. Tools forward the caller's JWT; never a service account (ADR 0001).
- Mutating tools pause for approval of the exact call (ADR 0002). Every LLM step has a timeout,
  bounded retries, a deterministic fallback and a token budget (ADR 0003).
- Secrets only from the environment; `DATABASE_URL` is a `SecretStr`. Never log tokens,
  passwords or personal data. `infrastructure/logging.py::redact_sensitive_data` scrubs every
  record; keep it last before rendering.
- Logs: structlog JSON with `request_id` bound by `api/middleware.py::RequestIdMiddleware`
  (`X-Request-Id`, validated, echoed). Get loggers with `structlog.get_logger(__name__)`.
- CORS origins only from `CORS_ALLOWED_ORIGINS`, empty by default.

## Code rules

- mypy strict, zero errors. No `Any` without a justification at the narrowest scope.
  `# type: ignore[code]` only with a justification comment.
- Pydantic v2 for every inbound message, model output and HTTP schema, `frozen=True` where
  possible. `StrEnum` for enums.
- No bare `except:`; never swallow exceptions.
- Docstrings on every public module, class and function; each package `__init__.py` describes its
  responsibility. Endpoints and models carry OpenAPI descriptions.
- Timestamps: UTC, timezone-aware, ISO 8601.

## Tests

- `tests/unit/`: no network, no containers. Integration tests (PostgreSQL, Kafka via
  Testcontainers) get the `integration` marker; live-model tests the `live_llm` marker (excluded
  by default).
- Drive the app with an httpx `AsyncClient` over `ASGITransport`, building it with
  `create_app(probes=[...])`. `create_app` reconfigures logging, so tests capturing logs call
  `configure_logging("INFO", stream)` after building the app.
- `tests/conftest.py` clears every settings variable from the environment for each test.
- No `time.sleep` or bare `asyncio.sleep`; use a polling helper with a timeout.
- Names describe behaviour: `test_should_..._when_...`.

## CI/CD

`ci.yml` (the gate above + actionlint), `security.yml` (gitleaks, pip-audit, CodeQL, Trivy),
`cd.yml` (reuses `ci.yml`, Trivy gate, then pushes to `ghcr.io/<owner>/ecommerce-ai`: `main` and
`sha-*` on `main`, semver tags on `vX.Y.Z`, which must equal `pyproject.toml`'s version). Actions
pinned by SHA, least-privilege permissions; check every workflow change with actionlint. New test
layers (integration, evals) extend `ci.yml` in the story that adds them.

## Branches and commits

- Branches: `feature/US-XX-short-description`, `fix/US-XX-short-description`,
  `chore/short-description`, created from `main`; never commit to `main` directly.
- `/speckit.specify`: clean tree → `git checkout main && git pull` →
  `git checkout -b feature/US-XX-...` → only then write the spec.
- Conventional Commits with scope: `feat(US-01): add product lookup tool`. Infra-only work:
  `chore:`, `build:`, `ci:`, `docs:`, `test:`. PRs reference the story: "Implements US-01: ...".

## Documentation

- ADRs in `docs/adr/` (template `docs/adr/template.md`, index `docs/adr/README.md`). Current: 0001
  token passthrough, 0002 human-in-the-loop, 0003 fallback and cost caps, 0004 evals as release
  gate.
- Keep `README.md`, this file and the `Dockerfile` up to date with every behaviour change.
- ecommerce-infra has the `ai.order-triage-completed` contract (`docs/events.md`: `cause` and
  `suggestedAction` enums, consumer group `ai-triage`, the HTTP exception for calls with the
  caller's token), the topic in `kafka-init`, `postgres-ai` on `5436` and the `ai` compose service.
  No gateway route yet: `/api/mcp` is a follow-up in ecommerce-infra (plain JSON, default
  timeout), which also adds `ORDERS_GRAPHQL_URL`, `INVENTORY_BASE_URL` and
  `NOTIFICATIONS_BASE_URL` to the `ai` service.
