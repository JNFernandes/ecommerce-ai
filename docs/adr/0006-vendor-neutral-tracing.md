# 0006. Vendor-neutral tracing behind an attribute allowlist

- **Status**: Accepted
- **Date**: 2026-11-26

## Context

Constitution VII requires every graph run to be traced with latency, tokens, cost, tool calls,
`promptVersion` and `modelId`, and forbids secrets, tokens and unnecessary personal data in trace
data. Until US-08 each run left only one structured log line. The options were LangSmith (built
into LangGraph, a hosted SaaS that records run inputs and outputs by default) and OpenTelemetry
(vendor-neutral, any backend through OTLP). Chat runs carry customer messages, tool results with
personal data and the system prompt; none of it may leave the process. Telemetry must never slow
or break a request, and tests must be able to capture spans per test.

## Decision

We will trace with OpenTelemetry, using manual spans only (no FastAPI/httpx auto-instrumentation,
which would add URLs and headers). Spans go through `ScrubbingSpanExporter`, which rebuilds every
span with only the allowlisted attribute keys in `telemetry/attributes.py` (identifiers, counts,
durations, outcomes, class names), drops events, link attributes and status descriptions, keeps
only `service.name`/`service.version` on the resource, and turns any exporter exception into a
logged failure. Exceptions are never recorded; `error.type` holds the class name. The exporter is
chosen by `OTEL_TRACES_EXPORTER`, default `none` (nothing leaves the process); `console` and
`otlp` (HTTP) are supported; export runs on a bounded `BatchSpanProcessor`. The provider is held
by the service, not the OTel global, so tests swap in an in-memory one. LangSmith stays available
but off by default; when turned on, inputs and outputs are hidden unless explicitly changed.

## Consequences

- Any OTLP backend (Jaeger, Tempo, Honeycomb, a collector) works without code changes; the default
  sends nothing.
- A new attribute must be added to the allowlist on purpose, or it is dropped silently at export:
  privacy by construction, at the cost of an extra step when adding telemetry.
- No auto-instrumentation means every span is written by hand; coverage of new code depends on
  review (span-tree tests pin the current trees).
- Cost and usage live in `llm_calls` rows (queryable, priced from `config/model_prices.json`), not
  in the trace backend, so the ADMIN summary works without one.
- Follow-up: the gateway (ecommerce-infra) routes `/api/ai/usage/summary`.
