# Feature Specification: Observability (US-08)

**Feature Branch**: `feature/US-08-observability`

**Created**: 2026-11-26

**Status**: Draft

**Input**: User description: "US-08 Observability: traces, per-call usage records with cost, an admin usage summary and optional LangSmith, never recording message content or secrets."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Trace every chat and triage run (Priority: P1)

As the service owner I can follow one chat run or triage run as a tree of steps (request, run, each graph node, each model call, each tool call, outbound platform calls, approval pause/resume, outbox publish, Kafka consume) with durations, tokens and outcome, so I can debug a slow or failing run.

**Why this priority**: Closes constitution VII TODO(TRACING); everything else builds on it.

**Independent Test**: Run a chat with a tool call and an approval pause, and one triage case, against an in-memory exporter; assert span names, parents and attributes.

**Acceptance Scenarios**:

1. **Given** default settings, **When** the service runs, **Then** no trace data leaves the process.
2. **Given** the console exporter, **When** a chat run completes, **Then** one trace shows request → chat run → nodes → model/tool calls, with the request id on each.
3. **Given** a triage case, **When** it is triaged and published, **Then** the consume, graph nodes, model call and outbox publish carry the `orderId`, and the published event carries a correlation header while its payload is unchanged.
4. **Given** the exporter endpoint is down or misconfigured, **When** requests run, **Then** responses and latency are unaffected and buffered spans are bounded (dropped, not queued forever).

---

### User Story 2 - Privacy allowlist on all telemetry (Priority: P1)

As the service owner I know traces and logs never hold message content, tool arguments with personal data, tool results, tokens, authorization headers or the system prompt, because only allowlisted attributes are ever kept.

**Why this priority**: Security-first (constitution II, VII); tracing without it is a data leak.

**Independent Test**: Run a conversation containing a fake bearer token, email and secret; assert none appear in any exported span or log line; set a non-allowlisted attribute and assert it is dropped.

**Acceptance Scenarios**:

1. **Given** any span, **When** it is exported, **Then** every attribute is in the allowlist (FR-004); others are removed.
2. **Given** a message with a fake token/email/secret, **When** the run is traced, **Then** none of those strings appear in spans or logs.

---

### User Story 3 - Per-call usage and cost records (Priority: P2)

As the service owner every model call (successful or not) is recorded with kind, ids, model, prompt version, tokens, estimated cost, latency and outcome, so I can control cost and compare prompt/model versions.

**Independent Test**: Drive model calls that succeed, time out, error, hit the budget and fall back; assert one record each with the right outcome and cost.

**Acceptance Scenarios**:

1. **Given** a known model price, **When** a call uses N input and M output tokens, **Then** its cost is N·in/1e6 + M·out/1e6 USD.
2. **Given** a model missing from the price file, **When** calls are recorded, **Then** cost is empty and a single warning is logged per model.
3. **Given** a timed-out call, **When** it fails, **Then** a record with outcome `timeout` exists.

---

### User Story 4 - Admin usage summary (Priority: P2)

As an administrator I request a usage summary for a date range grouped by day, model or kind, and see calls, tokens, estimated cost, p50/p95 latency, error rate, fallback rate and guardrail block counts.

**Independent Test**: Seed records and guardrail events; call the endpoint as ADMIN, CUSTOMER and anonymous.

**Acceptance Scenarios**:

1. **Given** an ADMIN, **When** they request a valid range, **Then** they get the aggregated groups.
2. **Given** a CUSTOMER, **Then** 403; **Given** no token, **Then** 401.
3. **Given** a range over 90 days, `from` after `to` or an unknown `groupBy`, **Then** a validation error (4xx) with no data.

---

### User Story 5 - Optional LangSmith without content (Priority: P3)

As the service owner I may turn on LangSmith; it is off by default and, when on, inputs and outputs stay hidden unless explicitly changed.

**Independent Test**: With defaults, run a chat and assert no LangSmith traffic; with tracing on, assert hide-inputs/outputs are set.

**Acceptance Scenarios**:

1. **Given** defaults, **Then** nothing is sent to LangSmith.
2. **Given** LangSmith enabled without further settings, **Then** inputs and outputs are hidden.

### Edge Cases

- Exporter raises or blocks: spans dropped, request completes, no error logs per request flood (rate-limited/once).
- Malformed `OTEL_*` values: service starts with tracing no-op and logs one warning.
- Price file missing or invalid: startup fails (it is a packaged, versioned file) — not a silent zero cost.
- Model call that never starts because the budget is exhausted: recorded with outcome `budget`, zero tokens.
- Empty date range result: summary returns empty groups, rates as null/0 without division errors.
- Client disconnect mid-stream: spans still end with an error type, usage recorded for the call made.
- Recording a usage row fails: the run continues (fail open), the failure is logged by class name.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The service MUST produce traces using a vendor-neutral standard, configured only by the standard `OTEL_*` environment variables; default exporter is none (no-op); `console` and `otlp` MUST be supported.
- **FR-002**: Spans MUST cover: HTTP request, chat run, each chat and triage graph node, each model call, each tool call, each outbound platform HTTP call, approval pause and resume, outbox publish and triage Kafka consume, with correct parent/child nesting.
- **FR-003**: Export MUST be asynchronous with a bounded queue; exporter failure or misconfiguration MUST NOT fail requests or add request latency.
- **FR-004**: Only these span attributes MAY be kept: request id, route, status code, conversation id, case id, order id, hashed user id, node name, tool name, tool outcome ok/error, model id, prompt version, input/output tokens, latency, estimated cost, finish reason, guardrail rule names and actions, revisions, source (LLM | FALLBACK_TEMPLATE), error type (class name). A scrubbing step MUST drop any other attribute, including those added by third-party instrumentation; span events and exception messages MUST NOT carry content.
- **FR-005**: Message content, tool arguments with personal data, tool results, tokens, authorization headers and the system prompt MUST NOT appear in spans or logs.
- **FR-006**: The request id MUST appear in logs, spans and outgoing platform calls (as `X-Request-Id`); for triage the `orderId` MUST be the correlation id in logs and spans, and the published `ai.order-triage-completed` message MUST carry a correlation header with the payload contract unchanged.
- **FR-007**: Every model call, including failed ones, MUST produce one usage record: id, kind (chat|triage), conversation id or case id, request id, model id, prompt version, input tokens, output tokens, estimated cost USD, latency ms, outcome (ok|timeout|error|budget|fallback), created at (UTC). Schema change via migration 0006.
- **FR-008**: Prices MUST come from a versioned data file (`config/model_prices.json`, USD per million input and output tokens), never from code; an unknown model MUST record a null cost and log one warning per model per process.
- **FR-009**: `GET /ai/usage/summary?from=&to=&groupBy=day|model|kind` MUST be ADMIN only (CUSTOMER 403, no token 401), validate the range (`from` ≤ `to`, at most 90 days) and return per group: calls, input/output tokens, estimated cost, p50 and p95 latency, error rate, fallback rate and guardrail block count; documented in OpenAPI.
- **FR-010**: LangSmith tracing MUST be off by default; when enabled, inputs and outputs MUST be hidden by default; the README MUST say so.
- **FR-011**: Usage recording and tracing MUST fail open: a failure never changes the chat or triage outcome.
- **FR-012**: Deliver `docs/observability.md` (span trees, attribute list, console and OTLP setup, reading the summary, privacy rules), ADR 0006 "vendor-neutral tracing with a privacy allowlist", and README/CLAUDE.md updates.

### Key Entities

- **LLM call record**: one model invocation; attributes per FR-007; links to a conversation or triage case by opaque id; append-only.
- **Model price list**: versioned file mapping model id → input and output USD per million tokens.
- **Usage summary group**: aggregation key (day, model or kind) with the metrics in FR-009.
- **Guardrail event** (existing, US-07): source of block counts for the summary.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of chat and triage runs in tests produce a complete trace with the expected span names and parents.
- **SC-002**: 0 occurrences of seeded fake token, email or secret in exported spans or logs.
- **SC-003**: 100% of model calls (ok, timeout, error, budget, fallback) produce exactly one usage record.
- **SC-004**: With the exporter unreachable, response status codes and bodies are identical to the no-op case and no request waits on export.
- **SC-005**: An administrator gets cost and latency percentiles for any ≤ 90-day window in one request.
- **SC-006**: With default settings, zero bytes of telemetry leave the process.

## Assumptions

- ADMIN is an existing role in ecommerce-identity tokens; the route is declared with `x-roles: [ADMIN]`.
- The gateway route for `/ai/usage/summary` is an ecommerce-infra follow-up, out of scope here.
- Fallback outcome is recorded as one row per run that ended on the template (no model call needed); budget refusals likewise record a zero-token row.
- Dates are UTC days; `to` is inclusive; default range when omitted: last 7 days.
- Latency percentiles are computed in the database over the selected rows.
- Usage records are kept indefinitely for now; retention is a later chore.
- Out of scope: collector/Jaeger/Grafana, dashboards, alerting, metrics endpoint, evals (US-09), frontend.
