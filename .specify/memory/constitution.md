<!--
SYNC IMPACT REPORT
==================
Version change:    1.1.0 → 2.0.0
Bump rationale:    MAJOR. Principle IV's triage-worker rules are redefined (US-04): one result per
                   order instead of per consumed `eventId`, an outbox instead of an outbound
                   dead-letter table, and `modelId` null on a fallback. VI and VII gain interim
                   rules until US-09 (evals) and US-08 (tracing).
Approval:          sole maintainer (self-approved in the US-04 PR).

Modified principles:
  - IV. Reliability & Cost Control — fallback marked by `source = FALLBACK_TEMPLATE` and
    `modelId = null`; intake idempotent by `eventId`; one result per order; flow split into
    intake (evidence → commit) and triage (result + outbox + status in one transaction →
    publish); outbox relay replaces the outbound dead-letter table.
  - VI. Evals as Release Gate — suite introduced by US-09; interim gate: gatekeeper and injection
    unit tests, pinned prompt hashes, a `live_llm` test.
  - VII. Observability — until US-08, one structured log line per graph run with latency, tokens,
    `promptVersion`, `modelId`, `orderId`.

Added sections:   (none)
Removed sections: (none)

Migration note:   no existing code depends on the replaced rules (no result or publish existed
                  before US-04); US-03 intake already matches the new intake rule.

Templates reviewed:
  ✅ .specify/templates/plan-template.md, spec-template.md, tasks-template.md — no change needed.

Runtime guidance:
  ⚠ README.md, CLAUDE.md — updated by US-04 (T037).

Cross-repository follow-ups (ecommerce-infra, before merge):
  ⚠ docs/events.md — `ai.order-triage-completed`: one result per order (case), outbox
    redelivery instead of "dead-lettered", `modelId` null only for `FALLBACK_TEMPLATE`.

Deferred TODOs:
  - TODO(WORKER_SERVICE_IDENTITY): ecommerce-identity documents no service-account or
    client-credentials flow. Until one is documented in its README, the triage worker MUST work
    from event payloads only and MUST NOT call any tool (Principle II).
  - TODO(EVAL_SUITE): US-09 introduces the suite; it MUST include cases for the triage prompt, the
    gatekeeper and every tool registered by then (Principle VI).
  - TODO(EVAL_THRESHOLD): the composite-score threshold value is agreed in the story that
    introduces the eval suite and recorded in the suite configuration (Principle VI).
  - TODO(TRACING): US-08 adds the trace backend (LangSmith or OpenTelemetry) (Principle VII).
-->

# ecommerce-ai Constitution

`ecommerce-ai` is the Python AI service of a polyglot e-commerce system (Orders: NestJS, Payments:
.NET, Inventory: Java, Notifications: Python, Identity), whose HTTP APIs are reached through the
gateway at `http://localhost:8090/api`. It has three capabilities: (a) an **MCP server** that
exposes the platform's documented APIs as tools, (b) a **triage worker** that consumes failed or
cancelled order events, explains them with a LangGraph flow and publishes
`ai.order-triage-completed`, and (c) a **conversational assistant** (FastAPI, SSE streaming,
LangGraph with a PostgreSQL checkpointer) that uses the MCP tools. The REST contracts in each
service's `README.md` and the event contracts in `ecommerce-infra/docs/events.md` are the source of
truth for every endpoint, topic, field and payload this service uses; it MUST NOT invent
endpoints, fields, topics or enum values.

Out of scope unless this constitution is amended: fine-tuning, autonomous refunds or any other
autonomous money movement, multi-tenant model hosting, voice, and vector stores or RAG over
documents.

## Core Principles

### I. Layered Architecture

The codebase MUST follow a strict layered model with five top-level packages:
**api → agents → tools → infrastructure**, and **messaging → agents**.

- **`api`** — FastAPI routers and their Pydantic request/response schemas. Routers handle I/O
  only (authentication, validation, SSE framing) and call exactly one entry point of an agent.
  They MUST NOT contain prompts, graph logic or HTTP calls to other services.
- **`agents`** — LangGraph graphs, nodes, state models and prompts. Nodes MUST NOT contain HTTP,
  Kafka or SQL code: they reach the outside world only through tools or through
  `typing.Protocol` ports implemented in `infrastructure`. The domain decisions of this service
  (triage cause, suggested action, gatekeeper verdict) live here.
- **`tools`** — the MCP server and the tool definitions. Each tool wraps exactly one documented
  endpoint of a platform service (Principle II) and MUST NOT contain prompt text, LLM calls or
  business decisions.
- **`messaging`** — Kafka consumers and producers, the outbound dead-letter table access and the
  retry worker. Consumers deserialize, validate and call one agent entry point. They MUST NOT
  contain business logic.
- **`infrastructure`** — HTTP clients (`httpx`), persistence (PostgreSQL, the LangGraph
  checkpointer), the LLM client and configuration (`pydantic-settings`). It MUST NOT make
  business decisions.

Prompts:
- Every prompt MUST live in a versioned file under `agents/prompts/` (for example
  `triage.v1.md`) and be loaded by id and version. Inline prompt strings in Python code are
  prohibited.
- A prompt file is immutable once released: a change is a new version, and the active version is
  selected in code or configuration and recorded in every trace and published event
  (`promptVersion`).

Placing logic in the wrong layer is a constitutional violation and MUST be rejected in code
review.

### II. Security First (NON-NEGOTIABLE)

Authentication:
- Every route except health, liveness, readiness and the API documentation MUST require an RS256
  bearer token issued by `ecommerce-identity`, verified locally against its JWKS with the same
  settings as the other services: `IDENTITY_JWKS_URL`, `JWT_ISSUER`, `JWT_AUDIENCE`, a fixed
  algorithm list and a configured clock tolerance. Keys MUST be fetched lazily and cached;
  Identity's availability MUST NOT affect startup or readiness.
- Every route MUST declare its access (public or the roles it requires). A route that declares
  neither MUST make the application fail to build (default deny). The MCP server endpoint
  requires a token like any other protected route.
- Every missing or invalid token MUST get the same 401 response (`WWW-Authenticate: Bearer`); a
  valid token without a usable role gets 403; no obtainable signing keys gets 503.

Token passthrough (ADR 0001):
- Tools MUST call the platform APIs through the gateway with the **caller's own JWT**. They MUST
  NOT use a service account, an admin token or any credential other than the caller's. The AI can
  therefore never see or do more than the user can (confused-deputy prevention), and ownership is
  enforced by the platform service, not by the model.
- The triage worker MUST use a dedicated service identity limited to read-only tools. Until
  `ecommerce-identity` documents a way to issue such an identity, the worker MUST work from event
  payloads only and MUST NOT call any tool.

Tool allowlist:
- Every tool MUST have an allowlist entry, a typed Pydantic input schema and a timeout. A tool
  that is not on the allowlist MUST NOT be registered.
- Each tool maps to exactly one endpoint documented in the owning service's README. The initial
  allowlist is limited to:
  - `GET /api/inventory/products`, `GET /api/inventory/products/{id}` — read.
  - `POST /api/graphql` (`order` and `orders` queries) — read.
  - `GET /api/notifications`, `GET /api/notifications/{id}` — read.
  - `GET /api/identity/users/me` — read.
  - `POST /api/orders/{orderId}/cancel` — **mutating**, governed by Principle III.
- Payments exposes no HTTP API; no payment tool exists until Payments documents one. Admin-only
  endpoints (order confirmation, user and role management, audit) MUST NOT be exposed as tools.
- Adding a tool requires the endpoint to be documented in the owning service's README first, an
  allowlist entry, a contract test (Principle IX) and an eval suite run (Principle VI).

Secrets and personal data:
- Secrets (API keys, database passwords, the service credential) MUST come from environment
  variables only and MUST NOT be committed, logged or sent to the model.
- Tokens, passwords and personal data MUST NOT appear in logs, traces or prompts beyond what the
  task needs. The caller's JWT is never part of a prompt or a tool result shown to the model.

Prompt-injection defense:
- All input from users, events and tool results is untrusted **data**, never instructions. It
  MUST be passed to the model in clearly delimited data sections, and no text inside it can
  change the allowlist, the approval rules of Principle III or the identity a tool runs with.
- CORS allowed origins MUST come from an environment variable and MUST be empty by default.

### III. AI Safety & Human-in-the-Loop (NON-NEGOTIABLE)

- The AI MUST NOT move money or change state on its own. Tools are read-only by default.
- Any mutating tool (for example cancel order) MUST pause the graph with a LangGraph
  `interrupt` before the call and MUST require explicit user approval of the **exact** action:
  the tool, its target id and its arguments as they will be sent (ADR 0002). An approval applies
  to that single call only; any change to the action requires a new approval. A rejection, a
  timeout or an approval from anyone other than the token's subject MUST result in no call.
- The triage worker MUST NOT call any mutating tool. `suggestedAction` is a suggestion for a
  human, never an executed action.
- A gatekeeper step MUST validate every customer-facing answer before it is sent. It MUST reject
  any answer that states a refund, amount, status, date or promise not present in a tool result
  or event payload of the current run, or that contains data of another user. A rejected answer
  is replaced by a safe deterministic response; it is never sent as is.
- Every model output used by code (tool arguments, triage results, gatekeeper verdicts) MUST be
  structured and validated by a Pydantic v2 model. An output that fails validation MUST NOT be
  partially used; it goes to the retry and fallback rules of Principle IV.

### IV. Reliability & Cost Control

Startup resilience:
- The service MUST start and answer health checks without Kafka, the LLM provider or the
  database being reachable. Liveness is `UP` whenever the process answers; readiness reports the
  state of the database, Kafka and the LLM provider, answers 503 when the database is
  unreachable, and MUST NOT fail because of Kafka or the LLM provider.

Timeouts, retries and fallback (ADR 0003):
- Every LLM call and every HTTP call MUST have a timeout and bounded retries with exponential
  backoff. Unbounded retries are prohibited.
- Every LLM-backed step MUST have a deterministic fallback. For triage, the fallback is a template
  explanation derived from the event alone, so a provider outage never blocks the event flow. A
  fallback result MUST be recognisable: its `source` is `FALLBACK_TEMPLATE` and its `modelId` is
  `null`, never a model id.

Cost budgets:
- Per-request and per-day token and cost budgets MUST be enforced from configuration, with a hard
  cap. A request that would exceed its budget MUST stop and use the fallback; once the daily cap
  is reached, every LLM-backed step uses its fallback until the next day (UTC).

Triage worker:
- Intake MUST be idempotent by `eventId`: each consumed event is stored at most once as evidence
  (UNIQUE constraint), and a redelivery is a recognised no-op.
- At most one triage result MUST exist per order (triage case, UNIQUE constraint); evidence
  arriving after it never produces a second result.
- The flow MUST be: consume → validate → save the evidence → commit the offset; then, once the
  case has settled: triage → save the result, its outgoing event and the case status in one
  transaction → publish. Committing an offset before the evidence is saved is a critical
  violation.
- Every outgoing event MUST be saved in an outbox table (topic, key, payload, `eventId`, status,
  attempts, last error, timestamps) in the same transaction as the result. A relay MUST publish
  it, retrying with bounded exponential backoff with the same `eventId`, key and payload until it
  is resolved or the attempts run out. Unparseable or invalid inbound messages MUST be stored in
  an inbound dead-letter table without retries, after which the offset is committed.
- All timestamps MUST be UTC, timezone-aware, and serialized as ISO 8601.

### V. Event Contracts

Kafka is the event bus between services. `ecommerce-infra/docs/events.md` is the source of truth
for every topic and payload.

**Consumed topics:**
- `orders.order-cancelled` — `aggregateId` is the order id; `customerId` and the optional
  `reason` are read as documented.
- `payments.payment-failed` — `aggregateId` is the payment id; the order is identified by
  `orderId`, which MUST be used instead of `aggregateId`.
- `inventory.stock-rejected` — `aggregateId` is the reservation id; the order is identified by
  `orderId`, which MUST be used instead of `aggregateId`.

**Produced topics:**
- `ai.order-triage-completed` — the standard envelope (`eventId`, `aggregateId` = order id,
  `occurredAt`, `version`) plus `orderId`, `cause`, `explanation`, `suggestedAction`,
  `confidence`, `promptVersion` and `modelId`. The Kafka message key is the `aggregateId`.

**Contract rules:**
- The new contract and topic MUST be added to `ecommerce-infra/docs/events.md` and to the
  `kafka-init` topic list in `ecommerce-infra/docker-compose.yml` (a linked PR) **before** the
  service publishes to it. Topic auto-creation is off.
- `cause` and `suggestedAction` MUST be enums (`StrEnum`) whose values are listed in
  `events.md`; `confidence` is a number from 0 to 1.
- Every event MUST include `eventId` (UUID), `occurredAt` (ISO 8601 timestamp), `aggregateId`
  (UUID) and `version` (integer). Payloads are JSON. Unknown extra fields MUST be ignored; a
  missing required field makes the message invalid.
- Event models MUST carry a docstring for every field and MUST match `events.md`. Any change to
  an event contract MUST update that file in the same change set.
- Consumers and producers MUST NOT contain business logic (Principle I).

### VI. Evals as Release Gate (NON-NEGOTIABLE)

- The verdict evaluation framework (https://github.com/JNFernandes/verdict) MUST run a versioned
  suite, kept in this repository, of **at least 30 cases** covering: order lookups, triage
  explanations, refund/cancel approval interrupts, forbidden tool use, ownership leaks and
  prompt-injection attempts. Every category MUST have at least one case.
- CI MUST fail when the composite score drops below the agreed threshold or regresses by more
  than **5%** against the last recorded run on `main` (ADR 0004). The threshold and the baseline
  are stored in the repository; changing either requires a PR.
- Any change to a prompt, the model id, a tool or the gatekeeper MUST be accompanied by a suite
  run whose result is attached to the PR. Until an LLM-backed agent exists, tools are gated by
  unit and contract tests only; the first LLM story's suite MUST include cases for every tool
  registered by then (TODO(EVAL_SUITE)).
- The verdict suite is introduced by US-09. Until then, LLM-backed steps are gated by
  deterministic gatekeeper unit tests, prompt-injection unit tests, a pinned hash for each prompt
  file and a `live_llm` test. US-09's suite MUST include cases for the triage prompt, the
  gatekeeper and every tool registered by then (TODO(EVAL_SUITE)).
- A failing safety case (forbidden tool use, ownership leak, prompt injection, missing approval
  interrupt) MUST block the merge regardless of the composite score.

### VII. Observability

- Every graph run MUST be traced (LangSmith or OpenTelemetry) with latency, token usage, cost,
  tool calls (name, duration, outcome) and `promptVersion` and `modelId`. Until US-08 adds the
  trace backend, each graph run MUST emit one structured log line carrying latency, input and
  output tokens, `promptVersion`, `modelId` and `orderId` (TODO(TRACING)).
- Trace and log data MUST NOT contain secrets, tokens or unnecessary personal data; tool
  arguments and results are redacted to what is needed for debugging.
- A correlation id (the `orderId` for triage, a request id for the assistant) MUST link logs,
  traces, checkpoints and published events.
- Logs MUST be structured (JSON) with the correlation id on every line.

### VIII. Test Coverage

All agents, tools and messaging code MUST have unit tests.

- Every graph node, tool, parser and consumer handler MUST have at least one test covering the
  happy path and at least one covering an error/edge case (timeout, invalid model output,
  fallback, rejected approval, budget exceeded).
- Unit tests MUST use a fake LLM and fake HTTP transports; the default test run MUST NOT touch
  the network.
- Minimum coverage: **80%** line coverage on the `agents`, `tools` and `messaging` packages,
  enforced with `--cov-fail-under=80` in the gate — the run fails below the threshold.
- A PR that adds or changes behaviour without corresponding tests MUST NOT be merged.

### IX. Testing Strategy

Four test layers are required:

- **Unit tests** — nodes, tools, parsers, the gatekeeper and consumers in isolation, with a fake
  LLM.
- **Integration tests** — repositories, the checkpointer, the dead-letter tables and the unique
  constraints against a real PostgreSQL, and consumers and producers against a real Kafka, via
  Testcontainers or the ecommerce-infra compose stack (no in-memory substitutes).
- **Contract tests** — every MCP tool's input and output schema against the documented endpoint
  in the owning service's README, and every event model against `events.md`.
- **Live-LLM tests** — opt-in, tagged with the `live_llm` marker and excluded from the default
  run.

Tools: pytest, pytest-asyncio, pytest-cov, httpx (`MockTransport` for fakes), Testcontainers
(PostgreSQL, Kafka). `time.sleep` and bare `asyncio.sleep` waits in tests are prohibited;
asynchronous assertions MUST use a polling helper with a timeout.
Test names MUST describe the behaviour, e.g.
`test_should_pause_for_approval_before_cancelling_an_order`.

### X. Branching Strategy

This project follows a **feature-branch workflow** tied to user stories, identical to
ecommerce-notifications.

**Branch naming convention:**
- Feature branches: `feature/US-XX-short-description`
- Bug fixes: `fix/US-XX-short-description`
- Infrastructure: `chore/short-description`

Examples:
- `feature/US-01-mcp-read-tools`
- `feature/US-02-order-triage-worker`

**Rules:**
- `main` is always production-ready — it MUST NOT receive direct commits, except the initial
  repository setup.
- Every user story gets its own branch created from `main` before any implementation begins.
- Each branch maps to exactly one user story.
- PRs MUST reference the user story: "Implements US-01: MCP read tools".
- Branches MUST be deleted after merge.
- Commit messages MUST follow Conventional Commits with a scope:
  `feat(US-01): add product lookup tool`
  `test(US-01): add contract tests for product tools`
  `docs(US-01): add ADR 0001 token passthrough`

**Automated branch creation (MANDATORY):**
When /speckit.specify is invoked for a user story, Claude Code MUST:
1. Ensure the working tree is clean (no uncommitted changes)
2. Checkout main and pull latest: `git checkout main && git pull`
3. Create and checkout the feature branch: `git checkout -b feature/US-XX-short-description`
4. Only then proceed with generating the spec

The branch MUST exist before any spec file is written.
Claude Code MUST NOT generate spec files while on main.

### XI. Build & Code Quality Integrity (NON-NEGOTIABLE)

The project MUST pass all quality checks at all times.

**After every task implementation, Claude Code MUST run in this order, using `uv`:**

1. `uv run ruff check`          — lint MUST report zero violations
2. `uv run ruff format --check` — formatting MUST be consistent
3. `uv run mypy`                — strict type checking MUST report zero errors
4. `uv run pytest --cov`        — unit, integration and contract tests and the coverage gate
   (`--cov-fail-under=80`) MUST pass; live-LLM tests are excluded by default

**Rules:**
- If any of the above fails, Claude Code MUST fix it before moving to the next task.
- A failing gate (lint, format, type or test failure) MUST never be committed to the branch.
- Ruff warnings and mypy errors are blocking — not deferred items.
- Claude Code MUST NOT mark a task as complete until all four checks pass.
- `Any` is banned unless used at the narrowest scope with a written justification.
  `# type: ignore` MUST name the error code and carry a justification comment. Bare `except:`
  and swallowed exceptions are prohibited.

**Tooling:**
- Project and environment management: `uv`
- Linter and formatter: Ruff (at least `E`, `F`, `I`, `B`, `UP`, `ANN`, `S`, `C4`, `SIM`, `RUF`)
- Type checker: mypy (strict); pyright in strict mode is an accepted alternative only through an
  ADR
- Tests: pytest, pytest-asyncio, pytest-cov
- Evals: verdict (Principle VI), run in CI

**Configuration files required:**
- `pyproject.toml` — `requires-python = ">=3.12,<3.13"`; Ruff, mypy strict, pytest markers
  (`live_llm`) and coverage settings configured
- `uv.lock`
- `.python-version` — `3.12`
- `.editorconfig`
- `.gitignore` for Python
- `.env.example` — every setting, with no real secret

## Technology Stack

| Concern | Choice |
|---------|--------|
| Runtime | Python 3.12 (managed by `uv`) |
| Framework | FastAPI + uvicorn, SSE streaming for the assistant |
| Models & settings | Pydantic v2 + pydantic-settings |
| Agents | LangGraph with the PostgreSQL checkpointer |
| Tools | FastMCP |
| LLM | Anthropic API; the model id comes from configuration and MUST NOT be hardcoded |
| Messaging | Kafka via aiokafka (confluent-kafka only through an ADR) |
| Persistence | PostgreSQL 16 |
| HTTP client | httpx (async), calling the platform through the gateway |
| Testing | pytest, pytest-asyncio, pytest-cov, httpx, Testcontainers (PostgreSQL, Kafka) |
| Evals | verdict |
| Observability | LangSmith or OpenTelemetry, structured JSON logs |
| Lint / Format | Ruff |
| Types | mypy (strict) |
| Containerization | Docker (multi-stage `Dockerfile` on a slim `python:3.12` image, non-root user) |
| Local infrastructure | ecommerce-infra (docker compose): service port `8002`; PostgreSQL 16 `localhost:5436` (credentials from environment); Kafka `localhost:9092`; gateway `http://localhost:8090/api`; Identity JWKS at `IDENTITY_JWKS_URL` |

Schema changes MUST be made only through versioned migrations in the codebase; ad-hoc scripts or
manual changes are prohibited. The checkpointer's own tables are created by its documented setup
call at startup, never by hand.
Introducing a new runtime dependency that duplicates a capability already provided by the stack
above requires an ADR in `docs/adr/` before the PR can be merged.

## Documentation Standards

All public modules, classes and functions MUST carry docstrings.
Each top-level package MUST include a module docstring in its `__init__.py` describing its
responsibility.
FastAPI endpoints and models MUST carry descriptions so the generated OpenAPI document is
complete. Every MCP tool MUST carry a description and documented input fields.
Kafka event payloads MUST be documented in the event models (every field) and in
`ecommerce-infra/docs/events.md`, which remains the source of truth.
Architectural decisions MUST be recorded as ADRs in `docs/adr/` using the standard template
(Context / Decision / Consequences). The initial ADRs are:
- `0001` — token passthrough vs service account.
- `0002` — human-in-the-loop for mutating tools.
- `0003` — fallback and cost caps.
- `0004` — evals as release gate.

ADRs are numbered sequentially with a four-digit prefix (`0001-short-title.md`, `0002-...`), are
never renumbered, and are indexed in `docs/adr/README.md`. A superseded ADR is kept and marked as
superseded by the newer one.
The repository MUST keep `README.md`, `CLAUDE.md` and the multi-stage `Dockerfile` up to date with
the current behaviour and this constitution.

## Governance

This constitution supersedes all other project conventions and style guides. When a conflict
arises, the constitution takes precedence.

**Amendment procedure**: Any amendment requires (1) a draft PR updating this file, (2) a version
bump per the policy below, (3) approval from at least one other maintainer, and (4) a migration
note if existing code must change.

**Versioning policy**:
- MAJOR: Removal or redefinition of an existing principle (breaking governance change).
- MINOR: Addition of a new principle or materially expanded guidance.
- PATCH: Clarifications, wording refinements, typo fixes, non-semantic changes.

**Compliance**: Every PR review MUST verify that no constitutional principle is violated.
Reviewers are empowered to block merges for violations without exception. New principles take
effect immediately upon merge.

**Version**: 2.0.0 | **Ratified**: 2026-11-23 | **Last Amended**: 2026-11-24
