# CLAUDE.md

AI microservice: Python 3.12, FastAPI, Pydantic v2, structlog; later LangGraph, FastMCP, aiokafka,
PostgreSQL 16 and the Anthropic API. The source of truth for every rule below is
`.specify/memory/constitution.md`. If this file and the constitution disagree, the constitution
wins.

## Stack

- Python 3.12 **through uv only**: always `uv run ...`. The machine's global Python must not be
  used. Add dependencies with `uv add` / `uv add --dev`, and commit `uv.lock`.
- Runtime now: FastAPI + uvicorn, Pydantic v2 + pydantic-settings, httpx, structlog,
  PyJWT[crypto], FastMCP, aiokafka, SQLAlchemy 2 async + asyncpg, Alembic. Dev: Ruff, mypy strict
  (pydantic plugin), pytest, pytest-asyncio (`asyncio_mode = "auto"`), pytest-cov, respx,
  testcontainers[postgres], LangGraph, anthropic (SDK 2.x, on `httpx2`). aiokafka is untyped:
  only `messaging/kafka.py` imports it; only `infrastructure/llm/` imports `anthropic`.
- HTTP port `8002`. Run: `uv run python -m ecommerce_ai`; schema: `uv run python -m ecommerce_ai
  migrate` (Alembic `upgrade head`, migrations packaged in `infrastructure/db/migrations`,
  hand-written revisions, schema only via migrations). Local infra comes from
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
  `auth.py` (`require_caller`, shared 401/403/503 bodies with `/mcp`), `chat.py` (`/chat` routes,
  `{code, message}` errors, `ChatHttpCode`), `sse.py` (framer: ids from 1, keep-alive,
  `CancelledError` → disconnect), the per-user minute limit (`infrastructure/rate_limit.py::SlidingWindowLimiter`, before DB work), `body_limit.py` (16 KB on `/chat`, 413
  `PAYLOAD_TOO_LARGE`, replaces the gateway's buffering limit; later receives pass through).
- `agents`: graphs, nodes, state, prompts. No HTTP/Kafka/SQL; outside world only via tools or
  `typing.Protocol` ports. Domain decisions (triage cause, suggested action, gatekeeper) live here.
  `agents/triage_intake`: `derive_cause`/`merge_cause` (FR-006), `TriageIntakeService.record`
  (one transaction per event, `processed`/`duplicate`), ports `TriageCaseStore`/`DeadLetterStore`.
  Agents never import messaging (topic names are repeated in `cause.py`, a test keeps them equal).
  `agents/triage`: `graph.py` (LangGraph, no checkpointer: classify → draft → gatekeeper →
  finalize | redraft ≤2 | fallback; draft reserves the case's remaining `CASE_TOKEN_CAP` from the
  daily budget, settles actual tokens, retries `ModelUnavailable` via `call_with_retry`),
  `gatekeeper.py` (pure), `templates.py`, `policy.py`, `service.py` (`triage_one`, one
  `triage_case` log line, `order_id` bound for the run), `worker.py` (`WorkerState`, DB backoff via
  injected `is_transient`), ports `TriageModel`/`TokenBudget`/`TriageCaseQueue`/`Clock`.
- `agents/assistant` (US-05): `graph.py` (`ChatGraph`: guard_input → agent ⇄ tools → gatekeeper →
  finalize, ≤ `CHAT_MAX_TOOL_ROUNDS`, custom events via `get_stream_writer`), `gatekeeper.py`
  (`check_segment`/`check_answer`, `SAFE_MESSAGE`), `segmenter.py`, `summaries.py` (fixed
  `tool.result` phrases), `tool_results.py` (delimited `<tool_result>` blocks), `tool_runner.py`
  (`ToolRunner` over `ToolInvoker`, `run_approved`), `limits.py` (`RunAllowance`, `ActiveStreams`,
  `ProposalQuota`), `service.py` (`ChatService`: checks → stream → store; one `chat_run` log line,
  `user_hash` only), ports `ChatModel`/`ToolRunner`/`ConversationStore`/`Checkpointer`/
  `ApprovalStore`. `agents/safety/rules.py`: pure rules shared by both gatekeepers (incl. the
  cancellation-claim rule). `agents/ports.py`: shared `TokenBudget`/`Clock`.
- US-06 approvals: `approvals.py` (pure: proposal checks, `PENDING` rule, `Refusal`, outcome
  mapping, fixed `APPROVAL_OUTCOME_MESSAGES`); the `tools` node turns `cancel_order` into a checked
  `PendingApproval` (hourly `ProposalQuota` via `RunContext`) or a refusal; the `approval` node
  starts with `interrupt()` (no side effect before it), then re-reads and runs `run_approved` with
  the state's arguments; `ChatGraph.resume`/`close_approval`. `ChatService.decide` records the
  decision atomically before resuming (shielded from disconnect), closes expired/orphaned
  approvals lazily, withdraws on delete; one `chat_approval` log line per proposal and decision.
- `agents/guardrails` (US-07): `Guardrails` bundle (`input_guard`, `tool_result_guard`,
  `output_checks`, `tool_guard`, `canary_factory`, each with a `NOOP`; built by
  `main.py::build_guardrails`), `events.py` (`GuardEvent` stage/rule/action/severity, ports
  `GuardrailEventStore`/`UserUsageStore`/`RateWindow`), `normalizer.py`, `input_rules.py` +
  `rules/input.v1.json` (versioned, SHA-pinned), `output_checks.py` (`LeakChecks`), `tool_guard.py`
  (`CallGuard`: allowlist, seen ids for `cancel_order`, loops), `canary.py`, `quotas.py`
  (`UserQuotas`: cooldown → minute in the API dependency, day in `ChatService.start`; cooldown
  event after the audit). `RunAllowance` reserves the user budget then the global one
  (`denied_scope`); `ActiveStreams` holds one stream per user; 429s carry `scope`.
- `tools`: MCP server and tools, one documented endpoint each. `invoker.py::ToolInvoker` runs a
  tool in process (allowlist, input validation, timeout, error mapping) for `/mcp` and the chat;
  `invoke` refuses mutating tools, only `invoke_approved` runs them (no retry). `tools/mutating/`:
  `Exposure` (`MCP`/`CHAT`), `ApprovalPolicy`, chat-only `cancel_order_tool`; `ToolRegistry.build`
  fails on a mutating tool without policy or exposed on `MCP`; `/mcp` serves
  `registry.exposed(Exposure.MCP)` (still the five read-only tools). No prompts, LLM calls or decisions.
- `messaging`: consumers/producers, dead letters, retry worker. No business logic.
  `contracts/` (frozen event models, `TOPIC_MODELS`), `decoding.py` (bytes → `TriageEventInput` or
  `DecodeFailure` with a reason free of payload values), `kafka.py` (`ConsumerPort`/`ProducerPort`
  + aiokafka adapters), `outbox_relay.py` (`OutboxRelay`, `RelayState`: publish stored payload,
  retry `min(base·2ⁿ, max)` jittered, EXHAUSTED at `OUTBOX_MAX_ATTEMPTS`, producer start and
  transient DB errors back off), `triage_published.py` (result → outbox message), `triage_consumer.py` (decode → intake or dead letter → commit offset+1 → one
  `triage_message` log line; DB retries, reconnect backoff), `consumer_state.py` (for `/ready`).
- `infrastructure/db`: lazy async engine (`DATABASE_URL` → `postgresql+asyncpg`),
  `is_transient_db_error`, Core tables, `TriageRepository` (`ON CONFLICT` + `FOR UPDATE`),
  `DeadLetterRepository`, `ConversationRepository` (every query filters `user_id`; delete removes the
  thread's checkpoints), `PostgresCheckpointSaver` (LangGraph saver, newest checkpoint only),
  `TriageQueueRepository` (`SKIP LOCKED` pick; result + outbox +
  COMPLETED in one transaction), `OutboxRepository`, `TokenBudgetRepository` (conditional upsert
  on `llm_token_usage`), `ApprovalRepository` (approvals + audit; conditional `decide`, every query
  filters `user_id`), `migrate.py`. Tables: `triage_cases`, `triage_evidence`,
  `inbound_dead_letters`, `triage_results`, `outbox_events`, `llm_token_usage`, `chat_conversations`,
  `chat_messages`, `chat_checkpoints`, `chat_checkpoint_writes` (migration 0003), `chat_approvals`
  (cascade with the conversation, one `pending` per conversation) and append-only `chat_actions`
  (audit, trigger-enforced, migration 0004); migration 0005: append-only `guardrail_events` (no
  FK, no content), `chat_user_usage` (per user and UTC day), `chat_conversations.tool_allowlist`;
  `GuardrailEventRepository`, `UserUsageRepository` (conditional token upsert).
- `infrastructure/llm/anthropic_model.py`: `AnthropicTriageModel` (`messages.parse`,
  `output_format=DraftOutput`, `max_retries=0`, per-call timeout, effort; refusal/truncation/schema
  → `ModelOutputInvalid`, API errors → `ModelUnavailable`); `build_anthropic_model(settings)`.
  `anthropic_chat_model.py`: `AnthropicChatModel` (`messages.stream` → `TextDelta`/`ToolUseRequest`/
  `ModelTurnEnd`), `build_anthropic_chat_model(settings, client)` shares the triage client.
- `infrastructure`: settings, logging, probes, HTTP/DB/LLM clients. No business decisions.
- `main.py` is the composition root (`create_app(settings=None, probes=None,
  consumer_factory=None, producer_factory=None, triage_queue=None, outbox_store=None,
  conversation_store=None, chat_model=None, checkpointer=None, tool_runner=None,
  token_budget=None, approval_store=None, clock=None)`): builds
  the engine, repositories and (only when `model_configured`) the Anthropic client; starts the
  consumer (`TRIAGE_CONSUMER_ENABLED`) and the worker + relay (`TRIAGE_WORKER_ENABLED`) tasks in
  the lifespan and stops them (cancel after `CONSUMER_SHUTDOWN_TIMEOUT_SECONDS`) before closing
  the LLM client and disposing the engine.
- Prompts live in versioned files under `agents/prompts/` (`triage.v1.md`, `assistant.v1.md`,
  `assistant.v2.md` (US-06 approval rules), `assistant.v3.md` (calls `cancel_order` directly for
  a named or single clear order), `assistant.v4.md` (active: v3 + per-run `{canary}` slot and
  truncation note), loaded by
  `loader.py::load_prompt`, SHA-256 pinned by a test), immutable once released; never inline
  prompt strings in Python.

## HTTP and security

- Now: `GET /health` (liveness, no dependencies, always 200) and `GET /ready` (TCP probes:
  `database` critical → 503 when down; `kafka` reported only; `consumer`
  `{connected, caughtUp, lastError}`, `triageWorker` `{running, lastError, modelConfigured}`,
  `outboxRelay` `{running, lastError}`, each `null` when disabled, never affecting status;
  `lastError` is a class name only). Probes live in `infrastructure/health.py`; startup never waits for a
  dependency (the consumer connects in its own task, the engine on first use).
- Triage consumer settings: `TRIAGE_CONSUMER_ENABLED`, `KAFKA_CONSUMER_GROUP` (`ai-triage`),
  `KAFKA_RECONNECT_BACKOFF_*`, `DB_RETRY_*`, `DATABASE_TIMEOUT_SECONDS`,
  `CONSUMER_SHUTDOWN_TIMEOUT_SECONDS`. Consumer logs never carry payload, `customerId` or reason.
- Triage worker settings: `TRIAGE_WORKER_ENABLED`, `TRIAGE_SETTLE_SECONDS`,
  `TRIAGE_POLL_INTERVAL_SECONDS`, `TRIAGE_BACKOFF_*`, `ANTHROPIC_API_KEY` (`SecretStr`),
  `ANTHROPIC_MODEL` (no code default; both set = `model_configured`), `LLM_TIMEOUT_SECONDS`,
  `LLM_MAX_RETRIES`, `LLM_MAX_OUTPUT_TOKENS`, `LLM_EFFORT`, `CASE_TOKEN_CAP`,
  `DAILY_TOKEN_BUDGET`, `OUTBOX_*`. Triage, relay and model logs never carry evidence, reasons,
  explanations or prompt text (FR-020).
- `/chat` (US-05, any role via `AUTHENTICATED`): conversations CRUD and
  `POST /chat/conversations/{id}/messages` → SSE (409 `APPROVAL_PENDING` while an approval is
  pending); US-06 `POST /chat/conversations/{id}/approvals/{approvalId}` (`{decision}`, same rate
  and body limits) → SSE continuation; the detail carries `pendingApproval`. Settings
  `APPROVAL_TTL_SECONDS`, `CANCEL_PROPOSALS_PER_HOUR`. US-07 settings: `USER_DAILY_TOKEN_BUDGET`,
  `USER_MESSAGES_PER_MINUTE`/`_PER_DAY`, `TOOL_RESULT_MAX_CHARS`, `GUARD_VIOLATION_THRESHOLD`,
  `GUARD_VIOLATION_WINDOW_SECONDS`, `GUARD_COOLDOWN_SECONDS`, `GUARD_LLM_CLASSIFIER` (no effect);
  guard rows and logs never carry text, tool data, the canary or tokens. Frozen contract `docs/chat-contract.md`, checked
  by `tests/contract/test_chat_contract.py`; change both together. Chat settings: `CHAT_*`
  (keep-alive, per-message token cap, rate limit, tool rounds, max messages); tools reuse
  `TOOL_TIMEOUT_SECONDS`; the budget is the shared `DAILY_TOKEN_BUDGET`. Chat logs never carry
  message text, tool arguments/results or the raw subject (FR-026).
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
- Every route declares access through `openapi_extra`: `PUBLIC` (`api/route_policy.py`),
  `AUTHENTICATED` (any role) or `x-roles`. `assert_route_policies` fails `build_api` for an undeclared route (default deny).
- Protected routes (`/mcp`, `/chat`): RS256 bearer from ecommerce-identity, verified locally against
  `IDENTITY_JWKS_URL` (lazy, cached), same 401 for every bad token, 403 without a role, 503 without
  keys. Tools forward the caller's JWT; never a service account (ADR 0001).
- Mutating tools pause for approval of the exact call (ADR 0002). Every LLM step has a timeout,
  bounded retries, a deterministic fallback and a token budget (ADR 0003).
- Secrets only from the environment (or the git-ignored `.env`); `DATABASE_URL` is a `SecretStr`
  (`postgresql://` or `postgresql+asyncpg://`). Describe failures with
  `infrastructure/errors.py::error_fields` (root cause of a `DBAPIError`, sanitized message). Never log tokens,
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

- `tests/unit/`: no network, no containers; Kafka via `tests/support/kafka.py::FakeConsumer`/
  `FakeProducer` and payload builders, the LLM via `tests/support/llm.py::ScriptedModel` /
  `ScriptedChatModel`, the chat via `tests/support/chat.py` (`FakeToolRunner`, `chat_app`,
  `chat_graph`) and `tests/support/sse.py`, storage
  via `tests/support/stores.py` (in-memory queue, outbox, token budget, conversations; injectable failures).
  `scripts/chat_smoke.py` / `mcp_smoke.py` are manual smoke clients (`TOKEN` env).
- `tests/integration/` (all marked `integration`, run by default and in CI): real PostgreSQL from
  `TEST_DATABASE_URL`, else Testcontainers `postgres:16-alpine`, else skipped
  (`INTEGRATION_REQUIRED=true`, set in CI, fails instead); schema by the real migrations, tables
  truncated per test. Live-model tests get the `live_llm` marker (excluded by default;
  `tests/integration/test_live_llm.py`: triage per cause + one assistant answer, needs
  `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL` in the env). Assistant evals: US-09 (TODO(EVAL_SUITE)).
- `tests/redteam/` (default run): `cases.jsonl` (44, 11 categories) and `legit.jsonl` (25, five
  languages) via `loader.py`; `runner.py` runs a case through `chat_graph` with
  `CompromisedChatModel` and the production guards; `test_guard_coverage.py` swaps each guard for
  its `NOOP` and expects a failure; `test_redteam_live.py` is `live_llm`. Grow the corpus with
  every new finding.
- Drive the app with an httpx `AsyncClient` over `ASGITransport`, building it with
  `create_app(probes=[...])`. `create_app` reconfigures logging, so tests capturing logs call
  `configure_logging("INFO", stream)` after building the app.
- `tests/conftest.py` clears every settings variable from the environment for each test, turns
  off the `.env` file (`Settings` reads `./.env` when present; env vars win) and
  sets `TRIAGE_CONSUMER_ENABLED=false`; consumer tests enable it with a fake `consumer_factory`.
- No `time.sleep` or bare `asyncio.sleep`; use `tests/support/polling.py` (`wait_until`,
  `poll_until`) with a timeout. Inject `wait`/`sleep`/`rng` into `TriageConsumer` instead of waiting.
- Names describe behaviour: `test_should_..._when_...`.

## CI/CD

`ci.yml` (the gate above, unit + integration tests, + actionlint), `security.yml` (gitleaks, pip-audit, CodeQL, Trivy),
`cd.yml` (reuses `ci.yml`, Trivy gate, then pushes to `ghcr.io/<owner>/ecommerce-ai`: `main` and
`sha-*` on `main`, semver tags on `vX.Y.Z`, which must equal `pyproject.toml`'s version). Actions
pinned by SHA, least-privilege permissions; check every workflow change with actionlint. New test
layers (evals) extend `ci.yml` in the story that adds them. The image's entrypoint
(`docker-entrypoint.sh`) runs `migrate` (failure logged, not fatal), then `exec`s the app.

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
  gate, 0005 layered guardrails. Threat model: `docs/threat-model.md` (OWASP LLM mapping).
- Keep `README.md`, this file and the `Dockerfile` up to date with every behaviour change.
- Constitution 2.0.0 (US-04): IV one result per order + outbox relay, `modelId` null on fallback;
  VI interim LLM gate until US-09 (gatekeeper/injection tests, pinned prompt hash, `live_llm`);
  VII one structured log line per graph run until US-08 adds tracing.
- ecommerce-infra has the `ai.order-triage-completed` contract (`docs/events.md`: `cause` and
  `suggestedAction` enums, consumer group `ai-triage`, the HTTP exception for calls with the
  caller's token), the topic in `kafka-init`, `postgres-ai` on `5436` and the `ai` compose service.
  The gateway routes `/api/ai/health` and the `/api/ai/chat/...` routes by allowlist (the stream
  route without the buffering body limit; entrypoint write timeout 300 s); `/mcp` is internal only
  (`http://ai:8002/mcp`), never routed. Follow-up there (US-06): route the approval decision
  endpoint like the message stream.
