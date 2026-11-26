# Tasks: Observability (US-08)

**Input**: [spec.md](spec.md), [plan.md](plan.md) (decisions D1–D12, data model, contract).
**Tests**: requested (spec scope 8). Gate (CLAUDE.md) green and a commit after every phase.

## Phase 1: Setup

- [X] T001 Add `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` with `uv add`; commit `uv.lock` (pyproject.toml)
- [X] T002 [P] Create `config/model_prices.json` (`version`, `models` → `input_per_mtok`/`output_per_mtok` for the models in use) and `COPY config ./config` in Dockerfile
- [X] T003 [P] Add settings `MODEL_PRICES_PATH`, `LANGSMITH_TRACING=false`, `LANGSMITH_HIDE_INPUTS=true`, `LANGSMITH_HIDE_OUTPUTS=true` (OTEL_* stay standard env, read in T006) in src/ecommerce_ai/infrastructure/settings.py; clear them in tests/conftest.py

## Phase 2: Foundational (blocks all stories)

- [X] T004 Create `telemetry` package: `attributes.py` (allowlist key constants + `ALLOWED_KEYS`, D6), `tracing.py` (`use_provider`, `get_tracer`, `span(name, **attrs)` context manager with `record_exception=False`, sets `error.type` = class name, D2/D7), `nodes.py` (`traced_node(name, fn)` for sync/async LangGraph nodes) in src/ecommerce_ai/telemetry/
- [X] T005 Implement `ScrubbingSpanExporter` (rebuild span with allowlisted attributes, drop events/link attributes/status description, scrub resource to `service.*`, catch exporter exceptions → FAILURE, warn by class name at most once per minute, D4) in src/ecommerce_ai/infrastructure/tracing.py
- [X] T006 Implement `build_tracer_provider(env)` in src/ecommerce_ai/infrastructure/tracing.py: `OTEL_TRACES_EXPORTER` none(default)/console/otlp, `OTEL_SDK_DISABLED`, unknown → None + one warning; `BatchSpanProcessor(ScrubbingSpanExporter(...))`; shutdown hook (D3, D5)
- [X] T007 Wire in `create_app(..., tracer_provider=None)`: build provider, `use_provider`, flush/shutdown in lifespan after workers stop, in src/ecommerce_ai/main.py
- [X] T008 [P] Test support: `tests/support/tracing.py` (`InMemorySpanExporter` provider fixture through the scrubber, `spans_by_name`, `assert_parent`)
- [X] T009 [P] Unit tests: scrubber drops non-allowlisted attributes, events, status descriptions; failing exporter returns FAILURE without raising; default/none/disabled/unknown exporter → no-op in tests/unit/telemetry/test_tracing_setup.py

## Phase 3: US1 – Trace every chat and triage run (P1) 🎯 MVP

Independent test: chat with a tool call + approval pause and one triage case produce the expected span tree (plan D7).

- [X] T010 [US1] `http.request` span (`http.route`, `http.response.status_code`, `request.id`) in src/ecommerce_ai/api/middleware.py::RequestIdMiddleware
- [X] T011 [US1] Chat: `chat.run`/`chat.resume` spans (`conversation.id`, `user.hash`, `request.id`, `prompt.version`) in src/ecommerce_ai/agents/assistant/service.py; wrap every node with `traced_node` and add `approval.pause`/`approval.resume` spans in src/ecommerce_ai/agents/assistant/graph.py
- [X] T012 [US1] `llm.call` spans (model, tokens, finish reason, `latency.ms`, `cost.usd` once US3 lands, `llm.outcome`) around model calls in src/ecommerce_ai/agents/assistant/graph.py and src/ecommerce_ai/agents/triage/graph.py
- [X] T013 [P] [US1] `tool.call` span (`tool.name`, `tool.outcome`) in src/ecommerce_ai/tools/invoker.py; `upstream.http` span (`http.response.status_code`, `error.type`) in src/ecommerce_ai/infrastructure/upstream.py
- [X] T014 [US1] Triage: `triage.run` (`order.id`, `case.id`, `triage.source`, `triage.revisions`) in src/ecommerce_ai/agents/triage/service.py; `traced_node` for classify/draft/gatekeeper/finalize/fallback in src/ecommerce_ai/agents/triage/graph.py
- [X] T015 [US1] `triage.consume` span in src/ecommerce_ai/messaging/triage_consumer.py; `outbox.publish` span + Kafka headers `correlation-id: <orderId>` and `traceparent` (add `headers` to `ProducerPort.send` and the aiokafka adapter) in src/ecommerce_ai/messaging/kafka.py, src/ecommerce_ai/messaging/outbox_relay.py, tests/support/kafka.py::FakeProducer
- [X] T016 [P] [US1] Span-tree tests: chat run with tool call and approval pause (names, parents, `request.id` everywhere) in tests/unit/telemetry/test_chat_spans.py
- [X] T017 [P] [US1] Triage span-tree test + published message has `correlation-id` header and unchanged payload in tests/unit/telemetry/test_triage_spans.py
- [X] T018 [P] [US1] Exporter down (OTLP to `http://localhost:1`) and raising exporter: chat responses identical to no-op, in tests/unit/telemetry/test_exporter_failure.py

## Phase 4: US2 – Privacy allowlist (P1)

Independent test: conversation with fake token, email and secret leaves no trace in spans or logs.

- [X] T019 [US2] Test: chat run (and tool call with fake email argument) containing `sk-ant-FAKE…`, `bearer eyJ…`, `jane@example.com`, `secret-XYZ`; assert none appear in any exported span attribute/name/event or captured log line; system prompt text absent, in tests/unit/telemetry/test_privacy.py
- [X] T020 [US2] Fix any leak found by T019 at its source (no content attributes, error.type only) in the files touched by Phase 3

## Phase 5: US3 – Per-call usage and cost records (P2)

Independent test: ok/timeout/error/budget/fallback each produce one row with the right outcome and cost.

- [X] T021 [US3] `ModelPrices` (Pydantic, load from `MODEL_PRICES_PATH`, startup error when missing/invalid) + `estimate_cost(model_id, in, out) -> Decimal | None` warning once per unknown model in src/ecommerce_ai/infrastructure/pricing.py
- [X] T022 [US3] Migration 0006 `llm_calls` (plan data model) in src/ecommerce_ai/infrastructure/db/migrations/versions/0006_llm_calls.py and Core table in src/ecommerce_ai/infrastructure/db/tables.py
- [X] T023 [US3] `LlmCall` model, `LlmCallOutcome`/`LlmCallKind` StrEnums and `LlmCallRecorder` port in src/ecommerce_ai/agents/ports.py; `LlmCallRepository` (insert, cost via pricing) in src/ecommerce_ai/infrastructure/db/llm_call_repository.py; in-memory recorder in tests/support/stores.py (cost is priced in the agent via a `CostEstimator` port so the span and the row agree; the repository only inserts)
- [X] T024 [US3] Record calls in triage `_draft` (ok/timeout/error/budget) and one `fallback` row per fallback run in src/ecommerce_ai/agents/triage/graph.py; chat `agent` node (ok/timeout/error) and budget denial in src/ecommerce_ai/agents/assistant/graph.py + service.py; fail open (log class name); inject via src/ecommerce_ai/main.py (`llm_call_recorder=None`)
- [X] T025 [P] [US3] Unit tests: cost with known prices, unknown model → None + one warning, missing file → error in tests/unit/test_pricing.py; outcome rows for ok/timeout/error/budget/fallback and recorder failure not affecting results in tests/unit/test_llm_call_recording.py
- [X] T026 [P] [US3] Integration: repository insert + constraints in tests/integration/test_llm_calls.py

## Phase 6: US4 – Admin usage summary (P2)

Independent test: ADMIN 200 with aggregates, CUSTOMER 403, no token 401, bad range 422.

- [X] T027 [US4] `UsageSummaryRepository.summarize(from, to, group_by)` (SQL `percentile_cont`, `FILTER`, guardrail `blocked` counts per day/totals) in src/ecommerce_ai/infrastructure/db/usage_summary_repository.py
- [X] T028 [US4] `GET /usage/summary` router (query validation: dates, `from ≤ to`, ≤ 90 days, default last 7, `groupBy`; frozen camelCase response models; OpenAPI descriptions; `x-roles: [ADMIN]`) in src/ecommerce_ai/api/usage.py; register in src/ecommerce_ai/api/app.py and src/ecommerce_ai/main.py
- [X] T029 [P] [US4] Unit tests: 401/403/422/200 with a fake summary store in tests/unit/test_usage_api.py
- [X] T030 [P] [US4] Integration: seeded rows → grouped calls, tokens, cost, p50/p95, error/fallback rates, guardrail blocks; empty range → zero/null rates in tests/integration/test_usage_summary.py

## Phase 7: US5 – Optional LangSmith (P3)

- [X] T031 [US5] `configure_langsmith(settings)` writing `LANGSMITH_TRACING`/`LANGCHAIN_TRACING_V2` and, when on, `LANGCHAIN_HIDE_INPUTS/OUTPUTS` in src/ecommerce_ai/infrastructure/langsmith.py; call it in src/ecommerce_ai/main.py
- [X] T032 [P] [US5] Tests: default (even with a stray `LANGSMITH_TRACING=true` env) → tracing disabled and no client created; enabled → hide inputs/outputs true, in tests/unit/test_langsmith.py

## Phase 8: Polish

- [X] T033 [P] docs/observability.md (span trees, attribute list, console + OTLP run, reading the summary, privacy rules)
- [X] T034 [P] docs/adr/0006-vendor-neutral-tracing.md + index in docs/adr/README.md; mark TODO(TRACING) done in .specify/memory/constitution.md (VII)
- [X] T035 README.md (settings, LangSmith privacy note, usage endpoint), CLAUDE.md (telemetry layer, llm_calls, migration 0006, `/usage/summary`, gateway follow-up)
- [X] T036 Run the full gate and quickstart checks from plan.md

## Dependencies

Setup → Foundational → US1 → US2 (tests the spans US1 adds). US3 needs Foundational only (T012 adds `cost.usd` after T021). US4 needs T022. US5 independent after T003. Polish last.

## Parallel examples

- Phase 2: T008 ∥ T009 after T004–T006.
- US1: T013 ∥ T014; tests T016 ∥ T017 ∥ T018.
- US3 ∥ US5 once Phase 2 is done; T025 ∥ T026.

## Strategy

MVP = Phases 1–4 (tracing + privacy). Then US3 → US4 → US5, committing after each phase.
