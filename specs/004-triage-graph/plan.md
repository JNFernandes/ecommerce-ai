# Implementation Plan: US-04 Triage graph

**Branch**: `feature/US-04-triage-graph` | **Date**: 2026-11-24 | **Spec**: [spec.md](spec.md)

## Summary

A lifespan background worker picks settled PENDING_TRIAGE cases (`FOR UPDATE SKIP LOCKED`), runs a
LangGraph flow (classify → draft → gatekeeper → revise ≤2 → finalize | fallback template) using only
stored evidence, and in the same transaction saves one result, one outbox event and the COMPLETED
status. An outbox relay publishes `ai.order-triage-completed` with backoff and a stable `eventId`.
Anthropic SDK calls have timeout, bounded retries, per-case cap and a DB-tracked daily budget.
Decisions: [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.12 (uv)
**Primary Dependencies**: + `langgraph`, `anthropic` (added now; both in the constitution stack)
**Storage**: PostgreSQL 16: migration 0002 adds 3 tables + COMPLETED, see [data-model.md](data-model.md)
**Testing**: pytest-asyncio; fake `TriageModel`, in-memory stores, `FakeProducer`; integration on
real PostgreSQL; one `live_llm` test (excluded by default)
**Target Platform**: Linux container, port 8002
**Project Type**: web service (single project)
**Performance Goals**: case explained ≤ 10 s after settling (SC-004); `/health` ≤ 1 s with deps down
**Constraints**: no platform/MCP calls, no user token; nothing connects at startup; budget ≤ daily + 1 cap
**Scale/Scope**: 1 worker + 1 relay task, 1 graph (5 nodes), 3 new tables, ~14 settings

## Constitution Check

| Principle | Status |
|---|---|
| I Layering | ✅ graph, gatekeeper, templates, worker loop in `agents/triage` (ports only); prompt in `agents/prompts/triage.v1.md`; SQL/LLM adapters in `infrastructure`; relay + contract in `messaging`. |
| II Security | ✅ no user token, no tools; key is `SecretStr`; logs carry ids/counts only (FR-020); evidence treated as untrusted (FR-014). |
| III AI safety | ✅ deterministic gatekeeper on every draft; template on failure; structured, Pydantic-validated output never partially used; advisory event only. |
| IV Reliability | ✅ (constitution 2.0.0) starts without deps; timeouts, bounded retries, fallback, per-case and daily caps; one result per order; result + outbox + status in one transaction, then publish; `modelId` null on fallback. |
| V Event contracts | ✅ frozen model matching `events.md`, key = order id; ⚠ `events.md` wording follow-up PR in ecommerce-infra before publishing in a shared env. |
| VI Evals | ✅ (constitution 2.0.0) suite arrives in US-09; interim gate: gatekeeper and injection unit tests, pinned prompt hash, one `live_llm` test. |
| VII Observability | ✅ (constitution 2.0.0) one `triage_case` log line per run with latency, tokens, `promptVersion`, `modelId`, `order_id`; trace backend in US-08. |
| VIII/IX Tests | ✅ unit for every node, gatekeeper rule, template, worker, relay; integration for SQL; contract test. |
| XI Gate | ✅ unchanged. |
| Stack | ✅ LangGraph + Anthropic listed; checkpointer not used yet (research §1); schema via migration only. |

Post-design re-check: no deviations (constitution 2.0.0).

## Project Structure

```text
specs/004-triage-graph/  plan.md research.md data-model.md quickstart.md contracts/triage-completed.md

src/ecommerce_ai/
├── agents/
│   ├── prompts/  __init__.py loader.py triage.v1.md
│   ├── triage/
│   │   ├── __init__.py
│   │   ├── model.py        # SuggestedAction, ResultSource, CaseForTriage, DraftOutput, Violation, TriageResult
│   │   ├── policy.py       # default/allowed actions per cause (FR-006)
│   │   ├── gatekeeper.py   # check(draft, case) -> list[Violation] (pure)
│   │   ├── templates.py    # fallback per cause
│   │   ├── ports.py        # TriageModel, TokenBudget, TriageCaseQueue/UnitOfWork, Clock
│   │   ├── graph.py        # LangGraph StateGraph + nodes
│   │   ├── worker.py       # TriageWorker loop + WorkerState
│   │   └── service.py      # triage_one(): pick -> graph -> save
│   └── triage_intake/service.py  # leave COMPLETED cases unchanged
├── infrastructure/
│   ├── settings.py         # + worker/LLM/budget/outbox settings
│   ├── llm/  __init__.py anthropic_model.py   # TriageModel impl (parse, timeout, retries, usage)
│   └── db/   tables.py triage_queue_repository.py token_budget_repository.py
│             outbox_repository.py migrations/versions/0002_triage_results.py
├── messaging/
│   ├── contracts/events.py # + OrderTriageCompleted
│   ├── triage_published.py # TriageResult -> outbox message (payload, key, topic)
│   ├── kafka.py            # + ProducerPort, AIOKafkaProducerAdapter
│   └── outbox_relay.py     # relay loop + RelayState
├── api/health.py           # + triageWorker, outboxRelay
└── main.py                 # wire worker + relay in lifespan; stop both before engine dispose

tests/
├── support/  llm.py (ScriptedModel) kafka.py (+FakeProducer) stores.py (+queue, budget, outbox fakes)
├── unit/     test_gatekeeper.py test_templates.py test_policy.py test_triage_graph.py
│             test_triage_worker.py test_outbox_relay.py test_anthropic_model.py test_prompt_loader.py
│             (+ settings/health/main/intake)
├── contract/ test_event_models.py (+ produced event)
└── integration/ test_triage_queue.py test_token_budget.py test_outbox.py test_triage_end_to_end.py
                 test_migrations.py (+0002)  test_live_llm.py (live_llm)
```

**Also in this story**: constitution amended to 2.0.0 first (IV, VI, VII); `README.md`,
`CLAUDE.md`, `.env.example`, Dockerfile check (prompt `.md` in the wheel); ecommerce-infra `events.md`
PR merged before this one (constitution V).

## Complexity Tracking

| Deviation | Why | Simpler alternative rejected because |
|---|---|---|
| None | Deviations resolved by constitution 2.0.0 | — |
