# 0003. Deterministic fallbacks, timeouts and cost caps for LLM steps

- **Status**: Accepted
- **Date**: 2026-11-23

## Context

The LLM provider can be slow, down, rate limited or return output that fails validation. The
triage worker sits on the event flow and must not stall it. LLM usage costs money per token, and a
loop or abuse could run up an unbounded bill. The service must also start with no LLM key at all.

## Decision

We will give every LLM and HTTP call a timeout (`LLM_TIMEOUT_SECONDS` for the model) and bounded
retries with exponential backoff. Every LLM-backed step has a deterministic fallback; for triage
it is a template explanation built from the event alone, marked by a `modelId` that names the
template. Per-request and per-day token budgets come from configuration (`DAILY_TOKEN_BUDGET`) and
are hard caps: a request that would exceed its budget uses the fallback, and once the daily cap is
reached every step uses its fallback until the next UTC day. An unset `ANTHROPIC_MODEL` means
"always fall back".

## Consequences

- A provider outage degrades answers but never blocks events or startup.
- Costs are bounded by configuration, not by model behaviour.
- Fallback output is less useful and must stay recognisable in events and traces.
- Token accounting must be persisted so the daily cap holds across restarts.
