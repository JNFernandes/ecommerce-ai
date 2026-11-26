# Implementation Plan: Observability (US-08)

**Branch**: `feature/US-08-observability` | **Date**: 2026-11-26 | **Spec**: [spec.md](spec.md)

## Summary

OpenTelemetry spans (manual, no auto-instrumentation) behind a scrubbing exporter wrapper with an
attribute allowlist; one `llm_calls` row per model call (migration 0006) with cost from
`config/model_prices.json`; ADMIN-only usage summary; LangSmith off by default with hidden I/O.
Small story: research, data model and contract are folded into this file.

## Technical Context

**Language/Version**: Python 3.12 (uv) · **New deps**: `opentelemetry-api`, `opentelemetry-sdk`,
`opentelemetry-exporter-otlp-proto-http` (HTTP, no grpc wheels) · **Storage**: PostgreSQL 16,
migration 0006 · **Testing**: pytest, `InMemorySpanExporter`, testcontainers for repo/summary ·
**Constraints**: zero request latency from export; nothing leaves the process by default.

## Constitution Check

- I Layering: tracing helpers in `ecommerce_ai/telemetry/` (API-only, like structlog: cross-cutting,
  no I/O); SDK/exporter setup in `infrastructure/tracing.py`; usage storage behind an
  `LlmCallRecorder` port. ✅
- II Security / VII Observability: allowlist scrubber, no content, error type = class name,
  `record_exception=False`; correlation id per VII. Closes TODO(TRACING). ✅
- IV Cost control: per-call cost records; recording fails open. ✅
- V Event contracts: payload unchanged; only Kafka headers added. ✅
- XI Gate: unchanged. No violations → no complexity tracking.

## Decisions (research)

1. **Manual spans, not FastAPI/httpx auto-instrumentation** — full control over names and
   attributes; auto-instrumentation adds URLs/headers we'd only scrub again.
2. **Own provider, not the OTel global** — `telemetry.tracing.use_provider(p)` sets a module
   reference read by `get_tracer()`; tests swap in an in-memory provider per test (the OTel global
   can be set only once). Default: no provider → OTel API no-op tracer.
3. **Exporter selection** — `OTEL_TRACES_EXPORTER` read by us, default `none` (SDK default is otlp);
   `console` → `ConsoleSpanExporter`, `otlp` → OTLP/HTTP (endpoint etc. from standard `OTEL_*`);
   `OTEL_SDK_DISABLED=true` or unknown value → no-op + one warning.
4. **Scrubber = exporter wrapper** (`ScrubbingSpanExporter`): ReadableSpan is immutable in
   `on_end`, so the wrapper rebuilds each span with allowlisted attributes, drops events and links'
   attributes and the status description, then delegates; it catches every exporter exception
   (returns FAILURE, logs class name once per minute). Also applied to resource attributes
   except `service.name`/`service.version`.
5. **Fail open / bounded** — `BatchSpanProcessor` (background thread, `OTEL_BSP_MAX_QUEUE_SIZE`
   default 2048, drops when full), export timeout from `OTEL_EXPORTER_OTLP_TIMEOUT`.
6. **Allowlist keys** (constants in `telemetry/attributes.py`): `request.id`, `http.route`,
   `http.response.status_code`, `conversation.id`, `case.id`, `order.id`, `user.hash`,
   `node.name`, `tool.name`, `tool.outcome`, `gen_ai.request.model`, `prompt.version`,
   `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `latency.ms`, `cost.usd`,
   `gen_ai.response.finish_reason`, `guard.rules`, `guard.actions`, `triage.revisions`,
   `triage.source`, `error.type`, `llm.outcome`, `messaging.destination`.
7. **Span names**: `http.request`, `chat.run`, `chat.resume`, `node.<name>`, `llm.call`,
   `tool.call`, `upstream.http`, `approval.pause`, `approval.resume`, `triage.run`,
   `triage.consume`, `outbox.publish`. Graph nodes are wrapped by a `traced_node(name, fn)`
   helper at graph build time (both graphs), so node code is untouched.
8. **Correlation** — request id already bound by `RequestIdMiddleware` and forwarded by
   `UpstreamTransport`; set as `request.id` on `http.request` and `chat.run`. Triage: `order.id`
   on every triage span and log line (bound in `triage_one`, already). Kafka: `ProducerPort.send`
   gains `headers`; relay sends `correlation-id: <orderId>` (and `traceparent` when a span is
   active). Consumer reads nothing new.
9. **Usage recording** — `LlmCallRecorder` port (`agents/ports.py`), `LlmCallRepository`
   (insert only). Triage `_draft` and chat `agent` node record after each call: `ok`,
   `timeout` (`ModelUnavailable` caused by timeout), `error`, `budget` (reservation denied, 0
   tokens, no call), `fallback` (one row when a triage run ends on the template). Recorder errors
   are logged by class name and swallowed only after logging (fail open, FR-011).
10. **Prices** — `config/model_prices.json` `{"version": 1, "models": {id: {"input_per_mtok",
    "output_per_mtok"}}}` validated by Pydantic; path setting `MODEL_PRICES_PATH` (default
    `config/model_prices.json`); invalid/missing → startup error; Dockerfile copies `config/`.
    `Decimal` math; unknown model → `None` + one warning per model id.
11. **Usage summary path** — service route `GET /usage/summary` (gateway prefix `/api/ai` gives
    the requested `/api/ai/usage/summary`, like `/chat`), `x-roles: [ADMIN]` via `require_caller`.
    Aggregation in SQL (`percentile_cont`, `FILTER`). Gateway route = ecommerce-infra follow-up.
12. **LangSmith** — settings `LANGSMITH_TRACING=false`, `LANGSMITH_HIDE_INPUTS=true`,
    `LANGSMITH_HIDE_OUTPUTS=true`; `configure_langsmith(settings)` at startup writes
    `LANGSMITH_TRACING`/`LANGCHAIN_TRACING_V2` (false when off) and, when on,
    `LANGCHAIN_HIDE_INPUTS/OUTPUTS` into `os.environ` (overriding a stray env var when off).
    Test: default → `langsmith.utils.tracing_is_enabled()` is false and no client is created.

## Data model (migration 0006)

`llm_calls`: `id uuid pk`, `kind text check (chat|triage)`, `conversation_id uuid null`,
`case_id uuid null` (check exactly one set), `request_id text null`, `model_id text null`,
`prompt_version text not null`, `input_tokens int ≥0`, `output_tokens int ≥0`,
`estimated_cost_usd numeric(12,6) null`, `latency_ms int ≥0`,
`outcome text check (ok|timeout|error|budget|fallback)`, `created_at timestamptz default now()`.
Indexes: `(created_at)`, `(model_id, created_at)`. No FK (records outlive deleted conversations).

## Contract: `GET /usage/summary`

Query: `from`, `to` (ISO dates, UTC, inclusive; default last 7 days; `from ≤ to`, span ≤ 90 days),
`groupBy` (`day`|`model`|`kind`, default `day`). 401 no/bad token, 403 not ADMIN, 422 invalid.
200 body (camelCase, frozen models):
`{from, to, groupBy, groups: [{key, calls, inputTokens, outputTokens, estimatedCostUsd,
latencyP50Ms, latencyP95Ms, errorRate, fallbackRate, guardrailBlocks}], totals: {…same…}}`.
Rates = share of calls (`timeout`+`error` for error rate); null when `calls = 0`.
`guardrailBlocks` = `guardrail_events` with action `blocked` in the group (day and totals; null
for `model`/`kind`). Cost sums ignore nulls; `estimatedCostUsd` as a string decimal.

## Project Structure

```text
config/model_prices.json
src/ecommerce_ai/telemetry/{__init__,attributes,tracing,nodes}.py     # API-only helpers
src/ecommerce_ai/infrastructure/tracing.py                            # SDK setup, ScrubbingSpanExporter
src/ecommerce_ai/infrastructure/langsmith.py                          # env configuration
src/ecommerce_ai/infrastructure/pricing.py                            # price file + cost
src/ecommerce_ai/infrastructure/db/{llm_call_repository,usage_summary_repository}.py
src/ecommerce_ai/infrastructure/db/migrations/versions/0006_llm_calls.py
src/ecommerce_ai/api/usage.py                                         # /usage/summary
touch: api/middleware.py, agents/{triage,assistant}/graph.py + service.py, agents/ports.py,
  tools/invoker.py, infrastructure/upstream.py, messaging/{kafka,outbox_relay,triage_consumer}.py,
  infrastructure/settings.py, main.py, Dockerfile
tests/unit/telemetry/…, tests/integration/test_llm_calls.py, test_usage_summary.py
docs/observability.md, docs/adr/0006-vendor-neutral-tracing.md, README.md, CLAUDE.md
```

## Validation (quickstart)

- `OTEL_TRACES_EXPORTER=console uv run python -m ecommerce_ai` + `scripts/chat_smoke.py` → span
  tree on stdout, no message text.
- `OTEL_TRACES_EXPORTER=otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:1` → chat still works.
- `curl -H "Authorization: Bearer $ADMIN_TOKEN" localhost:8002/usage/summary?groupBy=model`.
- Gate (CLAUDE.md) green.
