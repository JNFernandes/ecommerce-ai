# Observability (US-08)

Traces (OpenTelemetry), one usage row per model call (`llm_calls`), an ADMIN usage summary and an
optional LangSmith. Nothing leaves the process by default, and no message text, prompt, tool
argument or result, token or secret is ever recorded. Decision: [ADR 0006](adr/0006-vendor-neutral-tracing.md).

## Turning tracing on

| Variable | Default | Effect |
|---|---|---|
| `OTEL_TRACES_EXPORTER` | `none` | `none`, `console` (spans as JSON on stdout) or `otlp` (OTLP over HTTP) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `_TRACES_ENDPOINT` | SDK default | Collector URL for `otlp` (e.g. `http://localhost:4318`) |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | SDK default | Export timeout |
| `OTEL_BSP_MAX_QUEUE_SIZE` | `2048` | Spans buffered for export; more are dropped, requests never wait |
| `OTEL_SERVICE_NAME` | `ecommerce-ai` | `service.name` on the resource (the only resource keys kept: `service.name`, `service.version`) |
| `OTEL_SDK_DISABLED` | `false` | `true` turns tracing off whatever the exporter |

An unknown exporter name turns tracing off with one `tracing_disabled` warning. A failing or
unreachable exporter never affects a response: the export fails, a warning naming the error class
is logged at most once a minute, and the spans are dropped.

```sh
OTEL_TRACES_EXPORTER=console uv run python -m ecommerce_ai     # then: uv run python scripts/chat_smoke.py
OTEL_TRACES_EXPORTER=otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 uv run python -m ecommerce_ai
```

## Span trees

Chat message (`POST /chat/conversations/{id}/messages`) with one read-only tool round:

```text
http.request                       request.id, http.route, http.response.status_code
└─ chat.run                        request.id, conversation.id, user.hash, prompt.version
   ├─ node.guard_input
   ├─ node.agent
   │  └─ llm.call                  gen_ai.request.model, tokens, finish reason, latency.ms, cost.usd, llm.outcome
   ├─ node.tools
   │  └─ tool.call                 tool.name, tool.outcome
   │     └─ upstream.http          http.response.status_code, error.type (one per attempt)
   ├─ node.agent
   │  └─ llm.call
   ├─ node.gatekeeper
   └─ node.finalize
```

A proposed `cancel_order` goes `node.tools` → `node.approval`, which ends the run with an
`approval.pause` span (`tool.name`) in the same trace. The decision
(`POST .../approvals/{approvalId}`) is a new trace: `http.request` → `chat.resume` →
`node.approval` → `approval.resume` (→ `tool.call` when approved), then `node.agent` …
`node.finalize`.

Triage, three independent roots joined by `order.id`:

```text
triage.consume      messaging.destination, error.type     (Kafka consumer, one per record)
triage.run          order.id, case.id, triage.source, triage.revisions   (worker, one per case)
├─ node.classify
├─ node.draft
│  └─ llm.call      as above (llm.outcome=budget, latency.ms=0 when the budget refused the call)
├─ node.gatekeeper  (draft → gatekeeper repeats on a redraft, at most twice)
└─ node.finalize | node.fallback
outbox.publish      order.id, messaging.destination          (relay, one per attempt)
```

A reader leaving is never an error: `chat.run`/`chat.resume` end `OK` with
`stream.client_disconnected=true` when the client leaves after `message.end`, and `UNSET` with
`chat.aborted=true` when it aborts mid-run (no span is marked by a cancellation, and no
`error`/`timeout` usage row is written). Only real exceptions give `ERROR` + `error.type`.

`outbox.publish` sends the Kafka headers `correlation-id: <orderId>` and `traceparent`; the
payload is unchanged. A failed span carries status `ERROR` and `error.type` (a class name) only:
exceptions are never recorded, status descriptions and events are dropped.

## Attribute allowlist

`ScrubbingSpanExporter` keeps only these keys (`telemetry/attributes.py::ALLOWED_KEYS`); anything
else is dropped at export, so a new attribute must be added there on purpose:

`request.id`, `http.route`, `http.response.status_code`, `conversation.id`, `case.id`, `order.id`,
`user.hash`, `node.name`, `tool.name`, `tool.outcome`, `gen_ai.request.model`, `prompt.version`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `latency.ms`, `cost.usd`,
`gen_ai.response.finish_reason`, `guard.rules`, `guard.actions` (reserved), `triage.revisions`,
`triage.source`, `error.type`, `llm.outcome`, `messaging.destination`,
`stream.client_disconnected`, `chat.aborted`.

`user.hash` is the same truncated SHA-256 of the token subject the chat logs use; the raw subject
never appears.

## Usage records and cost

Every model call writes one `llm_calls` row (migration 0006): `kind` (`chat`/`triage`),
`conversation_id` or `case_id`, `request_id`, `model_id`, `prompt_version`, input/output tokens,
`estimated_cost_usd`, `latency_ms`, `outcome` (`ok`, `timeout`, `error`, `budget` (refused by the
token budget, no call), `fallback` (one per triage run that ended on the template)). No content,
no foreign key (rows outlive deleted conversations). Recording fails open: an insert error is
logged by class name and the answer is unaffected.

Cost = tokens × price from `MODEL_PRICES_PATH` (default `config/model_prices.json`, list prices
per million tokens, `Decimal` math). A missing or invalid file stops startup; a model missing from
it records `null` cost with one warning per model id. Update the file when prices change.

## Usage summary

`GET /usage/summary` (`ADMIN` only; through the gateway `/api/ai/usage/summary`):

| Query | Default | Rule |
|---|---|---|
| `from`, `to` | the last 7 UTC days | ISO dates, inclusive, `from ≤ to`, at most 90 days |
| `groupBy` | `day` | `day`, `model` or `kind` |

```sh
curl -H "Authorization: Bearer $ADMIN_TOKEN" "localhost:8002/usage/summary?groupBy=model&from=2026-11-20"
```

Each group and the `totals` carry `calls`, `inputTokens`, `outputTokens`, `estimatedCostUsd`
(decimal string, unpriced calls count as zero), `latencyP50Ms`/`latencyP95Ms` (calls that reached
the model), `errorRate` (`timeout` + `error`), `fallbackRate` (null without calls) and
`guardrailBlocks` (blocked guardrail events; null for `model`/`kind`). `401` without a valid
token, `403` without `ADMIN`, `422` for an invalid range.

## LangSmith (optional)

Off by default: at startup `configure_langsmith` writes `LANGSMITH_TRACING(_V2)` and
`LANGCHAIN_TRACING(_V2)` from `LANGSMITH_TRACING` (default `false`), overriding any stray value,
and the hide flags from `LANGSMITH_HIDE_INPUTS`/`LANGSMITH_HIDE_OUTPUTS` (default `true`). Turning
it on (with `LANGSMITH_API_KEY`) sends run metadata to LangSmith with inputs and outputs hidden;
setting a hide flag to `false` would send message text and tool data off the platform, which the
privacy rules below forbid in production.

## Privacy rules

- Spans and `llm_calls` rows hold identifiers, counts, durations, outcomes and class names only.
- Never add message text, prompts, tool arguments or results, model output, tokens, emails or the
  raw user subject to a span, row or log line; `tests/unit/telemetry/test_privacy.py` checks a run
  seeded with a fake API key, bearer token, email and secret.
- Errors are described by `error.type` (the class name), never by the exception message.
